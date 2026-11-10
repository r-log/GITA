"""Deterministic ARQ job IDs, shared by the webhook layer and the runners.

ARQ refuses to enqueue a job whose ID is queued, running, or whose result
is still stored (``keep_result``). An ID must therefore name exactly one
unit of work: too coarse and legitimate work is dropped; too fine and
redeliveries run twice. See ``gita.web.dispatch`` for the full rationale.
"""
from __future__ import annotations


def review_job_id(repo_full_name: str, pr_number: int, head_sha: str | None) -> str:
    """One review of one commit of one PR."""
    return f"review-pr:{repo_full_name.strip().lower()}:{pr_number}:{head_sha or 'head'}"


def reindex_job_id(repo_full_name: str, after_sha: str) -> str:
    """One re-index per default-branch commit."""
    return f"reindex:{repo_full_name.strip().lower()}:{after_sha}"


def onboarding_job_id(repo_full_name: str, issue_number: int) -> str:
    """One onboarding run per issue."""
    return f"onboard:{repo_full_name.strip().lower()}:{issue_number}"
