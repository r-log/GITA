"""Tests for the neutral 'Repository Facts' section that replaced _detect_gaps()."""

import pytest

from src.indexer.code_map import generate_code_map


def _sample_records() -> list[dict]:
    return [
        {
            "file_path": "src/app.py",
            "language": "python",
            "line_count": 120,
            "structure": {
                "functions": [{"name": "main", "line": 10, "end_line": 40}],
                "routes": [],
            },
        },
        {
            "file_path": "Dockerfile",
            "language": "docker",
            "line_count": 20,
            "structure": {},
        },
        {
            "file_path": ".github/workflows/ci.yml",
            "language": "yaml",
            "line_count": 40,
            "structure": {},
        },
        {
            "file_path": "README.md",
            "language": "markdown",
            "line_count": 60,
            "structure": {},
        },
    ]


class TestRepositoryFactsSection:
    def test_section_present(self):
        code_map = generate_code_map(_sample_records(), project_name="demo")
        assert "## Repository Facts" in code_map

    def test_no_judgmental_language(self):
        """
        The whole point of replacing _detect_gaps is to stop priming the LLM
        with words like 'missing' or 'needs'. The facts section must use
        neutral language only.
        """
        code_map = generate_code_map(_sample_records(), project_name="demo")
        facts_start = code_map.index("## Repository Facts")
        facts_block = code_map[facts_start:].lower()
        banned = ["missing", "needs", "should", "low coverage", "gap", "detected gaps"]
        for word in banned:
            assert word not in facts_block, f"banned word '{word}' leaked into facts section"

    def test_reports_dockerfile_present(self):
        code_map = generate_code_map(_sample_records(), project_name="demo")
        assert "dockerfile_present: True" in code_map

    def test_reports_readme_present(self):
        code_map = generate_code_map(_sample_records(), project_name="demo")
        assert "readme_present: True" in code_map

    def test_reports_workflow_count(self):
        code_map = generate_code_map(_sample_records(), project_name="demo")
        assert "github_workflows: 1" in code_map

    def test_reports_test_file_count(self):
        records = _sample_records() + [
            {
                "file_path": "tests/test_app.py",
                "language": "python",
                "line_count": 30,
                "structure": {},
            }
        ]
        code_map = generate_code_map(records, project_name="demo")
        assert "test_files: 1" in code_map

    def test_empty_repo_does_not_crash(self):
        code_map = generate_code_map([], project_name="empty")
        # Empty-repo path returns the "Empty Repository" banner, no facts section
        assert "Empty Repository" in code_map
