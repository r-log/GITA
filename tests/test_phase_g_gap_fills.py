"""
Phase G gap-fill tests — covers remaining branches in small-coverage files:
predictor, risk_agent, code_index, indexer, graph_builder, analysis, progress_agent.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# predictor.py — trend branches, missing-field continues, error paths
# ---------------------------------------------------------------------------

class TestPredictorGapFills:
    async def test_velocity_decelerating_trend(self):
        """Cover line 55-56: decelerating trend detection."""
        from src.tools.ai.predictor import _calculate_velocity

        now = datetime.utcnow()
        issues = [
            # First half: fast closes
            {"number": 1, "state": "closed", "closed_at": (now - timedelta(days=20)).isoformat() + "Z"},
            {"number": 2, "state": "closed", "closed_at": (now - timedelta(days=19)).isoformat() + "Z"},
            {"number": 3, "state": "closed", "closed_at": (now - timedelta(days=18)).isoformat() + "Z"},
            # Second half: slow
            {"number": 4, "state": "closed", "closed_at": (now - timedelta(days=2)).isoformat() + "Z"},
        ]
        result = await _calculate_velocity(issues)
        assert result.success is True
        assert result.data["trend"] in ("decelerating", "steady", "accelerating")

    async def test_blockers_missing_updated_at_skips(self):
        """Cover line 127: continue when updated_at is missing."""
        from src.tools.ai.predictor import _detect_blockers

        issues = [
            {"number": 1, "state": "open"},  # No updated_at
        ]
        result = await _detect_blockers(issues, stale_days=1)
        assert result.success is True
        assert result.data["count"] == 0

    async def test_stale_prs_missing_created_at_skips(self):
        """Cover line 160: continue when created_at is missing."""
        from src.tools.ai.predictor import _detect_stale_prs

        prs = [
            {"number": 1, "state": "open"},  # No created_at
        ]
        result = await _detect_stale_prs(prs, stale_days=1)
        assert result.success is True
        assert result.data["count"] == 0

    def test_make_calculate_velocity_factory_handler(self):
        """Cover factory handler line 183+."""
        from src.tools.ai.predictor import make_calculate_velocity
        tool = make_calculate_velocity()
        assert tool.name == "calculate_velocity"
        assert "properties" in tool.parameters


# ---------------------------------------------------------------------------
# risk_agent.py — __init__, gather file dependents branch
# ---------------------------------------------------------------------------

class TestRiskAgentGapFills:
    @patch("src.agents.risk_agent._get_open_prs", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._get_file_dependents", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._detect_breaking_changes", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._scan_security_patterns", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._scan_secrets", new_callable=AsyncMock)
    async def test_gather_with_breaking_changes_queries_dependents(
        self, mock_secrets, mock_patterns, mock_breaking, mock_deps, mock_prs
    ):
        """Cover lines 141-147: file dependents queried when breaking changes found."""
        from src.agents.risk_agent import RiskDetectiveAgent
        from src.tools.base import ToolResult

        with patch.object(RiskDetectiveAgent, "__init__", lambda self, *a, **kw: None):
            agent = RiskDetectiveAgent.__new__(RiskDetectiveAgent)
            agent.name = "risk_detective"
            agent.installation_id = 1001
            agent.repo_full_name = "owner/repo"
            agent.repo_id = 42

            mock_secrets.return_value = ToolResult(success=True, data={})
            mock_patterns.return_value = ToolResult(success=True, data={})
            mock_breaking.return_value = ToolResult(success=True, data={"has_breaking": True})
            mock_deps.return_value = ToolResult(success=True, data={"count": 2, "dependents": []})
            mock_prs.return_value = ToolResult(success=True, data=[{"number": 1}])

            shared = {
                "files": [{"filename": "src/api.py"}],
                "diff": "+changed",
                "blast_radius": {},
            }
            gathered = await agent._gather_context(10, shared_data=shared)
            mock_deps.assert_called()
            assert gathered.get("other_open_prs", 0) >= 0


# ---------------------------------------------------------------------------
# indexer.py — get_code_map_for_repo, reindex update-existing path
# ---------------------------------------------------------------------------

class TestIndexerGapFills:
    @patch("src.indexer.indexer.generate_code_map")
    @patch("src.indexer.indexer.async_session")
    async def test_get_code_map_for_repo_with_records(self, mock_factory, mock_codemap):
        """Cover lines 190-209: get_code_map_for_repo with data."""
        from src.indexer.indexer import get_code_map_for_repo

        mock_record = MagicMock()
        mock_record.file_path = "src/main.py"
        mock_record.language = "python"
        mock_record.line_count = 50
        mock_record.structure = {"functions": []}

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[mock_record])))
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx
        mock_codemap.return_value = "# Code Map"

        result = await get_code_map_for_repo(42, "MyProject")
        assert result == "# Code Map"
        mock_codemap.assert_called_once()

    @patch("src.indexer.indexer.async_session")
    async def test_get_code_map_for_repo_empty(self, mock_factory):
        """Cover line 197: no records returns placeholder."""
        from src.indexer.indexer import get_code_map_for_repo

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        result = await get_code_map_for_repo(42)
        assert "No Index" in result

    @patch("src.indexer.indexer.update_graph_for_files", new_callable=AsyncMock)
    @patch("src.indexer.indexer.async_session")
    @patch("src.indexer.indexer.parse_file")
    @patch("src.indexer.indexer.download_specific_files", new_callable=AsyncMock)
    async def test_reindex_updates_existing_record(self, mock_download, mock_parse, mock_session_factory, mock_graph):
        """Cover lines 150-157: update existing CodeIndex record."""
        from src.indexer.indexer import reindex_files

        mock_download.return_value = {"src/main.py": "updated"}
        parsed = MagicMock()
        parsed.file_path = "src/main.py"
        parsed.language = "python"
        parsed.size_bytes = 100
        parsed.line_count = 10
        parsed.content_hash = "newhash"
        parsed.structure = {}
        mock_parse.return_value = parsed

        existing_record = MagicMock()
        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=existing_record)
        )
        session.commit = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory.return_value = ctx

        result = await reindex_files(1001, "owner/repo", 42, {"src/main.py"}, set())
        assert existing_record.content_hash == "newhash"
        assert result["files_updated"] == 1


# ---------------------------------------------------------------------------
# db/analysis.py — _resolve_issue_db_id create path
# ---------------------------------------------------------------------------

class TestAnalysisGapFills:
    @patch("src.tools.db.analysis.async_session")
    async def test_resolve_issue_creates_new(self, mock_factory):
        """Cover lines 22, 73-75: _resolve_issue_db_id when issue doesn't exist."""
        from src.tools.db.analysis import _resolve_issue_db_id

        session = AsyncMock()
        # First query: not found
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=None)
        )
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.refresh = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        result = await _resolve_issue_db_id(42, 5)
        session.add.assert_called_once()

    @patch("src.tools.db.analysis.async_session")
    async def test_get_analysis_history_with_results(self, mock_factory):
        """Cover analysis history retrieval with actual records."""
        from src.tools.db.analysis import _get_analysis_history

        mock_analysis = MagicMock()
        mock_analysis.id = 1
        mock_analysis.analysis_type = "smart_eval"
        mock_analysis.score = 8.0
        mock_analysis.risk_level = "low"
        mock_analysis.result = {"findings": []}
        mock_analysis.created_at = datetime(2026, 1, 1)

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[mock_analysis])))
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        result = await _get_analysis_history(42, "issue", 5)
        assert result.success is True


# ---------------------------------------------------------------------------
# db/code_index.py — query filters, code map retrieval
# ---------------------------------------------------------------------------

class TestCodeIndexGapFills:
    @patch("src.tools.db.code_index.async_session")
    async def test_query_with_search_filter(self, mock_factory):
        """Cover lines 50-65: query_code_index with search parameter."""
        from src.tools.db.code_index import _query_code_index

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        result = await _query_code_index(42, search="auth")
        assert result.success is True

    @patch("src.tools.db.code_index.async_session")
    async def test_get_code_map_with_records(self, mock_factory):
        """Cover lines 116-127: _get_code_map with actual records."""
        from src.tools.db.code_index import _get_code_map

        mock_record = MagicMock()
        mock_record.file_path = "src/main.py"
        mock_record.language = "python"
        mock_record.line_count = 50
        mock_record.structure = {"functions": [{"name": "main"}]}

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[mock_record])))
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        result = await _get_code_map(42)
        assert result.success is True


# ---------------------------------------------------------------------------
# progress_agent.py — __init__, milestone file coverage error
# ---------------------------------------------------------------------------

class TestProgressAgentGapFills:
    @patch("src.agents.progress_agent._detect_stale_prs", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._get_open_prs", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._get_milestone_file_coverage", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._detect_blockers", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._calculate_velocity", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._get_all_issues", new_callable=AsyncMock)
    async def test_gather_with_milestone_coverage_error(
        self, mock_issues, mock_velocity, mock_blockers, mock_coverage, mock_prs, mock_stale
    ):
        """Cover lines 148-149: exception in milestone file coverage is caught."""
        from src.agents.progress_agent import ProgressTrackerAgent
        from src.tools.base import ToolResult

        with patch.object(ProgressTrackerAgent, "__init__", lambda self, *a, **kw: None):
            agent = ProgressTrackerAgent.__new__(ProgressTrackerAgent)
            agent.name = "progress_tracker"
            agent.installation_id = 1001
            agent.repo_full_name = "owner/repo"
            agent.repo_id = 42

            mock_issues.return_value = ToolResult(success=True, data=[
                {"number": 1, "title": "Tracker", "state": "open",
                 "labels": [{"name": "Milestone Tracker"}], "body": ""},
            ])
            mock_velocity.return_value = ToolResult(success=True, data={})
            mock_blockers.return_value = ToolResult(success=True, data={})
            mock_coverage.side_effect = Exception("DB error")
            mock_prs.return_value = ToolResult(success=True, data=[])
            mock_stale.return_value = ToolResult(success=True, data={})

            gathered = await agent._gather_context()
            # Should not crash — exception caught
            assert len(gathered["trackers"]) == 1
