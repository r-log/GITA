"""Test-generator agent — GITA's only code-writing agent.

Input: a module without tests. Output: three chained Decisions
(``create_branch`` → ``update_file`` → ``open_pr``) that, under
``WriteMode.FULL`` and above the 0.90 code-writing threshold, land a
branch with a generated pytest file and open a PR.

- ``preflight`` — deterministic gates deciding whether a file is worth a
  generation attempt (no LLM, no GitHub).
- ``recipe`` — one LLM call, then static checks and a sandboxed test run.
- ``sandbox`` — where generated tests execute (see its threat model).
- ``bridge`` — pure Decision builder.
"""

from gita.agents.test_generator.bridge import (
    TestGenerationArtifact,
    build_test_generation_decisions,
    compute_branch_name,
    default_pr_body,
    default_pr_title,
)
from gita.agents.test_generator.preflight import (
    FilePreviewResult,
    PreflightResult,
    PreviewSummary,
    has_existing_tests,
    is_feasible,
    preview_repo,
)
from gita.agents.test_generator.recipe import (
    TestGenerationResult,
    derive_test_file_path,
    run_test_generation,
)

__all__ = [
    "FilePreviewResult",
    "PreflightResult",
    "PreviewSummary",
    "TestGenerationArtifact",
    "TestGenerationResult",
    "build_test_generation_decisions",
    "compute_branch_name",
    "default_pr_body",
    "default_pr_title",
    "derive_test_file_path",
    "has_existing_tests",
    "is_feasible",
    "preview_repo",
    "run_test_generation",
]
