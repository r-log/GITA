"""Isolated execution of generated tests.

Threat model
------------
A generated test file is LLM output conditioned on repository content,
and importing it imports repository code. Both are untrusted: a
contributor can plant instructions in a docstring, or simply commit a
module that does something at import time. Running the tests therefore
means running attacker-influenced code, and it must not be able to
reach anything the worker holds — the GitHub App private key (which
mints tokens for *every* installation), LLM and embedding API keys, the
database URL — or the network.

Two backends implement :class:`Sandbox`:

* :class:`DockerSandbox` — **the security boundary.** One throwaway
  container per run: no network, read-only workspace mount, no host
  environment variables, unprivileged user, all capabilities dropped,
  ``no-new-privileges``, pid/memory/CPU limits, tmpfs ``/tmp``.
* :class:`LocalSandbox` — a subprocess in a throwaway copy of the repo
  with a scrubbed environment. **Not a security boundary** (same uid,
  same filesystem, same network). Only for repositories you already
  trust, e.g. running ``gita generate-tests`` against your own code.

Both backends share the same workspace layout and the same interpreter
bootstrap, so a test that passes under one passes under the other given
the same dependencies.

Interpreter bootstrap
---------------------
Tests run as ``python -I -B -c <bootstrap>``. Isolated mode ignores
``PYTHON*`` environment variables and keeps the working directory off
``sys.path``; the bootstrap then *appends* the repo's import roots, so a
repository file named ``pytest.py`` or ``json.py`` cannot shadow the
real tool or the standard library and fake a passing run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from gita.indexer.imports import discover_package_roots
from gita.indexer.walker import SKIP_DIRS

logger = logging.getLogger(__name__)

# Files above this size are not copied into the workspace. Generated
# tests import source modules, not data blobs; this keeps a repo with
# committed artifacts from turning every verification into a large copy.
_MAX_COPY_FILE_BYTES = 5 * 1024 * 1024

# Cap captured output so a test that prints in a loop can't balloon the
# job result or the log line.
_MAX_OUTPUT_CHARS = 20_000

_BOOTSTRAP = (
    "import json, sys\n"
    "sys.path.extend(json.loads(sys.argv[1]))\n"
    "import pytest\n"
    "raise SystemExit(pytest.main(sys.argv[2:]))\n"
)

_SUMMARY_COUNT_RE = re.compile(
    r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)"
)


class SandboxUnavailableError(RuntimeError):
    """The configured sandbox backend cannot run on this host."""


class InvalidTestPathError(ValueError):
    """A test file path would escape the workspace or isn't a test file."""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SandboxRun:
    """Raw outcome of one pytest invocation inside a sandbox."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class PytestCounts:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0


def parse_pytest_counts(output: str) -> PytestCounts:
    """Extract pass/fail counts from pytest's final summary line.

    Only the last line that mentions a count is used, so test output
    that happens to contain "3 passed" doesn't confuse the parse.
    """
    for line in reversed(output.strip().splitlines()):
        matches = _SUMMARY_COUNT_RE.findall(line)
        if not matches:
            continue
        counts: dict[str, int] = {}
        for number, label in matches:
            key = "errors" if label.startswith("error") else label
            counts[key] = counts.get(key, 0) + int(number)
        return PytestCounts(
            passed=counts.get("passed", 0),
            failed=counts.get("failed", 0),
            errors=counts.get("errors", 0),
            skipped=counts.get("skipped", 0),
        )
    return PytestCounts()


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------
def validate_test_file_path(path: str) -> str:
    """Return ``path`` normalized, or raise :class:`InvalidTestPathError`.

    The path is used twice: inside the sandbox workspace, and as the
    file GITA writes on a branch of the target repo. It must therefore
    stay inside the repo *and* be recognisably a test file, so a bad
    value can't overwrite ``src/app.py`` or ``../../etc/cron.d/x``.
    """
    if not path or "\\" in path or "\x00" in path:
        raise InvalidTestPathError(f"invalid test file path: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or re.match(r"^[A-Za-z]:", path):
        raise InvalidTestPathError(f"test file path must be relative: {path!r}")
    if any(part in ("..", "") for part in pure.parts):
        raise InvalidTestPathError(
            f"test file path must not contain '..': {path!r}"
        )
    name = pure.name
    if pure.suffix != ".py" or not (
        name.startswith("test_") or name.endswith("_test.py")
    ):
        raise InvalidTestPathError(
            "test file path must name a pytest file "
            f"(test_*.py or *_test.py): {path!r}"
        )
    return str(pure)


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Workspace:
    """A throwaway copy of the repo with the generated test written in.

    ``root`` contains ``repo/`` (the copy) and ``pytest.ini`` (an empty
    config, so the repo's own pytest settings — ``addopts`` that need
    plugins we don't have, for instance — don't leak into the run).
    """

    root: Path
    test_relpath: str
    import_roots: tuple[str, ...]  # POSIX paths relative to repo/

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def ini(self) -> Path:
        return self.root / "pytest.ini"


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in SKIP_DIRS:
            ignored.add(name)
            continue
        full = os.path.join(directory, name)
        try:
            if (
                not os.path.islink(full)
                and os.path.isfile(full)
                and os.path.getsize(full) > _MAX_COPY_FILE_BYTES
            ):
                ignored.add(name)
        except OSError:
            ignored.add(name)
    return ignored


def _make_world_readable(root: Path) -> None:
    """Let the container's unprivileged user read the workspace.

    The parent temp dir stays 0700, so other host users still can't
    traverse into it; the bind mount doesn't need host-side traversal.
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            full = os.path.join(dirpath, name)
            if not os.path.islink(full):
                os.chmod(full, 0o644)


@contextmanager
def prepare_workspace(
    repo_root: Path, test_relpath: str, test_content: str
) -> Iterator[Workspace]:
    """Copy ``repo_root`` into a temp dir and write the test file there.

    The real working tree is never touched. Symlinks are copied as
    links, not followed, so a committed ``x.py -> /etc/shadow`` doesn't
    pull host files into the workspace.
    """
    test_relpath = validate_test_file_path(test_relpath)
    with tempfile.TemporaryDirectory(prefix="gita-sandbox-") as parent:
        root = Path(parent) / "ws"
        repo_copy = root / "repo"
        shutil.copytree(
            repo_root, repo_copy, symlinks=True, ignore=_copy_ignore
        )
        test_path = repo_copy / test_relpath
        test_path.parent.mkdir(parents=True, exist_ok=True)
        # A committed symlink (``tests -> /etc``) must not redirect the
        # write outside the workspace.
        if not test_path.parent.resolve().is_relative_to(repo_copy.resolve()):
            raise InvalidTestPathError(
                f"test file path resolves outside the repo: {test_relpath!r}"
            )
        if test_path.is_symlink():
            test_path.unlink()
        test_path.write_text(test_content, encoding="utf-8")
        (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        _make_world_readable(root)

        import_roots: list[str] = []
        for pkg_root in discover_package_roots(repo_copy):
            try:
                rel = pkg_root.resolve().relative_to(repo_copy.resolve())
            except ValueError:
                continue  # resolved through a symlink to outside the copy
            posix = rel.as_posix()
            if posix not in import_roots:
                import_roots.append(posix)
        if "." not in import_roots:
            import_roots.append(".")

        yield Workspace(
            root=root,
            test_relpath=test_relpath,
            import_roots=tuple(import_roots),
        )


def _pytest_args(ini: str, rootdir: str, test_relpath: str) -> list[str]:
    return [
        "-q",
        "--no-header",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        "--noconftest",
        "--import-mode=importlib",
        "-c",
        ini,
        "--rootdir",
        rootdir,
        test_relpath,
    ]


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
class Sandbox(Protocol):
    """Runs pytest against a prepared workspace."""

    name: str
    #: True only for backends that are a real security boundary.
    is_isolated: bool

    async def run_pytest(
        self, workspace: Workspace, *, timeout: float
    ) -> SandboxRun:
        ...


def _posix_preexec(cpu_seconds: int) -> None:  # pragma: no cover - child side
    import resource

    os.setsid()
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    file_cap = 64 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_cap, file_cap))


class LocalSandbox:
    """Subprocess with a scrubbed environment. NOT a security boundary.

    Protects against accidents (secrets in ``os.environ`` leaking into
    test output, the repo's pytest config or a stray ``pytest.py``
    hijacking the run, runaway CPU) but not against a hostile repo: the
    child runs as the worker's user and can read whatever that user can.
    """

    name = "local"
    is_isolated = False

    def __init__(self, python: str | None = None) -> None:
        self.python = python or sys.executable

    def _env(self, workspace: Workspace) -> dict[str, str]:
        tmp = workspace.root / "tmp"
        tmp.mkdir(exist_ok=True)
        env = {
            "PATH": os.defpath,
            "HOME": str(workspace.root),
            "TMPDIR": str(tmp),
            "LANG": "C.UTF-8",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        # Windows needs these for the interpreter to start at all.
        for key in ("SYSTEMROOT", "WINDIR"):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    async def run_pytest(
        self, workspace: Workspace, *, timeout: float
    ) -> SandboxRun:
        import_roots = [
            str((workspace.repo / r).resolve()) for r in workspace.import_roots
        ]
        cmd = [
            self.python,
            "-I",
            "-B",
            "-c",
            _BOOTSTRAP,
            json.dumps(import_roots),
            *_pytest_args(
                str(workspace.ini), str(workspace.repo), workspace.test_relpath
            ),
        ]
        kwargs: dict = {}
        if os.name == "posix":
            cpu = max(1, int(timeout) + 5)
            kwargs["preexec_fn"] = lambda: _posix_preexec(cpu)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace.repo),
            env=self._env(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError:
            _kill_tree(process)
            await process.wait()
            return SandboxRun(
                returncode=-1,
                stdout="",
                stderr=f"timed out after {timeout:.0f}s",
                timed_out=True,
            )
        return SandboxRun(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=_truncate(stdout.decode("utf-8", errors="replace")),
            stderr=_truncate(stderr.decode("utf-8", errors="replace")),
        )


def _kill_tree(process: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


# Environment variables the *docker CLI* (a trusted host process) may
# need to reach the daemon. None of these are forwarded into the
# container — the container gets no host environment at all.
_DOCKER_CLIENT_ENV = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "SYSTEMROOT",
)


class DockerSandbox:
    """One locked-down container per run. This is the security boundary.

    The image must provide ``python`` with ``pytest`` installed, plus any
    third-party packages the target repo's modules import (see
    ``docker/sandbox.Dockerfile``). There is no network inside the
    container, so nothing can be installed at run time.
    """

    name = "docker"
    is_isolated = True

    def __init__(
        self,
        image: str,
        *,
        docker: str = "docker",
        memory: str = "512m",
        cpus: str = "1",
        pids_limit: int = 128,
    ) -> None:
        self.image = image
        self.docker = docker
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit

    def build_command(self, workspace: Workspace, container_name: str) -> list[str]:
        import_roots = [
            str(PurePosixPath("/work/repo") / r) for r in workspace.import_roots
        ]
        return [
            self.docker,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--user",
            "65534:65534",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "--volume",
            f"{workspace.root}:/work:ro",
            "--workdir",
            "/work/repo",
            self.image,
            "python",
            "-I",
            "-B",
            "-c",
            _BOOTSTRAP,
            json.dumps(import_roots),
            *_pytest_args("/work/pytest.ini", "/work/repo", workspace.test_relpath),
        ]

    def _client_env(self) -> dict[str, str]:
        return {k: os.environ[k] for k in _DOCKER_CLIENT_ENV if k in os.environ}

    async def run_pytest(
        self, workspace: Workspace, *, timeout: float
    ) -> SandboxRun:
        if shutil.which(self.docker) is None:
            raise SandboxUnavailableError(
                f"docker CLI {self.docker!r} not found on PATH"
            )
        container_name = f"gita-sandbox-{uuid.uuid4().hex[:12]}"
        cmd = self.build_command(workspace, container_name)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            env=self._client_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError:
            # Killing the CLI doesn't stop the container; kill it by name.
            await self._force_remove(container_name)
            process.kill()
            await process.wait()
            return SandboxRun(
                returncode=-1,
                stdout="",
                stderr=f"timed out after {timeout:.0f}s",
                timed_out=True,
            )
        returncode = process.returncode if process.returncode is not None else -1
        err_text = stderr.decode("utf-8", errors="replace")
        # 125 = docker itself failed (daemon down, image missing, bad flag).
        if returncode == 125:
            raise SandboxUnavailableError(
                f"docker run failed: {err_text.strip()[:500]}"
            )
        return SandboxRun(
            returncode=returncode,
            stdout=_truncate(stdout.decode("utf-8", errors="replace")),
            stderr=_truncate(err_text),
        )

    async def _force_remove(self, container_name: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            self.docker,
            "rm",
            "--force",
            container_name,
            env=self._client_env(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


def make_sandbox(backend: str, *, image: str) -> Sandbox:
    """Build the sandbox named by configuration (``TEST_GEN_SANDBOX``)."""
    if backend == "docker":
        return DockerSandbox(image)
    if backend == "local":
        return LocalSandbox()
    raise ValueError(f"unknown sandbox backend {backend!r}")
