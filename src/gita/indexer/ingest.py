"""End-to-end ingest: walk a local repo, parse every source file, and persist
rows into ``repos`` / ``code_index`` / ``import_edges``.

Week 1 strategy is nuke-and-repave per call: for the given repo, we wipe its
existing ``code_index`` and ``import_edges`` rows before reinserting. This is
simple, correct, and cheap for the repo sizes we care about right now.
Incremental updates arrive in Week 2 alongside push-event handling.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex, ImportEdge, Repo
from gita.indexer.imports import discover_package_roots, resolve_import
from gita.indexer.parsers import parse_file
from gita.indexer.walker import iter_files

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    repo_id: str
    files_indexed: int
    functions_extracted: int
    classes_extracted: int
    edges_total: int
    edges_resolved: int
    head_sha: str | None


def _read_head_sha(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


async def _get_or_create_repo(
    session: AsyncSession, name: str, root_path: Path
) -> Repo:
    stmt = select(Repo).where(Repo.name == name)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        existing.root_path = str(root_path)
        return existing
    repo = Repo(name=name, root_path=str(root_path))
    session.add(repo)
    await session.flush()  # materialize repo.id
    return repo


async def _clear_repo_rows(session: AsyncSession, repo_id) -> None:
    await session.execute(
        delete(ImportEdge).where(ImportEdge.repo_id == repo_id)
    )
    await session.execute(
        delete(CodeIndex).where(CodeIndex.repo_id == repo_id)
    )
    await session.flush()


async def index_repository(
    session: AsyncSession,
    repo_name: str,
    root_path: Path,
    *,
    include_tests: bool = False,
) -> IngestResult:
    """Ingest a local repo into the three tables.

    Caller owns the transaction — this function does NOT commit. Commit or
    rollback on the caller's side.
    """
    root_path = root_path.resolve()
    if not root_path.is_dir():
        raise ValueError(f"root_path is not a directory: {root_path}")

    repo = await _get_or_create_repo(session, repo_name, root_path)
    head_sha = _read_head_sha(root_path)
    # Discover package roots ONCE per repo — walks the __init__.py chain to
    # find every directory that absolute Python imports can resolve against.
    # Passed into resolve_import() below so src/ and backend/ layouts work.
    package_roots = discover_package_roots(root_path)
    await _clear_repo_rows(session, repo.id)

    code_rows: list[CodeIndex] = []
    functions_total = 0
    classes_total = 0

    # Pass 1 — parse each file and collect CodeIndex rows
    for discovered in iter_files(root_path, include_tests=include_tests):
        try:
            content = discovered.path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            logger.warning(
                "file_read_failed path=%s error=%s",
                discovered.relative_path,
                exc,
            )
            continue

        structure = parse_file(
            discovered.path, content, discovered.language
        )
        structure_json = structure.to_jsonb()
        functions_total += len(structure.functions)
        classes_total += len(structure.classes)

        row = CodeIndex(
            repo_id=repo.id,
            file_path=discovered.relative_path,
            language=discovered.language,
            content=content,
            line_count=content.count("\n") + (0 if content.endswith("\n") else 1),
            indexed_at_sha=head_sha,
            structure=structure_json,
        )
        code_rows.append(row)

    session.add_all(code_rows)
    await session.flush()

    # Pass 2 — build import edges from the already-persisted structures
    edge_rows: list[ImportEdge] = []
    edges_resolved = 0

    for row in code_rows:
        imports = row.structure.get("imports", []) if row.structure else []
        source_file = root_path / row.file_path
        for imp in imports:
            raw = imp.get("raw", "")
            if not raw:
                continue
            resolved = resolve_import(
                raw,
                source_file,
                root_path,
                row.language,
                package_roots=package_roots,
            )
            dst_file: str | None = None
            if resolved is not None:
                try:
                    dst_file = str(
                        resolved.resolve().relative_to(root_path)
                    ).replace("\\", "/")
                    edges_resolved += 1
                except ValueError:
                    dst_file = None
            edge_rows.append(
                ImportEdge(
                    repo_id=repo.id,
                    src_file=row.file_path,
                    dst_file=dst_file,
                    raw_import=raw,
                    language=row.language,
                )
            )

    session.add_all(edge_rows)

    repo.head_sha = head_sha
    repo.indexed_at = datetime.now(UTC)
    await session.flush()

    return IngestResult(
        repo_id=str(repo.id),
        files_indexed=len(code_rows),
        functions_extracted=functions_total,
        classes_extracted=classes_total,
        edges_total=len(edge_rows),
        edges_resolved=edges_resolved,
        head_sha=head_sha,
    )
