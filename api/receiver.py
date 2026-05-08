from __future__ import annotations
from typing import Any
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel, Field
import uuid

from worker.tasks import run_tier1_checks
from config.settings import get_settings
from utils.logger import get_logger, configure_logging

configure_logging()
log = get_logger(__name__)
app = FastAPI(title="Tier 1 Worker API", version="1.0.0")


# ── Request schema ──────────────────────────────────────────────────

class SubmissionJob(BaseModel):
    submission_id:         str
    assessment_id:         str
    attempt_number:        int                  = Field(default=1, ge=1)
    artifact_urls:         dict[str, str]
    required_deliverables: list[str]
    min_harness_pass_rate: float                = Field(default=0.7, ge=0.0, le=1.0)
    test_cases:            list[dict[str, Any]] = Field(default_factory=list)
    entry_point_role:      str                  = "solution"
    tier2_webhook_url:     str                  = ""


# ── Auth dependency ─────────────────────────────────────────────────

def verify_token(authorization: str = Header(...)) -> None:
    settings = get_settings()
    expected = f"Bearer {settings.assessment_service_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# ── Routes ──────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/jobs/tier1", status_code=202)
def enqueue_tier1_job(job: SubmissionJob) -> dict[str, str]:
    """
    Receives a submission job from the Assessment API and enqueues
    it for async Tier 1 processing via Celery.

    Returns immediately with a job_id — the caller polls for results
    via the Assessment API's submission status endpoint.
    """
    job_id = str(uuid.uuid4())

    log.info(
        "job_received",
        submission_id=job.submission_id,
        assessment_id=job.assessment_id,
        job_id=job_id,
    )

    settings = get_settings()
    payload  = job.model_dump()

    if not payload.get("tier2_webhook_url"):
        payload["tier2_webhook_url"] = settings.tier2_webhook_url

    run_tier1_checks.apply_async(
        args=[payload],
        task_id=job_id,
        queue="tier1",
    )

    return {
        "job_id":        job_id,
        "submission_id": job.submission_id,
        "status":        "queued",
        "message":       "Tier 1 evaluation started",
    }


@app.get("/jobs/{job_id}/status")
def get_job_status(job_id: str) -> dict[str, Any]:
    """Check the Celery task status for a given job_id."""
    from worker.celery_app import celery_app
    result = celery_app.AsyncResult(job_id)
    return {
        "job_id": job_id,
        "state":  result.state,
        "result": result.result if result.ready() else None,
    }
