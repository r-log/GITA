"""Test-generator agent — Week 8.

GITA's first code-writing agent. Inputs a module that lacks a test
file; outputs three chained Decisions (``create_branch`` →
``update_file`` → ``open_pr``) that, under ``WriteMode.FULL``, land a
branch + generated pytest file + open PR on the target repo.

Day 3 ships the bridge (pure Decision-builder, no LLM, no GitHub).
Day 4 ships the recipe that produces the test content via LLM +
AST + subprocess verification.
"""
