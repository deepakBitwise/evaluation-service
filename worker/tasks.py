from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from worker.celery_app import celery_app
from config.settings import get_settings
from utils.logger import get_logger, configure_logging

from storage.client import fetch_all_artifacts, upload_json

from checks.file_presence    import FilePresenceCheck
from checks.secret_scanner   import SecretScannerCheck
from checks.syntax_validator import SyntaxValidatorCheck
from checks.sandbox_executor import SandboxExecutorCheck
from checks.harness_evaluator import HarnessEvaluatorCheck
from checks.advisory_checks  import AdvisoryChecksBundle
from checks.base              import CheckResult, CheckStatus

configure_logging()
log = get_logger(__name__)


@celery_app.task(
    name="worker.tasks.run_tier1_checks",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def run_tier1_checks(self, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Main Tier 1 orchestrator task.

    Receives a job payload from the Assessment API containing:
      - submission_id
      - assessment_id
      - attempt_number
      - artifact_urls         (dict: role → pre-signed URL)
      - required_deliverables (list of role strings)
      - min_harness_pass_rate (float 0.0-1.0)
      - test_cases            (list of harness test case dicts)
      - tier2_webhook_url     (str)

    Runs checks in fail-fast sequence. Stops immediately on any
    required check failure and writes a rejection record.
    On full pass, uploads sandbox output and triggers Tier 2.
    """
    settings = get_settings()

    submission_id        = payload["submission_id"]
    assessment_id        = payload["assessment_id"]
    attempt_number       = int(payload.get("attempt_number", 1))
    artifact_urls        = payload["artifact_urls"]
    required_deliverables = payload["required_deliverables"]
    min_pass_rate        = float(payload.get("min_harness_pass_rate", 0.7))
    test_cases           = payload.get("test_cases", [])
    entry_point_role     = payload.get("entry_point_role", "solution")

    log.info(
        "tier1_started",
        submission_id=submission_id,
        assessment_id=assessment_id,
        attempt_number=attempt_number,
    )

    check_results: list[CheckResult] = []

    # ── Step 1: File Presence ───────────────────────────────────────
    log.info("check_start", check="file_presence")
    result = FilePresenceCheck().execute(
        artifact_urls=artifact_urls,
        required_deliverables=required_deliverables,
    )
    check_results.append(result)

    if result.failed:
        return _build_rejection(
            submission_id, assessment_id, attempt_number,
            check_results, "File presence check failed"
        )

    # ── Step 2: Fetch all file contents from storage ────────────────
    log.info("fetching_artifacts", submission_id=submission_id)
    file_contents = fetch_all_artifacts(artifact_urls)

    # ── Step 3: Secret Scanning ─────────────────────────────────────
    if settings.secret_scan_enabled:
        log.info("check_start", check="secret_scan")
        result = SecretScannerCheck().execute(file_contents=file_contents)
        check_results.append(result)

        if result.failed:
            log.warning(
                "security_flag",
                submission_id=submission_id,
                findings=result.metadata.get("finding_count", 0),
            )
            return _build_rejection(
                submission_id, assessment_id, attempt_number,
                check_results, "Secret detected — security review required"
            )

    # ── Step 4: Syntax Validation ───────────────────────────────────
    log.info("check_start", check="syntax_validation")
    result = SyntaxValidatorCheck().execute(
        file_contents=file_contents,
        artifact_urls=artifact_urls,
    )
    check_results.append(result)

    if result.failed:
        return _build_rejection(
            submission_id, assessment_id, attempt_number,
            check_results, "Syntax validation failed"
        )

    # ── Step 5: Sandbox Execution ───────────────────────────────────
    log.info("check_start", check="sandbox_execution")
    sandbox_result = SandboxExecutorCheck().execute(
        file_contents=file_contents,
        test_cases=test_cases,
        entry_point_role=entry_point_role,
    )
    check_results.append(sandbox_result)

    if sandbox_result.failed:
        return _build_rejection(
            submission_id, assessment_id, attempt_number,
            check_results, "Sandbox execution failed"
        )

    # ── Step 6: Harness Pass Rate ───────────────────────────────────
    log.info("check_start", check="harness_pass_rate")
    result = HarnessEvaluatorCheck().execute(
        sandbox_metadata=sandbox_result.metadata,
        min_pass_rate=min_pass_rate,
    )
    check_results.append(result)

    if result.failed:
        return _build_rejection(
            submission_id, assessment_id, attempt_number,
            check_results, "Harness pass rate below threshold"
        )

    # ── Step 7: Advisory Checks (non-blocking) ──────────────────────
    log.info("check_start", check="advisory_bundle")
    advisory_result = AdvisoryChecksBundle().execute(
        artifact_urls=artifact_urls,
        file_contents=file_contents,
    )
    check_results.append(advisory_result)

    # ── Step 8: Upload sandbox output to object storage ─────────────
    sandbox_output_key = f"sandbox_outputs/{submission_id}/output.json"
    sandbox_json       = json.dumps(sandbox_result.metadata, indent=2)
    sandbox_output_url = upload_json(sandbox_output_key, sandbox_json)
    log.info("sandbox_output_uploaded", url=sandbox_output_url)

    # ── Step 9: Assemble tier1_results ─────────────────────────────
    tier1_results = _assemble_results(
        submission_id=submission_id,
        assessment_id=assessment_id,
        attempt_number=attempt_number,
        check_results=check_results,
        sandbox_output_url=sandbox_output_url,
        pass_rate=result.metadata.get("pass_rate", 0),
        tests_passed=result.metadata.get("tests_passed", 0),
        tests_total=result.metadata.get("tests_total", 0),
    )

    # ── Step 10: Write to DB ────────────────────────────────────────
    _write_to_db(submission_id, tier1_results)

    # ── Step 11: Trigger Tier 2 ─────────────────────────────────────
    tier2_payload = {
        **payload,
        "tier1_results":      tier1_results,
        "sandbox_output_url": sandbox_output_url,
    }
    _trigger_tier2(tier2_payload)

    log.info(
        "tier1_passed",
        submission_id=submission_id,
        pass_rate=result.metadata.get("pass_rate"),
    )
    return tier1_results


def _build_rejection(
    submission_id: str,
    assessment_id: str,
    attempt_number: int,
    check_results: list[CheckResult],
    reason: str,
) -> dict[str, Any]:
    payload = {
        "submission_id":   submission_id,
        "assessment_id":   assessment_id,
        "attempt_number":  attempt_number,
        "tier":            "tier1_automated_checks",
        "tier1_status":    "rejected",
        "evaluated_at":    datetime.now(timezone.utc).isoformat(),
        "all_required_passed": False,
        "rejection_reason": reason,
        "checks":          [r.to_dict() for r in check_results],
        "next_action":     "notify_learner",
    }
    _write_to_db(submission_id, payload)
    log.info("tier1_rejected", submission_id=submission_id, reason=reason)
    return payload


def _assemble_results(
    submission_id: str,
    assessment_id: str,
    attempt_number: int,
    check_results: list[CheckResult],
    sandbox_output_url: str,
    pass_rate: float,
    tests_passed: int,
    tests_total: int,
) -> dict[str, Any]:
    return {
        "submission_id":       submission_id,
        "assessment_id":       assessment_id,
        "attempt_number":      attempt_number,
        "tier":                "tier1_automated_checks",
        "tier1_status":        "passed",
        "evaluated_at":        datetime.now(timezone.utc).isoformat(),
        "all_required_passed": True,
        "summary": {
            "harness_pass_rate": pass_rate,
            "tests_passed":      tests_passed,
            "tests_total":       tests_total,
        },
        "checks":             [r.to_dict() for r in check_results],
        "sandbox_output_url": sandbox_output_url,
    }


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _write_to_db(submission_id: str, payload: dict[str, Any]) -> None:
    settings = get_settings()
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{settings.assessment_api_base_url}/submissions/{submission_id}/tier1_results",
                json=payload,
                headers={"Authorization": f"Bearer {settings.assessment_service_token}"},
            )
            resp.raise_for_status()
    except Exception as exc:
        log.error("db_write_failed", submission_id=submission_id, error=str(exc))
        raise


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _trigger_tier2(payload: dict[str, Any]) -> None:
    settings = get_settings()
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                settings.tier2_webhook_url,
                json={"inputs": payload, "response_mode": "async"},
                headers={
                    "Authorization": f"Bearer {settings.tier2_dify_token}",
                    "Content-Type":  "application/json",
                },
            )
            resp.raise_for_status()
        log.info("tier2_triggered", submission_id=payload.get("submission_id"))
    except Exception as exc:
        log.error("tier2_trigger_failed", error=str(exc))
        raise
