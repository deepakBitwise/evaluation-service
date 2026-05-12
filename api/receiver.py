from __future__ import annotations
from typing import Any, Literal
import uuid
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel, Field

from worker.tasks import run_tier1_checks
from config.settings import get_settings
from utils.logger import get_logger, configure_logging

configure_logging()
log = get_logger(__name__)
app = FastAPI(title="Tier 1 Worker API", version="3.0.0")


class SubmissionJob(BaseModel):
    submission_id:     str
    assessment_id:     str
    level:             int   = Field(default=1, ge=1, le=7)
    attempt_number:    int   = Field(default=1, ge=1)
    zip_storage_key:   str
    rubric_version:    str   = "rubv_001"
    # tier2_webhook_url: str   = ""
    # "standard"  → conversational chatbot (no web search)
    # "web_search" → agent uses a web search tool for real-time info
    agent_type: Literal["standard", "web_search"] = "standard"


def verify_token(authorization: str = Header(...)) -> None:
    settings = get_settings()
    if authorization != f"Bearer {settings.assessment_service_token}":
        raise HTTPException(status_code=401, detail="Invalid or missing token")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/jobs/tier1", status_code=202, dependencies=[Depends(verify_token)])
def enqueue_tier1_job(job: SubmissionJob) -> dict[str, str]:
    """
    Enqueues a Level 1 submission for async Tier 1 evaluation.

    The learner uploads their ZIP directly to object storage via a
    presigned URL. The Assessment API calls this endpoint with the
    storage key of that ZIP and declares the agent_type.

    agent_type options:
      "standard"   — conversational chatbot, no real-time tools
      "web_search"  — agent uses web search for real-time information
    """
    job_id   = str(uuid.uuid4())
    settings = get_settings()
    payload  = job.model_dump()

    if not payload.get("tier2_webhook_url"):
        payload["tier2_webhook_url"] = settings.tier2_webhook_url

    log.info("job_received", submission_id=job.submission_id,
             level=job.level, agent_type=job.agent_type, job_id=job_id)

    run_tier1_checks.apply_async(args=[payload], task_id=job_id, queue="tier1")

    return {
        "job_id":        job_id,
        "submission_id": job.submission_id,
        "status":        "queued",
        "agent_type":    job.agent_type,
        "message":       f"Tier 1 evaluation started — Level {job.level} ({job.agent_type} agent)",
    }


@app.get("/jobs/{job_id}/status", dependencies=[Depends(verify_token)])
def get_job_status(job_id: str) -> dict[str, Any]:
    from worker.celery_app import celery_app
    result = celery_app.AsyncResult(job_id)
    return {
        "job_id": job_id,
        "state":  result.state,
        "result": result.result if result.ready() else None,
    }