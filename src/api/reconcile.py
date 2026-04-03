"""
Manual reconciliation trigger endpoint.

POST /api/reconcile              — reconcile all repos
POST /api/reconcile              — reconcile specific repo (body: {"repo_full_name": "owner/repo"})
"""

import json
import structlog
from fastapi import APIRouter, Request

log = structlog.get_logger()

router = APIRouter()


@router.post("/api/reconcile")
async def trigger_reconciliation(request: Request):
    """
    Manually trigger reconciliation.
    Optional JSON body: {"repo_full_name": "owner/repo"} to reconcile a single repo.
    Without body, reconciles all tracked repos.
    """
    from src.workers.reconciliation import reconcile_all_repos, reconcile_single_repo

    body = {}
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return {"status": "error", "message": "Invalid JSON body"}

    repo_full_name = body.get("repo_full_name")

    try:
        if repo_full_name:
            log.info("reconcile_manual_trigger", repo=repo_full_name)
            result = await reconcile_single_repo(repo_full_name)
            return {"status": "ok", "results": [{"repo": repo_full_name, **result}]}
        else:
            log.info("reconcile_manual_trigger_all")
            results = await reconcile_all_repos()
            return {"status": "ok", "results": results}
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        log.error("reconcile_trigger_error", error=str(e))
        return {"status": "error", "message": f"Reconciliation failed: {e}"}
