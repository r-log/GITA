"""Tests for src.agents.onboarding_agent — JSON extraction, _run_pass, handle flow."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.agents.onboarding_agent import _extract_json


# ---------------------------------------------------------------------------
# _extract_json — pure function, no mocks needed
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_pure_json_object(self):
        result = _extract_json('{"milestones": []}')
        assert result == '{"milestones": []}'

    def test_pure_json_array(self):
        result = _extract_json('[1, 2, 3]')
        assert result == '[1, 2, 3]'

    def test_json_in_code_fence(self):
        text = '```json\n{"plan": "test"}\n```'
        result = _extract_json(text)
        assert result == '{"plan": "test"}'

    def test_json_in_fence_without_json_tag(self):
        text = '```\n{"plan": "test"}\n```'
        result = _extract_json(text)
        assert result == '{"plan": "test"}'

    def test_json_with_preamble(self):
        text = 'Here is the plan:\n{"milestones": ["v1", "v2"]}'
        result = _extract_json(text)
        assert result == '{"milestones": ["v1", "v2"]}'

    def test_json_embedded_in_prose_with_fence(self):
        text = 'I analyzed the code.\n\n```json\n{"result": true}\n```\n\nDone.'
        result = _extract_json(text)
        assert result == '{"result": true}'

    def test_no_json_returns_original(self):
        text = 'No JSON here at all'
        result = _extract_json(text)
        assert result == text

    def test_whitespace_stripped(self):
        result = _extract_json('  \n {"key": "val"} \n ')
        assert result == '{"key": "val"}'

    def test_nested_braces(self):
        text = 'Result: {"outer": {"inner": 1}}'
        result = _extract_json(text)
        assert '{"outer": {"inner": 1}}' in result


# ---------------------------------------------------------------------------
# OnboardingAgent._run_pass — tool/model swapping
# ---------------------------------------------------------------------------

class TestRunPass:
    @patch("src.agents.onboarding_agent.index_repository", new_callable=AsyncMock)
    async def test_run_pass_swaps_tools_and_model(self, mock_index):
        """_run_pass temporarily swaps tools and model, then restores."""
        from src.agents.onboarding_agent import OnboardingAgent
        from src.tools.base import Tool, ToolResult

        with patch.object(OnboardingAgent, "__init__", lambda self, *a, **kw: None):
            agent = OnboardingAgent.__new__(OnboardingAgent)
            agent.name = "onboarding"
            agent.description = "test"

            original_tool = Tool(name="original", description="orig", parameters={}, handler=lambda: None)
            agent.tools = [original_tool]
            agent._tool_map = {"original": original_tool}
            agent.model = "original-model"
            agent._usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "by_model": {}}
            agent.system_prompt = "test"
            agent._client = AsyncMock()

            pass_tool = Tool(name="pass_tool", description="pass", parameters={}, handler=lambda: None)

            captured_model = None
            captured_tools = None

            async def mock_run_tool_loop(messages, max_calls=20):
                nonlocal captured_model, captured_tools
                captured_model = agent.model
                captured_tools = [t.name for t in agent.tools]
                return ("pass result", [])

            agent.run_tool_loop = mock_run_tool_loop

            text, log = await agent._run_pass(
                "test_pass", "system prompt", "user content",
                tools=[pass_tool], model="pass-model",
            )

            # During pass, model and tools were swapped
            assert captured_model == "pass-model"
            assert captured_tools == ["pass_tool"]

            # After pass, originals are restored
            assert agent.model == "original-model"
            assert agent.tools == [original_tool]
            assert text == "pass result"


# ---------------------------------------------------------------------------
# OnboardingAgent._step1_index
# ---------------------------------------------------------------------------

class TestStep1Index:
    @patch("src.agents.onboarding_agent.index_repository", new_callable=AsyncMock)
    async def test_calls_index_repository(self, mock_index):
        from src.agents.onboarding_agent import OnboardingAgent

        with patch.object(OnboardingAgent, "__init__", lambda self, *a, **kw: None):
            agent = OnboardingAgent.__new__(OnboardingAgent)
            agent.name = "onboarding"
            agent.installation_id = 1001
            agent.repo_full_name = "owner/repo"
            agent.repo_id = 42

            mock_index.return_value = "# Code Map\n- src/main.py"
            result = await agent._step1_index()
            assert "Code Map" in result
            mock_index.assert_called_once_with(
                installation_id=1001, repo_full_name="owner/repo", repo_id=42,
            )


# ---------------------------------------------------------------------------
# OnboardingAgent._step2_fetch_state
# ---------------------------------------------------------------------------

class TestStep2FetchState:
    @patch("src.agents.onboarding_agent._get_collaborators", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._get_all_issues", new_callable=AsyncMock)
    async def test_detects_fresh_mode(self, mock_issues, mock_collabs):
        from src.agents.onboarding_agent import OnboardingAgent
        from src.tools.base import ToolResult

        with patch.object(OnboardingAgent, "__init__", lambda self, *a, **kw: None):
            agent = OnboardingAgent.__new__(OnboardingAgent)
            agent.name = "onboarding"
            agent.installation_id = 1001
            agent.repo_full_name = "owner/repo"
            agent.repo_id = 42

            mock_issues.return_value = ToolResult(success=True, data=[
                {"number": 1, "title": "Bug", "labels": [{"name": "bug"}]},
            ])
            mock_collabs.return_value = ToolResult(success=True, data=[{"login": "dev1"}])

            result = await agent._step2_fetch_state()
            assert result["is_progressive"] is False
            assert len(result["existing_issues"]) == 1

    @patch("src.agents.onboarding_agent._get_collaborators", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._get_all_issues", new_callable=AsyncMock)
    async def test_detects_progressive_mode(self, mock_issues, mock_collabs):
        from src.agents.onboarding_agent import OnboardingAgent
        from src.tools.base import ToolResult

        with patch.object(OnboardingAgent, "__init__", lambda self, *a, **kw: None):
            agent = OnboardingAgent.__new__(OnboardingAgent)
            agent.name = "onboarding"
            agent.installation_id = 1001
            agent.repo_full_name = "owner/repo"
            agent.repo_id = 42

            mock_issues.return_value = ToolResult(success=True, data=[
                {"number": 1, "title": "Milestone v1", "labels": [{"name": "Milestone Tracker"}]},
            ])
            mock_collabs.return_value = ToolResult(success=True, data=[])

            result = await agent._step2_fetch_state()
            assert result["is_progressive"] is True
            assert len(result["milestone_trackers"]) == 1


# ---------------------------------------------------------------------------
# OnboardingAgent._llm_call
# ---------------------------------------------------------------------------

class TestLlmCall:
    async def test_returns_extracted_json(self):
        from src.agents.onboarding_agent import OnboardingAgent

        with patch.object(OnboardingAgent, "__init__", lambda self, *a, **kw: None):
            agent = OnboardingAgent.__new__(OnboardingAgent)
            agent.name = "onboarding"
            agent.model = "test-model"
            agent._usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "by_model": {}}

            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = '{"plan": "test"}'
            mock_response.usage = MagicMock(prompt_tokens=50, completion_tokens=25)

            agent._client = AsyncMock()
            agent._client.chat.completions.create = AsyncMock(return_value=mock_response)

            result = await agent._llm_call("system prompt", "user content")
            assert '{"plan": "test"}' in result


# ---------------------------------------------------------------------------
# OnboardingAgent.handle — high-level flow
# ---------------------------------------------------------------------------

class TestHandle:
    async def test_fresh_flow(self):
        from src.agents.onboarding_agent import OnboardingAgent

        with patch.object(OnboardingAgent, "__init__", lambda self, *a, **kw: None):
            agent = OnboardingAgent.__new__(OnboardingAgent)
            agent.name = "onboarding"
            agent.description = "test"
            agent.installation_id = 1001
            agent.repo_full_name = "owner/repo"
            agent.repo_id = 42
            agent._usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "by_model": {}}
            agent.system_prompt = "test"
            agent._client = AsyncMock()
            agent.tools = []
            agent._tool_map = {}
            agent._pass_prompts = {}
            agent._validation_tools = []

            # Mock all steps
            agent._step1_index = AsyncMock(return_value="# Code Map")
            agent._step2_fetch_state = AsyncMock(return_value={
                "existing_issues": [], "collaborators": [],
                "milestone_trackers": [], "is_progressive": False,
            })
            agent._step3_milestones = AsyncMock(return_value={
                "milestones": [{"title": "v1", "tasks": [{"title": "Task 1"}]}]
            })
            agent._step3_5_validate = AsyncMock(return_value={"valid": True})
            agent._step4_create_issues = AsyncMock(return_value={
                "milestones_created": 1, "issues_created": 1, "actions": [],
            })

            from src.agents.base import AgentContext
            ctx = AgentContext(
                event_type="installation.created",
                event_payload={},
                repo_full_name="owner/repo",
                installation_id=1001,
                repo_id=42,
            )
            result = await agent.handle(ctx)
            assert result.status == "success"
            agent._step1_index.assert_called_once()


# ---------------------------------------------------------------------------
# Helper: create a mocked OnboardingAgent with no real __init__
# ---------------------------------------------------------------------------

def _make_agent():
    """Create a bare OnboardingAgent for unit testing methods directly."""
    from src.agents.onboarding_agent import OnboardingAgent
    with patch.object(OnboardingAgent, "__init__", lambda self, *a, **kw: None):
        agent = OnboardingAgent.__new__(OnboardingAgent)
        agent.name = "onboarding"
        agent.description = "test"
        agent.installation_id = 1001
        agent.repo_full_name = "owner/repo"
        agent.repo_id = 42
        agent._usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "by_model": {}}
        agent.system_prompt = "test"
        agent._client = AsyncMock()
        agent.tools = []
        agent._tool_map = {}
        agent._pass_prompts = {"pass3_5_validation": "validate prompt"}
        agent._validation_tools = []
        return agent


# ---------------------------------------------------------------------------
# Group 1: _step4_issues -- fresh flow issue creation
# ---------------------------------------------------------------------------

class TestStep4Issues:
    @patch("src.agents.onboarding_agent._save_issue_record", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._create_issue", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._create_label", new_callable=AsyncMock)
    async def test_creates_sub_issues_and_tracker(self, mock_label, mock_create, mock_update, mock_save):
        from src.tools.base import ToolResult
        mock_label.return_value = ToolResult(success=True, data={})
        mock_create.return_value = ToolResult(success=True, data={"number": 10})
        agent = _make_agent()

        scratchpad = {
            "milestones": {
                "milestones": [{
                    "title": "v1",
                    "description": "First milestone",
                    "tasks": [{"title": "Add auth", "status": "not-started", "labels": ["enhancement"], "files": ["auth.py"]}],
                }]
            },
            "existing": {"existing_issues": []},
        }
        summary, log = await agent._step4_issues(scratchpad)
        assert len(log) == 2  # 1 sub-issue + 1 tracker
        assert mock_create.call_count == 2
        mock_save.assert_called()

    @patch("src.agents.onboarding_agent._save_issue_record", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._create_issue", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._create_label", new_callable=AsyncMock)
    async def test_skips_duplicate_titles(self, mock_label, mock_create, mock_update, mock_save):
        from src.tools.base import ToolResult
        mock_label.return_value = ToolResult(success=True, data={})
        mock_create.return_value = ToolResult(success=True, data={"number": 10})
        agent = _make_agent()

        scratchpad = {
            "milestones": {
                "milestones": [{
                    "title": "v1",
                    "tasks": [{"title": "Add auth", "status": "not-started"}],
                }]
            },
            "existing": {"existing_issues": [{"title": "Add auth", "number": 1}]},
        }
        summary, log = await agent._step4_issues(scratchpad)
        assert len(log) == 0  # Skipped duplicate, no tracker

    @patch("src.agents.onboarding_agent._save_issue_record", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._create_issue", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._create_label", new_callable=AsyncMock)
    async def test_closes_done_tasks(self, mock_label, mock_create, mock_update, mock_save):
        from src.tools.base import ToolResult
        mock_label.return_value = ToolResult(success=True, data={})
        mock_create.return_value = ToolResult(success=True, data={"number": 10})
        mock_update.return_value = ToolResult(success=True, data={})
        agent = _make_agent()

        scratchpad = {
            "milestones": {
                "milestones": [{
                    "title": "v1",
                    "tasks": [{"title": "Setup CI", "status": "done", "labels": [], "files": ["ci.yml"]}],
                }]
            },
            "existing": {"existing_issues": []},
        }
        await agent._step4_issues(scratchpad)
        mock_update.assert_called_once()

    @patch("src.agents.onboarding_agent._save_issue_record", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._create_issue", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._create_label", new_callable=AsyncMock)
    async def test_empty_milestones(self, mock_label, mock_create, mock_save):
        from src.tools.base import ToolResult
        mock_label.return_value = ToolResult(success=True, data={})
        agent = _make_agent()

        scratchpad = {"milestones": {"milestones": []}, "existing": {"existing_issues": []}}
        summary, log = await agent._step4_issues(scratchpad)
        assert len(log) == 0
        mock_create.assert_not_called()

    @patch("src.agents.onboarding_agent._save_issue_record", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._create_issue", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._create_label", new_callable=AsyncMock)
    async def test_create_issue_failure_skips(self, mock_label, mock_create, mock_save):
        from src.tools.base import ToolResult
        mock_label.return_value = ToolResult(success=True, data={})
        mock_create.return_value = ToolResult(success=False, error="rate limited")
        agent = _make_agent()

        scratchpad = {
            "milestones": {"milestones": [{"title": "v1", "tasks": [{"title": "Task", "status": "not-started"}]}]},
            "existing": {"existing_issues": []},
        }
        summary, log = await agent._step4_issues(scratchpad)
        assert len(log) == 0


# ---------------------------------------------------------------------------
# Group 2: _step4_progressive_execute
# ---------------------------------------------------------------------------

class TestStep4ProgressiveExecute:
    @patch("src.agents.onboarding_agent._create_issue", new_callable=AsyncMock)
    async def test_create_milestone_action(self, mock_create):
        from src.tools.base import ToolResult
        mock_create.return_value = ToolResult(success=True, data={"number": 20})
        agent = _make_agent()

        scratchpad = {
            "progressive": {
                "actions": [{"type": "create_milestone", "title": "v2", "description": "Second",
                             "tasks": [{"title": "Add API", "description": "REST", "labels": ["enhancement"]}]}],
                "summary": "Created v2",
            },
            "existing": {"existing_issues": []},
        }
        summary, log = await agent._step4_progressive_execute(scratchpad)
        assert len(log) >= 2
        assert mock_create.call_count >= 2

    @patch("src.agents.onboarding_agent._create_issue", new_callable=AsyncMock)
    async def test_create_issue_action(self, mock_create):
        from src.tools.base import ToolResult
        mock_create.return_value = ToolResult(success=True, data={"number": 25})
        agent = _make_agent()

        scratchpad = {
            "progressive": {"actions": [{"type": "create_issue", "title": "Fix bug", "description": "A bug", "labels": ["bug"]}], "summary": "Created"},
            "existing": {"existing_issues": []},
        }
        summary, log = await agent._step4_progressive_execute(scratchpad)
        assert len(log) == 1
        assert log[0]["tool"] == "create_issue"

    @patch("src.agents.onboarding_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.onboarding_agent._post_comment", new_callable=AsyncMock)
    async def test_close_issue_action(self, mock_comment, mock_update):
        from src.tools.base import ToolResult
        mock_update.return_value = ToolResult(success=True, data={})
        mock_comment.return_value = ToolResult(success=True, data={})
        agent = _make_agent()

        scratchpad = {
            "progressive": {"actions": [{"type": "close_issue", "issue_number": 5, "reason": "Completed"}], "summary": "Closed 1"},
            "existing": {"existing_issues": []},
        }
        summary, log = await agent._step4_progressive_execute(scratchpad)
        assert len(log) == 1
        assert log[0]["tool"] == "close_issue"
        mock_comment.assert_called_once()

    @patch("src.agents.onboarding_agent._post_comment", new_callable=AsyncMock)
    async def test_flag_stale_action(self, mock_comment):
        from src.tools.base import ToolResult
        mock_comment.return_value = ToolResult(success=True, data={})
        agent = _make_agent()

        scratchpad = {
            "progressive": {"actions": [{"type": "flag_stale", "issue_number": 7, "reason": "No activity"}], "summary": "Flagged"},
            "existing": {"existing_issues": []},
        }
        summary, log = await agent._step4_progressive_execute(scratchpad)
        assert len(log) == 1
        assert log[0]["tool"] == "flag_stale"

    async def test_empty_actions(self):
        agent = _make_agent()
        scratchpad = {"progressive": {"actions": [], "summary": "Nothing"}, "existing": {"existing_issues": []}}
        summary, log = await agent._step4_progressive_execute(scratchpad)
        assert len(log) == 0


# ---------------------------------------------------------------------------
# Group 3: _step3_5_validate -- fuzzy dedup + cleanup
# ---------------------------------------------------------------------------

class TestStep3_5Validate:
    async def test_auto_skips_high_fuzzy_match(self):
        agent = _make_agent()
        scratchpad = {
            "milestones": {"milestones": [{"title": "v1", "tasks": [{"title": "Implement user authentication", "status": "not-started"}]}]},
            "existing": {"existing_issues": [{"title": "Implement user authentication", "number": 5}]},
        }
        result = await agent._step3_5_validate(scratchpad)
        remaining_tasks = result["milestones"][0]["tasks"] if result["milestones"] else []
        assert len(remaining_tasks) == 0

    async def test_no_match_keeps_tasks(self):
        agent = _make_agent()
        scratchpad = {
            "milestones": {"milestones": [{"title": "v1", "tasks": [{"title": "Build payment gateway", "status": "not-started"}]}]},
            "existing": {"existing_issues": [{"title": "Fix login bug", "number": 1}]},
        }
        result = await agent._step3_5_validate(scratchpad)
        assert len(result["milestones"]) == 1
        assert len(result["milestones"][0]["tasks"]) == 1

    async def test_removes_empty_milestones(self):
        agent = _make_agent()
        scratchpad = {
            "milestones": {"milestones": [{"title": "v1", "tasks": [{"title": "Add auth", "status": "not-started"}]}]},
            "existing": {"existing_issues": [{"title": "Add auth", "number": 1}]},
        }
        result = await agent._step3_5_validate(scratchpad)
        assert len(result["milestones"]) == 0

    async def test_no_existing_issues(self):
        agent = _make_agent()
        scratchpad = {
            "milestones": {"milestones": [{"title": "v1", "tasks": [{"title": "Build API", "status": "not-started"}]}]},
            "existing": {"existing_issues": []},
        }
        result = await agent._step3_5_validate(scratchpad)
        assert len(result["milestones"]) == 1


# ---------------------------------------------------------------------------
# Group 4: Summary builders
# ---------------------------------------------------------------------------

class TestSummaryBuilders:
    def test_build_fresh_summary(self):
        agent = _make_agent()
        scratchpad = {"milestones": {"milestones": [{"title": "v1"}, {"title": "v2"}]}}
        tool_call_log = [
            {"tool": "create_issue", "result": {"success": True}, "args": {"title": "Task 1"}},
            {"tool": "create_issue", "result": {"success": True}, "args": {"title": "v1", "labels": ["Milestone Tracker"]}},
        ]
        result = agent._build_fresh_summary(scratchpad, tool_call_log)
        assert "Onboarding Complete" in result
        assert "Milestones planned: **2**" in result

    def test_build_progressive_summary(self):
        agent = _make_agent()
        scratchpad = {"progressive": {"analysis": {"overall_health": "good", "completed_since_last_run": ["Task A done"]}}}
        tool_call_log = [
            {"tool": "create_issue", "result": {"success": True}},
            {"tool": "close_issue", "result": {"success": True}},
        ]
        result = agent._build_progressive_summary(scratchpad, tool_call_log)
        assert "Progressive Update Complete" in result
        assert "good" in result

    def test_build_summary_comment_dispatches(self):
        agent = _make_agent()
        scratchpad = {"milestones": {"milestones": []}, "progressive": {"analysis": {}}}
        fresh = agent._build_summary_comment(scratchpad, [], is_progressive=False)
        assert "Onboarding Complete" in fresh
        prog = agent._build_summary_comment(scratchpad, [], is_progressive=True)
        assert "Progressive Update" in prog


# ---------------------------------------------------------------------------
# Group 5: Progressive context + handle flows
# ---------------------------------------------------------------------------

class TestBuildProgressiveContext:
    def test_builds_context_with_trackers(self):
        agent = _make_agent()
        scratchpad = {
            "code_map": "# Code Map\n- src/main.py",
            "existing": {
                "existing_issues": [
                    {"number": 1, "title": "Tracker v1", "state": "open",
                     "labels": [{"name": "Milestone Tracker"}],
                     "body": "- [x] Task A (#2)\n- [ ] Task B (#3)"},
                    {"number": 2, "title": "Task A", "state": "closed", "labels": []},
                    {"number": 3, "title": "Task B", "state": "open", "labels": []},
                ],
                "milestone_trackers": [
                    {"number": 1, "title": "Tracker v1", "state": "open",
                     "labels": [{"name": "Milestone Tracker"}],
                     "body": "- [x] Task A (#2)\n- [ ] Task B (#3)"},
                ],
            },
        }
        result = agent._build_progressive_context(scratchpad)
        assert "Code Map" in result
        assert "Tracker v1" in result


class TestHandleProgressiveFlow:
    async def test_progressive_flow(self):
        agent = _make_agent()
        agent._step1_index = AsyncMock(return_value="# Code Map")
        agent._step2_fetch_state = AsyncMock(return_value={
            "existing_issues": [{"number": 1, "title": "Tracker", "labels": [{"name": "Milestone Tracker"}], "body": "- [ ] Task (#2)", "state": "open"}],
            "collaborators": [], "milestone_trackers": [{"number": 1}], "is_progressive": True,
        })
        agent._step3_progressive = AsyncMock(return_value={
            "actions": [], "analysis": {"overall_health": "good"}, "summary": "No changes", "overall_confidence": 0.9,
        })
        agent._step4_progressive_execute = AsyncMock(return_value=("Done", []))
        agent._build_summary_comment = MagicMock(return_value="Summary")

        with patch("src.agents.onboarding_agent._save_onboarding_run", new_callable=AsyncMock):
            from src.agents.base import AgentContext
            ctx = AgentContext(event_type="installation.created", event_payload={}, repo_full_name="owner/repo", installation_id=1001, repo_id=42)
            result = await agent.handle(ctx)
            assert result.status == "progressive_update"
            agent._step3_progressive.assert_called_once()

    async def test_handle_index_failure(self):
        agent = _make_agent()
        agent._step1_index = AsyncMock(side_effect=Exception("Index failed"))

        from src.agents.base import AgentContext
        ctx = AgentContext(event_type="installation.created", event_payload={}, repo_full_name="owner/repo", installation_id=1001, repo_id=42)
        result = await agent.handle(ctx)
        assert result.status == "failed"
