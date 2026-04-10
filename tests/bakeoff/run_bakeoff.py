"""
LLM Bake-off: test tool-calling quality on realistic GITA agent scenarios.

Usage:
    # Test the model currently configured in .env
    python -m tests.bakeoff.run_bakeoff

    # Test a specific model (overrides .env)
    python -m tests.bakeoff.run_bakeoff --model moonshotai/kimi-k2.5
    python -m tests.bakeoff.run_bakeoff --model anthropic/claude-sonnet-4

    # Compare two models side by side
    python -m tests.bakeoff.run_bakeoff --compare moonshotai/kimi-k2.5 anthropic/claude-sonnet-4
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test")
os.environ.setdefault("GITHUB_APP_PRIVATE_KEY_PATH", "tests/fixtures/fake-key.pem")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://t:t@localhost/t")
os.environ.setdefault("REDIS_URL", "redis://localhost")
os.environ.setdefault("ENVIRONMENT", "testing")

# Fix Windows console encoding for Unicode in model output
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from openai import OpenAI
from src.core.config import settings


# -- Tool Schemas (matching real GITA agents) ---------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_issue",
            "description": "Fetch issue details by number.",
            "parameters": {
                "type": "object",
                "properties": {"issue_number": {"type": "integer"}},
                "required": ["issue_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pr",
            "description": "Fetch pull request details.",
            "parameters": {
                "type": "object",
                "properties": {"pr_number": {"type": "integer"}},
                "required": ["pr_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "post_comment",
            "description": "Post a comment on an issue or PR.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_number": {"type": "integer", "description": "Issue or PR number"},
                    "body": {"type": "string", "description": "Comment body (markdown)"},
                },
                "required": ["issue_number", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_analysis",
            "description": "Save analysis results to the database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_type": {"type": "string", "enum": ["issue", "pr", "milestone"]},
                    "target_number": {"type": "integer"},
                    "analysis_type": {"type": "string", "enum": ["smart", "risk", "quality", "progress"]},
                    "result": {"type": "object", "description": "Analysis result data"},
                    "score": {"type": "number", "description": "Overall score 0.0-1.0"},
                    "risk_level": {"type": "string", "enum": ["info", "warning", "critical"]},
                },
                "required": ["target_type", "target_number", "analysis_type", "result"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_check_run",
            "description": "Create a GitHub check run (pass/fail status).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "head_sha": {"type": "string"},
                    "conclusion": {"type": "string", "enum": ["success", "failure", "neutral"]},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["name", "head_sha", "conclusion", "title", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_comments",
            "description": "Search issue/PR comment history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_number": {"type": "integer"},
                    "keyword": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_issue_full",
            "description": "Get full issue details from local DB including body, author, timestamps.",
            "parameters": {
                "type": "object",
                "properties": {"github_number": {"type": "integer"}},
                "required": ["github_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_events",
            "description": "Search webhook event history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string"},
                    "target_number": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


# -- Mock Tool Responses ------------------------------------------

MOCK_RESPONSES = {
    "get_issue": {
        "number": 42, "title": "OAuth login fails on Safari",
        "state": "open", "body": "Users on Safari get stuck on the callback page after OAuth login. The page spins indefinitely. Works fine on Chrome and Firefox.",
        "labels": [{"name": "bug"}, {"name": "auth"}],
        "user": {"login": "sarah-dev"},
        "created_at": "2026-03-15T10:00:00Z",
    },
    "get_pr": {
        "number": 87, "title": "Fix OAuth callback redirect on Safari",
        "state": "open", "body": "Fixes #42. The issue was the SameSite cookie attribute being set to Strict instead of Lax for the OAuth callback.",
        "user": {"login": "john-dev"},
        "head": {"ref": "fix/oauth-safari", "sha": "abc123def456"},
        "base": {"ref": "main"},
        "additions": 15, "deletions": 3, "changed_files": 2,
    },
    "get_issue_full": {
        "number": 42, "title": "OAuth login fails on Safari",
        "state": "open", "body": "Users on Safari get stuck on the callback page after OAuth login.",
        "author": "sarah-dev", "labels": [{"name": "bug"}],
        "created_at": "2026-03-15T10:00:00Z", "closed_at": None,
    },
    "search_comments": [
        {"author": "sarah-dev", "body": "I can reproduce this on Safari 17.4 with privacy relay enabled.", "created_at": "2026-03-15T12:00:00Z"},
        {"author": "john-dev", "body": "I think the SameSite cookie attribute might be the issue. Safari is stricter about this.", "created_at": "2026-03-16T09:00:00Z"},
    ],
    "search_events": [
        {"event_type": "issues", "action": "opened", "sender": "sarah-dev", "target_number": 42, "received_at": "2026-03-15T10:00:00Z"},
        {"event_type": "pull_request", "action": "opened", "sender": "john-dev", "target_number": 87, "received_at": "2026-03-17T14:00:00Z"},
    ],
    "save_analysis": {"saved": True},
    "post_comment": {"id": 99001, "html_url": "https://github.com/test/repo/issues/42#comment-99001"},
    "create_check_run": {"id": 55001},
}


# -- Test Scenarios -----------------------------------------------

SCENARIOS = [
    {
        "name": "PR Review (simple)",
        "system_prompt": (
            "You are a PR reviewer for a GitHub repository. Analyze the PR, check if it addresses the linked issue, "
            "and post a review comment. Save your analysis to the database. Create a check run with your verdict.\n\n"
            "CONTEXT:\n"
            "- Repository: test-owner/test-repo\n"
            "- Event: pull_request.opened\n"
            "- PR #87: 'Fix OAuth callback redirect on Safari'\n"
            "- Diff: Changes auth/oauth.py (SameSite=Lax) and templates/oauth_callback.html\n"
            "- The PR body says 'Fixes #42'\n\n"
            "Available information:\n"
            "- PR diff shows: cookie SameSite attribute changed from 'Strict' to 'Lax'\n"
            "- 2 files changed, 15 additions, 3 deletions\n"
            "- No test files were modified\n\n"
            "Your job: fetch the linked issue, review the change, post a comment, save analysis, create check run."
        ),
        "expected_tools": {"get_issue", "post_comment", "save_analysis", "create_check_run"},
        "expected_targets": {"issue_number": [42, 87], "pr_number": [87], "target_number": [42, 87]},
        "max_expected_calls": 8,
    },
    {
        "name": "Issue Analysis (S.M.A.R.T.)",
        "system_prompt": (
            "You are an issue analyst. Evaluate this newly opened issue against S.M.A.R.T. criteria.\n\n"
            "CONTEXT:\n"
            "- Repository: test-owner/test-repo\n"
            "- Event: issues.opened\n"
            "- Issue #42: 'OAuth login fails on Safari'\n\n"
            "Your job: fetch the full issue details, search for related comments and events, "
            "evaluate the issue quality, save your analysis, and post a helpful comment with suggestions."
        ),
        "expected_tools": {"get_issue_full", "save_analysis", "post_comment"},
        "expected_targets": {"github_number": [42], "issue_number": [42], "target_number": [42]},
        "max_expected_calls": 8,
    },
    {
        "name": "Multi-step Research",
        "system_prompt": (
            "You are an issue analyst investigating a bug report. You need to gather context before making a decision.\n\n"
            "CONTEXT:\n"
            "- Repository: test-owner/test-repo\n"
            "- Event: issues.opened\n"
            "- Issue #42: 'OAuth login fails on Safari'\n\n"
            "Your job:\n"
            "1. First, fetch the full issue details\n"
            "2. Search for any comments on this issue\n"
            "3. Search for related events\n"
            "4. Based on all gathered information, save a thorough analysis\n"
            "5. Post a comment summarizing your findings and recommendations\n\n"
            "Do these steps in order. Do not skip steps."
        ),
        "expected_tools": {"get_issue_full", "search_comments", "search_events", "save_analysis", "post_comment"},
        "expected_targets": {"github_number": [42], "target_number": [42], "issue_number": [42]},
        "max_expected_calls": 10,
    },
]

# Long-chain scenario (W2) - tests coherence over 15+ tool calls
LONG_CHAIN_SCENARIOS = [
    {
        "name": "Deep investigation (15+ calls)",
        "system_prompt": (
            "You are a thorough issue investigator. You MUST execute ALL of the following steps IN ORDER.\n"
            "Do NOT skip any step. Do NOT combine steps. One tool call per step.\n\n"
            "CONTEXT:\n"
            "- Repo: test-owner/test-repo\n"
            "- Issue #42: 'OAuth login fails on Safari'\n"
            "- Possibly related issues: #10, #23, #35\n"
            "- Possibly related PR: #87\n\n"
            "STEPS (execute ALL, one at a time):\n"
            "1. get_issue_full for issue #42\n"
            "2. search_comments for issue #42 (no keyword filter)\n"
            "3. search_events for issue #42\n"
            "4. get_issue for issue #10\n"
            "5. get_issue for issue #23\n"
            "6. get_issue for issue #35\n"
            "7. search_comments with keyword 'OAuth'\n"
            "8. search_comments with keyword 'Safari'\n"
            "9. search_comments with keyword 'cookie'\n"
            "10. search_events with event_type 'pull_request'\n"
            "11. get_pr for PR #87\n"
            "12. search_comments for PR #87\n"
            "13. search_events for target_number 87\n"
            "14. save_analysis with ALL findings compiled\n"
            "15. post_comment with a comprehensive summary referencing all gathered data\n\n"
            "CRITICAL: You must make at least 15 tool calls. Each step = exactly one tool call.\n"
            "After ALL 15 steps, write a final summary mentioning specific data from steps 1-13."
        ),
        "expected_tools": {"get_issue_full", "get_issue", "search_comments", "search_events", "get_pr", "save_analysis", "post_comment"},
        "expected_targets": {},
        "max_expected_calls": 20,
    },
]


# -- Scoring ------------------------------------------------------

@dataclass
class ToolCall:
    name: str
    args: dict
    valid_name: bool = False
    valid_args: bool = False
    error: str = ""


@dataclass
class ScenarioResult:
    name: str
    model: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_text: str = ""
    total_calls: int = 0
    valid_calls: int = 0
    invalid_calls: int = 0
    hallucinated_tools: int = 0
    expected_tools_called: set = field(default_factory=set)
    expected_tools_missing: set = field(default_factory=set)
    duration_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""


def validate_tool_call(name: str, args_str: str) -> ToolCall:
    """Validate a single tool call against known schemas."""
    tc = ToolCall(name=name, args={})

    # Check tool name
    if name in TOOL_NAMES:
        tc.valid_name = True
    else:
        tc.error = f"Unknown tool: {name}"
        return tc

    # Check args parse as JSON
    try:
        tc.args = json.loads(args_str) if isinstance(args_str, str) else args_str
        tc.valid_args = True
    except (json.JSONDecodeError, TypeError) as e:
        tc.error = f"Invalid JSON args: {e}"
        tc.valid_args = False

    return tc


def get_mock_response(tool_name: str) -> str:
    """Return a mock response for a tool call."""
    resp = MOCK_RESPONSES.get(tool_name, {"status": "ok"})
    return json.dumps(resp)


# -- Runner -------------------------------------------------------


def run_scenario(client: OpenAI, model: str, scenario: dict) -> ScenarioResult:
    """Run a single scenario: send messages, handle tool calls, measure quality."""
    result = ScenarioResult(name=scenario["name"], model=model)
    start = time.time()

    messages = [
        {"role": "system", "content": scenario["system_prompt"]},
        {"role": "user", "content": "Begin your analysis now. Use the tools available to you."},
    ]

    max_rounds = 25
    round_num = 0

    try:
        while round_num < max_rounds:
            round_num += 1

            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                temperature=0.2,
            )

            choice = response.choices[0]
            usage = response.usage
            if usage:
                result.input_tokens += usage.prompt_tokens or 0
                result.output_tokens += usage.completion_tokens or 0

            # If the model returned text (no tool calls), we're done
            if choice.finish_reason == "stop" or not choice.message.tool_calls:
                result.final_text = choice.message.content or ""
                break

            # Process tool calls
            assistant_msg = choice.message.model_dump()
            messages.append(assistant_msg)

            for tc in choice.message.tool_calls:
                call = validate_tool_call(tc.function.name, tc.function.arguments)
                result.tool_calls.append(call)
                result.total_calls += 1

                if call.valid_name and call.valid_args:
                    result.valid_calls += 1
                    result.expected_tools_called.add(call.name)
                elif not call.valid_name:
                    result.hallucinated_tools += 1
                    result.invalid_calls += 1
                else:
                    result.invalid_calls += 1

                # Send mock response back
                mock_resp = get_mock_response(tc.function.name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": mock_resp,
                })

    except Exception as e:
        result.error = str(e)

    result.duration_s = time.time() - start

    # Calculate missing expected tools
    expected = scenario.get("expected_tools", set())
    result.expected_tools_missing = expected - result.expected_tools_called

    return result


# -- Reporting ----------------------------------------------------


def print_result(r: ScenarioResult, scenario: dict):
    """Print detailed result for one scenario."""
    validity_pct = (r.valid_calls / r.total_calls * 100) if r.total_calls > 0 else 0
    expected_pct = (len(r.expected_tools_called & scenario.get("expected_tools", set())) /
                    len(scenario.get("expected_tools", {"_"})) * 100)

    print(f"\n  {'-' * 60}")
    print(f"  Scenario: {r.name}")
    print(f"  Model:    {r.model}")
    print(f"  {'-' * 60}")

    if r.error:
        print(f"  ERROR: {r.error}")
        return

    print(f"  Tool calls:        {r.total_calls} total, {r.valid_calls} valid, {r.invalid_calls} invalid")
    print(f"  Tool validity:     {validity_pct:.0f}%")
    print(f"  Hallucinated:      {r.hallucinated_tools}")
    print(f"  Expected coverage: {expected_pct:.0f}% ({r.expected_tools_called & scenario.get('expected_tools', set())} of {scenario.get('expected_tools', set())})")

    if r.expected_tools_missing:
        print(f"  Missing tools:     {r.expected_tools_missing}")

    max_calls = scenario.get("max_expected_calls", 10)
    efficiency = "good" if r.total_calls <= max_calls else f"verbose ({r.total_calls} > {max_calls} expected)"
    print(f"  Loop efficiency:   {r.total_calls} calls ({efficiency})")
    print(f"  Duration:          {r.duration_s:.1f}s")
    print(f"  Tokens:            {r.input_tokens} in, {r.output_tokens} out")

    if r.final_text:
        preview = r.final_text[:300].replace("\n", " ")
        print(f"  Final output:      {preview}...")

    # Show each tool call
    print(f"\n  Tool call log:")
    for i, tc in enumerate(r.tool_calls, 1):
        status = "OK" if tc.valid_name and tc.valid_args else f"FAIL: {tc.error}"
        args_preview = json.dumps(tc.args)[:80] if tc.args else "{}"
        print(f"    {i:2d}. {tc.name}({args_preview}) -> {status}")


def print_comparison(results_a: list[ScenarioResult], results_b: list[ScenarioResult]):
    """Print side-by-side comparison of two models."""
    model_a = results_a[0].model if results_a else "?"
    model_b = results_b[0].model if results_b else "?"

    print(f"\n{'=' * 70}")
    print(f"COMPARISON: {model_a} vs {model_b}")
    print(f"{'=' * 70}")

    header = f"{'Metric':<30} {model_a[:25]:<28} {model_b[:25]:<28}"
    print(header)
    print("-" * 70)

    # Aggregate metrics
    for results, label in [(results_a, model_a), (results_b, model_b)]:
        total_tc = sum(r.total_calls for r in results)
        valid_tc = sum(r.valid_calls for r in results)
        halluc = sum(r.hallucinated_tools for r in results)
        total_dur = sum(r.duration_s for r in results)
        total_in = sum(r.input_tokens for r in results)
        total_out = sum(r.output_tokens for r in results)
        if label == model_a:
            a_vals = (total_tc, valid_tc, halluc, total_dur, total_in, total_out)
        else:
            b_vals = (total_tc, valid_tc, halluc, total_dur, total_in, total_out)

    def row(metric, a_val, b_val, fmt="{}", better="lower"):
        a_str = fmt.format(a_val)
        b_str = fmt.format(b_val)
        if better == "higher":
            winner = "<<" if a_val > b_val else (">>" if b_val > a_val else "==")
        else:
            winner = "<<" if a_val < b_val else (">>" if b_val < a_val else "==")
        print(f"  {metric:<28} {a_str:<26} {winner:^4} {b_str:<26}")

    a_validity = (a_vals[1] / a_vals[0] * 100) if a_vals[0] else 0
    b_validity = (b_vals[1] / b_vals[0] * 100) if b_vals[0] else 0

    row("Total tool calls", a_vals[0], b_vals[0])
    row("Valid tool calls", a_vals[1], b_vals[1], "{}", "higher")
    row("Tool validity %", f"{a_validity:.0f}%", f"{b_validity:.0f}%")
    row("Hallucinated tools", a_vals[2], b_vals[2])
    row("Total duration", f"{a_vals[3]:.1f}s", f"{b_vals[3]:.1f}s")
    row("Input tokens", a_vals[4], b_vals[4])
    row("Output tokens", a_vals[5], b_vals[5])

    # Cost estimate
    # Pricing: lookup by model prefix
    pricing = {
        "moonshotai": (0.3827, 1.72),
        "anthropic/claude-sonnet": (3.0, 15.0),
        "anthropic/claude-haiku": (0.80, 4.0),
        "anthropic/claude-opus": (15.0, 75.0),
    }

    def estimate_cost(model, in_tok, out_tok):
        for prefix, (in_price, out_price) in pricing.items():
            if prefix in model:
                return in_tok / 1_000_000 * in_price + out_tok / 1_000_000 * out_price
        return 0.0

    a_cost = estimate_cost(model_a, a_vals[4], a_vals[5])
    b_cost = estimate_cost(model_b, b_vals[4], b_vals[5])
    row("Estimated cost", f"${a_cost:.4f}", f"${b_cost:.4f}")

    print()


# -- Main ---------------------------------------------------------


def run_all(model: str, scenarios: list | None = None) -> list[ScenarioResult]:
    """Run scenarios against one model."""
    scenario_list = scenarios or SCENARIOS
    client = OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.effective_api_key,
    )

    print(f"\n{'=' * 60}")
    print(f"Running bake-off: {model}")
    print(f"Base URL: {settings.llm_base_url}")
    print(f"Scenarios: {len(scenario_list)}")
    print(f"{'=' * 60}")

    results = []
    for scenario in scenario_list:
        print(f"\n  Running: {scenario['name']}...", end="", flush=True)
        result = run_scenario(client, model, scenario)
        results.append(result)
        print(f" done ({result.total_calls} calls, {result.duration_s:.1f}s)")
        print_result(result, scenario)

    return results


def save_results(results: list[ScenarioResult], model: str):
    """Save results to a JSON file for later comparison."""
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)

    safe_name = model.replace("/", "_").replace(":", "_")
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = results_dir / f"{safe_name}_{ts}.json"

    data = []
    for r in results:
        data.append({
            "name": r.name,
            "model": r.model,
            "total_calls": r.total_calls,
            "valid_calls": r.valid_calls,
            "invalid_calls": r.invalid_calls,
            "hallucinated_tools": r.hallucinated_tools,
            "expected_tools_called": list(r.expected_tools_called),
            "expected_tools_missing": list(r.expected_tools_missing),
            "duration_s": r.duration_s,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "error": r.error,
            "final_text_preview": r.final_text[:500] if r.final_text else "",
        })

    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"\n  Results saved: {path}")
    return path


# ── Default model list for full bake-off ─────────────────────────

BAKEOFF_MODELS = [
    "moonshotai/kimi-k2.5",
    "anthropic/claude-sonnet-4",
    "anthropic/claude-haiku-4.5",
    "deepseek/deepseek-chat",
    "qwen/qwen-2.5-coder-32b-instruct",
    "meta-llama/llama-3.3-70b-instruct",
]


def main():
    parser = argparse.ArgumentParser(description="LLM Bake-off for GITA agents")
    parser.add_argument("--model", type=str, default=None, help="Test one specific model")
    parser.add_argument("--compare", type=str, nargs=2, metavar=("MODEL_A", "MODEL_B"),
                        help="Compare two models side by side")
    parser.add_argument("--all", action="store_true",
                        help="Run all 6 candidate models (the full bake-off)")
    parser.add_argument("--long", action="store_true",
                        help="Run the long-chain (15+ calls) test across all models")
    parser.add_argument("--save", action="store_true", default=True,
                        help="Save results to tests/bakeoff/results/ (default: true)")
    args = parser.parse_args()

    if args.long:
        # Skip Qwen (failed on short test) — run the 5 working models
        long_models = [m for m in BAKEOFF_MODELS if "qwen" not in m]
        print(f"\n{'#' * 60}")
        print(f"LONG-CHAIN TEST: {len(long_models)} models x 1 scenario (15+ calls)")
        print(f"{'#' * 60}")

        all_model_results = {}
        for model in long_models:
            try:
                results = run_all(model, LONG_CHAIN_SCENARIOS)
                all_model_results[model] = results
                if args.save:
                    save_results(results, model + "_long")
            except Exception as e:
                print(f"\n  FAILED: {model} - {e}")

        # Master summary for long-chain
        print(f"\n{'#' * 70}")
        print(f"LONG-CHAIN MASTER SUMMARY (15+ tool calls)")
        print(f"{'#' * 70}")
        header = f"  {'Model':<40} {'Valid%':>7} {'Calls':>6} {'Hallu':>6} {'Time':>8} {'In tok':>8} {'Out tok':>8}"
        print(header)
        print(f"  {'-' * 76}")
        for model, results in all_model_results.items():
            total = sum(r.total_calls for r in results)
            valid = sum(r.valid_calls for r in results)
            halluc = sum(r.hallucinated_tools for r in results)
            dur = sum(r.duration_s for r in results)
            in_tok = sum(r.input_tokens for r in results)
            out_tok = sum(r.output_tokens for r in results)
            pct = (valid / total * 100) if total > 0 else 0
            print(f"  {model:<40} {pct:>6.0f}% {total:>6} {halluc:>6} {dur:>7.1f}s {in_tok:>8} {out_tok:>8}")
        return

    elif args.all:
        print(f"\n{'#' * 60}")
        print(f"FULL BAKE-OFF: {len(BAKEOFF_MODELS)} models x {len(SCENARIOS)} scenarios")
        print(f"{'#' * 60}")

        all_model_results = {}
        for model in BAKEOFF_MODELS:
            try:
                results = run_all(model)
                all_model_results[model] = results
                if args.save:
                    save_results(results, model)
            except Exception as e:
                print(f"\n  FAILED: {model} - {e}")

        # Print all pairwise comparisons against baseline
        if "anthropic/claude-sonnet-4" in all_model_results:
            baseline = all_model_results["anthropic/claude-sonnet-4"]
            for model, results in all_model_results.items():
                if model != "anthropic/claude-sonnet-4":
                    print_comparison(baseline, results)

        # Print the master summary
        print(f"\n{'#' * 70}")
        print(f"MASTER SUMMARY")
        print(f"{'#' * 70}")
        header = f"  {'Model':<40} {'Valid%':>7} {'Calls':>6} {'Time':>7} {'In tok':>8} {'Out tok':>8}"
        print(header)
        print(f"  {'-' * 68}")
        for model, results in all_model_results.items():
            total = sum(r.total_calls for r in results)
            valid = sum(r.valid_calls for r in results)
            dur = sum(r.duration_s for r in results)
            in_tok = sum(r.input_tokens for r in results)
            out_tok = sum(r.output_tokens for r in results)
            pct = (valid / total * 100) if total > 0 else 0
            print(f"  {model:<40} {pct:>6.0f}% {total:>6} {dur:>6.1f}s {in_tok:>8} {out_tok:>8}")

    elif args.compare:
        model_a, model_b = args.compare
        results_a = run_all(model_a)
        results_b = run_all(model_b)
        if args.save:
            save_results(results_a, model_a)
            save_results(results_b, model_b)
        print_comparison(results_a, results_b)
    else:
        model = args.model or settings.ai_default_model
        results = run_all(model)
        if args.save:
            save_results(results, model)


if __name__ == "__main__":
    main()
