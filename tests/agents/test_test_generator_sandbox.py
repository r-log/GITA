"""Tests for the generated-test sandbox.

The local backend is exercised directly. The Docker backend is covered
twice: its command line is asserted unconditionally (that's where the
isolation flags live), and a real container run executes when a Docker
daemon and the sandbox image are available — set
``GITA_SANDBOX_IMAGE`` to override the image name (default
``gita-sandbox:latest``; build it from ``docker/sandbox.Dockerfile``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from gita.agents.test_generator.sandbox import (
    DockerSandbox,
    InvalidTestPathError,
    LocalSandbox,
    PytestCounts,
    SandboxUnavailableError,
    make_sandbox,
    parse_pytest_counts,
    prepare_workspace,
    validate_test_file_path,
)

_PASSING = "def test_ok():\n    assert 1 + 1 == 2\n"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("")
    (repo / "src" / "pkg" / "mod.py").write_text("def double(x):\n    return 2 * x\n")
    return repo


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
class TestValidateTestFilePath:
    @pytest.mark.parametrize(
        "path",
        ["tests/test_mod.py", "test_mod.py", "pkg/tests/mod_test.py"],
    )
    def test_accepts_relative_pytest_files(self, path):
        assert validate_test_file_path(path) == path

    def test_normalizes_redundant_separators(self):
        assert validate_test_file_path("./tests//test_x.py") == "tests/test_x.py"

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/etc/test_x.py",
            "C:/temp/test_x.py",
            "..\\test_x.py",
            "../test_x.py",
            "tests/../../test_x.py",
            "src/app.py",  # not a test file: would overwrite source
            "tests/test_x.txt",
        ],
    )
    def test_rejects_escapes_and_non_test_files(self, path):
        with pytest.raises(InvalidTestPathError):
            validate_test_file_path(path)


class TestParsePytestCounts:
    def test_mixed_summary(self):
        out = "....F\n1 failed, 4 passed, 2 skipped in 0.12s\n"
        assert parse_pytest_counts(out) == PytestCounts(passed=4, failed=1, errors=0, skipped=2)

    def test_uses_last_summary_line_only(self):
        out = "print: 99 passed (fake)\n2 passed in 0.01s\n"
        assert parse_pytest_counts(out).passed == 2

    def test_errors(self):
        assert parse_pytest_counts("1 error in 0.1s").errors == 1

    def test_no_summary(self):
        assert parse_pytest_counts("") == PytestCounts()


def test_make_sandbox():
    assert isinstance(make_sandbox("local", image="x"), LocalSandbox)
    docker = make_sandbox("docker", image="img:1")
    assert isinstance(docker, DockerSandbox) and docker.image == "img:1"
    with pytest.raises(ValueError):
        make_sandbox("chroot", image="x")


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------
class TestWorkspace:
    def test_copies_repo_and_discovers_import_roots(self, tmp_path):
        repo = _repo(tmp_path)
        with prepare_workspace(repo, "tests/test_mod.py", _PASSING) as ws:
            assert (ws.repo / "src" / "pkg" / "mod.py").is_file()
            assert (ws.repo / "tests" / "test_mod.py").read_text() == _PASSING
            assert "src" in ws.import_roots
            assert ws.ini.read_text() == "[pytest]\n"
        assert not ws.root.exists()  # cleaned up
        assert not (repo / "tests").exists()  # original untouched

    def test_skips_vcs_and_venv_dirs(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text("secret remote")
        (repo / ".venv").mkdir()
        with prepare_workspace(repo, "tests/test_mod.py", _PASSING) as ws:
            assert not (ws.repo / ".git").exists()
            assert not (ws.repo / ".venv").exists()

    @pytest.mark.skipif(os.name != "posix", reason="symlinks")
    def test_symlinked_dir_cannot_redirect_the_test_write(self, tmp_path):
        repo = _repo(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / "tests").symlink_to(outside, target_is_directory=True)
        with (
            pytest.raises(InvalidTestPathError),
            prepare_workspace(repo, "tests/test_mod.py", _PASSING),
        ):
            pass
        assert list(outside.iterdir()) == []

    @pytest.mark.skipif(os.name != "posix", reason="symlinks")
    def test_symlinks_are_copied_as_links_not_followed(self, tmp_path):
        repo = _repo(tmp_path)
        secret = tmp_path / "host_secret.txt"
        secret.write_text("TOP-SECRET")
        (repo / "src" / "leak.py").symlink_to(secret)
        with prepare_workspace(repo, "tests/test_mod.py", _PASSING) as ws:
            assert (ws.repo / "src" / "leak.py").is_symlink()


# ---------------------------------------------------------------------------
# LocalSandbox
# ---------------------------------------------------------------------------
async def _run_local(repo: Path, content: str, *, timeout: float = 60):
    with prepare_workspace(repo, "tests/test_mod.py", content) as ws:
        return await LocalSandbox().run_pytest(ws, timeout=timeout)


class TestLocalSandbox:
    async def test_passing_tests(self, tmp_path):
        content = "from pkg.mod import double\n\ndef test_d():\n    assert double(2) == 4\n"
        run = await _run_local(_repo(tmp_path), content)
        assert run.returncode == 0, run.stdout + run.stderr
        assert parse_pytest_counts(run.stdout).passed == 1

    async def test_failing_tests(self, tmp_path):
        content = "from pkg.mod import double\n\ndef test_d():\n    assert double(2) == 5\n"
        run = await _run_local(_repo(tmp_path), content)
        assert run.returncode == 1
        assert parse_pytest_counts(run.stdout).failed == 1

    async def test_worker_secrets_are_not_inherited(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SECRET")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", "/secrets/app.pem")
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@db/x")
        content = (
            "import os\n\n"
            "def test_env_is_scrubbed():\n"
            "    leaked = {k for k in os.environ if k in {\n"
            "        'OPENROUTER_API_KEY', 'GITHUB_APP_PRIVATE_KEY_PATH', 'DATABASE_URL'}}\n"
            "    assert leaked == set()\n"
        )
        run = await _run_local(_repo(tmp_path), content)
        assert run.returncode == 0, run.stdout
        assert "SECRET" not in run.stdout + run.stderr

    async def test_repo_cannot_shadow_pytest_or_stdlib(self, tmp_path):
        """A committed ``pytest.py`` used to replace the verifier."""
        repo = _repo(tmp_path)
        hijack = "print('HIJACKED'); raise SystemExit(0)\n"
        (repo / "pytest.py").write_text(hijack)
        (repo / "py_compile.py").write_text(hijack)
        (repo / "src" / "json.py").write_text(hijack)
        content = "def test_real_failure():\n    assert False\n"
        run = await _run_local(repo, content)
        assert "HIJACKED" not in run.stdout
        assert run.returncode == 1

    async def test_repo_pytest_config_is_ignored(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\naddopts = '--cov=pkg --needs-plugin'\n"
        )
        (repo / "conftest.py").write_text("raise RuntimeError('conftest ran')\n")
        run = await _run_local(repo, _PASSING)
        assert run.returncode == 0, run.stdout + run.stderr

    async def test_timeout_kills_the_run(self, tmp_path):
        content = "import time\n\ndef test_hang():\n    time.sleep(60)\n"
        run = await _run_local(_repo(tmp_path), content, timeout=3)
        assert run.timed_out is True
        assert run.returncode == -1


# ---------------------------------------------------------------------------
# DockerSandbox — isolation flags (always) + real container (if available)
# ---------------------------------------------------------------------------
class TestDockerCommand:
    def test_isolation_flags(self, tmp_path):
        sandbox = DockerSandbox("img:1")
        with prepare_workspace(_repo(tmp_path), "tests/test_mod.py", _PASSING) as ws:
            cmd = sandbox.build_command(ws, "gita-sandbox-test")
        joined = " ".join(cmd)

        def flag(name: str) -> str:
            return cmd[cmd.index(name) + 1]

        assert flag("--network") == "none"
        assert "--read-only" in cmd
        assert flag("--cap-drop") == "ALL"
        assert flag("--security-opt") == "no-new-privileges"
        assert flag("--user") == "65534:65534"
        assert f"{ws.root}:/work:ro" in cmd
        assert "--pids-limit" in cmd and "--memory" in cmd
        # Only constant env vars reach the container — never host values.
        env_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--env"]
        assert env_values == ["HOME=/tmp", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1"]
        assert "--env-file" not in cmd and "--privileged" not in joined
        assert cmd[-1] == "tests/test_mod.py"

    async def test_missing_docker_cli_is_reported(self, tmp_path):
        sandbox = DockerSandbox("img:1", docker="docker-does-not-exist-xyz")
        with (
            prepare_workspace(_repo(tmp_path), "tests/test_mod.py", _PASSING) as ws,
            pytest.raises(SandboxUnavailableError),
        ):
            await sandbox.run_pytest(ws, timeout=10)


_IMAGE = os.environ.get("GITA_SANDBOX_IMAGE", "gita-sandbox:latest")


def _docker_image_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", _IMAGE],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.mark.skipif(
    not _docker_image_available(),
    reason=f"docker daemon or image {_IMAGE!r} not available",
)
class TestDockerSandboxIntegration:
    async def _run(self, repo: Path, content: str, timeout: float = 60):
        with prepare_workspace(repo, "tests/test_mod.py", content) as ws:
            return await DockerSandbox(_IMAGE).run_pytest(ws, timeout=timeout)

    async def test_passing_and_failing(self, tmp_path):
        repo = _repo(tmp_path)
        ok = await self._run(
            repo, "from pkg.mod import double\n\ndef test_d():\n    assert double(3) == 6\n"
        )
        assert ok.returncode == 0, ok.stdout + ok.stderr
        bad = await self._run(
            repo, "from pkg.mod import double\n\ndef test_d():\n    assert double(3) == 7\n"
        )
        assert bad.returncode == 1

    async def test_no_network_no_secrets_no_writes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SECRET")
        content = (
            "import os, socket, pytest\n\n"
            "def test_no_network():\n"
            "    with pytest.raises(OSError):\n"
            "        socket.create_connection(('1.1.1.1', 443), timeout=3)\n\n"
            "def test_no_host_env():\n"
            "    assert 'OPENROUTER_API_KEY' not in os.environ\n\n"
            "def test_workspace_is_read_only():\n"
            "    with pytest.raises(OSError):\n"
            "        open('/work/repo/planted.py', 'w')\n\n"
            "def test_runs_unprivileged():\n"
            "    assert os.getuid() == 65534\n"
        )
        run = await self._run(_repo(tmp_path), content)
        assert run.returncode == 0, run.stdout + run.stderr
        assert parse_pytest_counts(run.stdout).passed == 4

    async def test_timeout_removes_container(self, tmp_path):
        content = "import time\n\ndef test_hang():\n    time.sleep(120)\n"
        run = await self._run(_repo(tmp_path), content, timeout=8)
        assert run.timed_out is True
        leftover = subprocess.run(
            ["docker", "ps", "-q", "--filter", "name=gita-sandbox-"],
            capture_output=True,
            text=True,
        )
        assert leftover.stdout.strip() == ""
