"""Test-generation recipe.

Pipeline:
    1. Build context — fetch target file's content + symbols + neighborhood
    2. LLM call — produce pytest file content (schema-validated)
    3. Verify — three sequential gates that must all pass:
        a. ``ast.parse()``                   (in-process, syntax)
        b. ``python -m py_compile``          (subprocess, bytecode compile)
        c. ``python -m pytest --collect-only`` (subprocess, collection)
    4. Return a ``TestGenerationResult`` — verified or not, with errors

The caller is responsible for turning a verified result into a
``TestGenerationArtifact`` for the bridge. Day 5 wires the CLI; until
then the recipe can be driven directly from tests.
"""
from __future__ import annotations

import asyncio
import ast
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.test_generator.schemas import GeneratedTestResponse
from gita.db.models import CodeIndex
from gita.llm.client import LLMClient
from gita.views._common import resolve_repo
from gita.views.neighborhood import neighborhood_view

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_PROMPT_FILE = "test_generator.md"

_LLM_MAX_TOKENS = 4096
_SOURCE_CONTEXT_CAP_CHARS = 8_000
_SUBPROCESS_TIMEOUT_SECONDS = 30.0

# Verification-pass bonus applied to the LLM's self-reported confidence.
# A test file that cleared all three gates is materially more trustworthy
# than one that only the LLM is confident in; a failed verification pins
# the ceiling well below the default 0.9 code-action threshold so the
# bridge's Decisions auto-downgrade without needing per-agent logic.
_VERIFIED_BONUS = 0.05
_UNVERIFIED_CEILING = 0.4


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class TestGenerationResult:
    """The recipe's output — both success and failure shapes."""

    # Opt out of pytest collection — the ``Test*`` prefix names the
    # domain concept, not a pytest class.
    __test__ = False

    target_file: str
    test_file_path: str
    test_content: str           # populated even on verification failure
    verified: bool
    verification_errors: list[str] = field(default_factory=list)
    llm_model: str = ""
    covered_symbols: list[str] = field(default_factory=list)
    notes: str = ""
    llm_confidence: float = 0.0
    # Final blended confidence — this is what the bridge's Decisions use.
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# Prompt + context
# ---------------------------------------------------------------------------
def _load_prompt() -> str:
    return (PROMPTS_DIR / _PROMPT_FILE).read_text(encoding="utf-8")


async def _get_file_row(
    session: AsyncSession, repo_id: int, file_path: str
) -> CodeIndex | None:
    stmt = select(CodeIndex).where(
        CodeIndex.repo_id == repo_id,
        CodeIndex.file_path == file_path,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _render_symbol_summary(structure: dict[str, Any]) -> str:
    """Render the classes/functions from structure JSONB as one-liners."""
    lines: list[str] = []
    for cls in structure.get("classes", []):
        sig = cls.get("signature") or cls.get("name", "")
        lines.append(f"  class {sig} (lines {cls['start_line']}-{cls['end_line']})")
    for fn in structure.get("functions", []):
        sig = fn.get("signature") or fn.get("name", "")
        parent = fn.get("parent_class")
        prefix = f"{parent}." if parent else ""
        lines.append(
            f"  def {prefix}{sig} "
            f"(lines {fn['start_line']}-{fn['end_line']})"
        )
    return "\n".join(lines) if lines else "  (none)"


async def _build_prompt_context(
    session: AsyncSession, repo_name: str, target_file: str
) -> dict[str, Any] | None:
    """Assemble the facts the LLM needs to generate tests.

    Returns ``None`` when the target file is missing from the index —
    callers should treat that as a hard error, not a retry.
    """
    repo = await resolve_repo(session, repo_name)
    row = await _get_file_row(session, repo.id, target_file)
    if row is None:
        return None
    content = row.content or ""
    structure = row.structure or {}

    neighborhood = await neighborhood_view(session, repo_name, target_file)

    # Cap the raw source so enormous files don't blow the context window;
    # the structure summary carries the signature info for anything
    # truncated.
    source = content
    truncated = False
    if len(source) > _SOURCE_CONTEXT_CAP_CHARS:
        source = source[:_SOURCE_CONTEXT_CAP_CHARS]
        truncated = True

    return {
        "target_file": target_file,
        "source": source,
        "source_truncated": truncated,
        "line_count": len(content.splitlines()),
        "symbol_summary": _render_symbol_summary(structure),
        "imports": [fi.file_path for fi in neighborhood.imports][:10],
        "imported_by": neighborhood.imported_by[:10],
    }


def _render_user_prompt(ctx: dict[str, Any]) -> str:
    lines: list[str] = [
        f"Target module: `{ctx['target_file']}` ({ctx['line_count']} lines)",
        "",
        "Symbol summary (the things to test):",
        ctx["symbol_summary"],
        "",
    ]
    if ctx["imports"]:
        lines.append(
            "This module imports from: "
            + ", ".join(f"`{p}`" for p in ctx["imports"])
        )
        lines.append("")
    if ctx["imported_by"]:
        lines.append(
            f"Imported by {len(ctx['imported_by'])} file(s); top: "
            + ", ".join(f"`{p}`" for p in ctx["imported_by"][:5])
        )
        lines.append("")
    lines.append("Source:")
    lines.append("```python")
    lines.append(ctx["source"])
    if ctx["source_truncated"]:
        lines.append("# ... (source truncated; rely on symbol summary for tail)")
    lines.append("```")
    lines.append("")
    lines.append(
        "Produce a complete pytest test file for this module. Return "
        "only the JSON schema; the `test_file_content` field is the "
        "full file as raw Python source."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verification — the "stronger bar" the user asked for
# ---------------------------------------------------------------------------
def _subprocess_env(pythonpath: str | None) -> dict[str, str] | None:
    """Build an env dict that prepends ``pythonpath`` to the parent's
    ``PYTHONPATH``. Returns ``None`` to inherit the parent env unchanged
    when no PYTHONPATH override is requested — matches the old behavior.
    """
    if not pythonpath:
        return None
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{pythonpath}{os.pathsep}{existing}" if existing else pythonpath
    )
    return env


async def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: float = _SUBPROCESS_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run ``cmd`` as a subprocess and return (returncode, stdout, stderr)."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return (
            -1,
            "",
            f"subprocess timed out after {timeout}s: {' '.join(cmd)}",
        )
    return (
        process.returncode if process.returncode is not None else -1,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


async def verify_test_file(
    content: str,
    work_dir: Path,
    test_file_name: str,
    *,
    pythonpath: str | None = None,
) -> tuple[bool, list[str]]:
    """Three-gate verification. Returns ``(verified, errors)``.

    - Gate 1 — ``ast.parse`` (in-process; cheap). If it fails, skip the
      subprocess gates because they'll fail for the same reason with
      worse error messages.
    - Gate 2 — ``python -m py_compile``. Catches import-time syntax
      oddities the stdlib parser missed.
    - Gate 3 — ``python -m pytest --collect-only``. Catches import
      errors in the test file itself — the most common failure mode for
      LLM-generated tests.

    ``work_dir`` is the (scratch) directory the test file is dropped
    into for subprocess checks. ``pythonpath``, when set, is prepended
    to ``PYTHONPATH`` for the subprocesses so the generated test can
    resolve imports from the target repo without the file ever landing
    in the target's working tree.
    """
    errors: list[str] = []

    # Gate 1 — ast.parse (no subprocess cost)
    try:
        ast.parse(content)
    except SyntaxError as exc:
        errors.append(f"ast.parse failed: {exc}")
        return False, errors

    test_path = work_dir / test_file_name
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(content, encoding="utf-8")

    env = _subprocess_env(pythonpath)

    # Gate 2 — py_compile
    rc, stdout, stderr = await _run_subprocess(
        [sys.executable, "-m", "py_compile", str(test_path)],
        cwd=work_dir,
        env=env,
    )
    if rc != 0:
        msg = (stderr or stdout).strip() or f"py_compile exited {rc}"
        errors.append(f"py_compile failed: {msg}")
        return False, errors

    # Gate 3 — pytest --collect-only.
    # ``--rootdir`` pins pytest to the work_dir so it doesn't climb up
    # the filesystem looking for a pyproject.toml / conftest.py; that
    # would pull in the outer project's pytest config and confuse both
    # real runs (AMASS repo) and tmp_path-based tests here.
    rc, stdout, stderr = await _run_subprocess(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(work_dir),
            str(test_path),
        ],
        cwd=work_dir,
        env=env,
    )
    if rc != 0:
        # Pytest sends collection errors to stdout, not stderr; prefer
        # the richer message.
        msg = (stdout or stderr).strip() or f"pytest collect exited {rc}"
        errors.append(f"pytest --collect-only failed: {msg[:600]}")
        return False, errors

    return True, errors


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def derive_test_file_path(target_file: str) -> str:
    """Default sibling-test location: ``tests/test_<stem>.py``.

    Doesn't inspect the repo's actual test layout — callers with
    knowledge of the target's convention (e.g. AMASS's
    ``backend/tests/``) should override via ``test_file_path``.
    """
    stem = PurePosixPath(target_file).stem
    return f"tests/test_{stem}.py"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
async def run_test_generation(
    session: AsyncSession,
    repo_name: str,
    target_file: str,
    *,
    llm: LLMClient,
    repo_root: Path,
    model: str | None = None,
    test_file_path: str | None = None,
) -> TestGenerationResult:
    """Generate a verified pytest test file for ``target_file``.

    ``repo_root`` is the on-disk path of the indexed repo. It is used
    only as ``PYTHONPATH`` for the verification subprocesses — the
    generated test file is written into a private scratch tempdir, so
    the target repo's working tree is never mutated, even on
    verification failure.

    Returns a ``TestGenerationResult`` populated with content regardless
    of whether verification passed — callers can inspect
    ``verification_errors`` to decide whether to proceed to the bridge.
    When the target file is missing from the index, raises
    ``FileNotFoundError``.
    """
    resolved_test_path = test_file_path or derive_test_file_path(target_file)

    ctx = await _build_prompt_context(session, repo_name, target_file)
    if ctx is None:
        raise FileNotFoundError(
            f"target_file {target_file!r} not found in index for "
            f"repo {repo_name!r}"
        )

    system = _load_prompt()
    user = _render_user_prompt(ctx)

    response = await llm.call(
        system=system,
        user=user,
        response_schema=GeneratedTestResponse,
        model=model,
        max_tokens=_LLM_MAX_TOKENS,
    )
    if not isinstance(response.parsed, GeneratedTestResponse):
        # FakeLLMClient / OpenRouterClient both enforce this; belt-and-braces.
        raise RuntimeError(
            "LLM response did not parse as GeneratedTestResponse"
        )
    payload: GeneratedTestResponse = response.parsed

    with tempfile.TemporaryDirectory(prefix="gita-testgen-") as scratch:
        verified, errors = await verify_test_file(
            payload.test_file_content,
            Path(scratch),
            resolved_test_path,
            pythonpath=str(repo_root),
        )

    llm_conf = max(0.0, min(1.0, payload.confidence))
    blended = (
        min(1.0, llm_conf + _VERIFIED_BONUS)
        if verified
        else min(llm_conf, _UNVERIFIED_CEILING)
    )

    logger.info(
        "test_generation_done target=%s verified=%s llm_conf=%.2f "
        "blended_conf=%.2f errors=%d",
        target_file,
        verified,
        llm_conf,
        blended,
        len(errors),
    )

    return TestGenerationResult(
        target_file=target_file,
        test_file_path=resolved_test_path,
        test_content=payload.test_file_content,
        verified=verified,
        verification_errors=errors,
        llm_model=response.model or (model or ""),
        covered_symbols=list(payload.covered_symbols),
        notes=payload.notes,
        llm_confidence=llm_conf,
        confidence=blended,
    )
