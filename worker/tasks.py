from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from worker.celery_app import celery_app
from config.settings import get_settings
from config.level_specs import get_level_spec
from utils.logger import get_logger, configure_logging

from storage.client import fetch_zip_bytes, upload_extracted_files, upload_json

from checks.zip_extractor            import ZipExtractorCheck
from checks.required_files_validator import RequiredFilesValidator
from checks.env_structure_validator  import EnvStructureValidator
from checks.secret_scanner           import SecretScannerCheck
from checks.level1_sandbox_executor  import Level1SandboxExecutor
from checks.harness_evaluator        import HarnessEvaluatorCheck
from checks.advisory_checks         import AdvisoryChecksBundle
from checks.base                     import CheckResult, CheckStatus

configure_logging()
log = get_logger(__name__)


# ── Main Celery task ─────────────────────────────────────────────────────────

@celery_app.task(
    name="worker.tasks.run_tier1_checks",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def run_tier1_checks(self, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Level-aware Tier 1 orchestrator.

    Expected payload keys:
      submission_id     : str
      assessment_id     : str
      level             : int
      attempt_number    : int
      zip_storage_key   : str   ← object-store key of uploaded ZIP
      agent_type        : str   ← "standard" | "web_search" (default: "standard")
      rubric_version    : str
    """
    submission_id  = payload["submission_id"]
    assessment_id  = payload["assessment_id"]
    attempt_number = int(payload.get("attempt_number", 1))
    level          = int(payload.get("level", 1))

    log.info("tier1_started", submission_id=submission_id,
             assessment_id=assessment_id, level=level)

    try:
        spec = get_level_spec(level)
    except ValueError as exc:
        return _reject(submission_id, assessment_id, attempt_number, [], str(exc))

    if spec["submission_type"] == "zip_only":
        return _run_zip_only(payload, spec)

    return _reject(
        submission_id, assessment_id, attempt_number, [],
        f"Submission type '{spec['submission_type']}' not yet implemented",
    )


# ── Level 1 pipeline ─────────────────────────────────────────────────────────

def _run_zip_only(payload: dict[str, Any], spec: dict) -> dict[str, Any]:
    """
    Full Tier 1 pipeline for Level 1 ZIP-only submissions.

    Steps:
      1.  Fetch ZIP bytes
      2.  ZIP extraction + security
      3.  Required files check
      4.  .env structure validation
      5.  Secret scanning
      6.  agent.py syntax check
      7.  Sandbox execution (keyword matching)
      8.  Harness pass-rate gate
      9.  Advisory checks
      10. Upload extracted files
      11. Upload sandbox output
      12. Assemble tier1_results
      13. Write to DB
      14. Trigger Tier 2
    """
    submission_id  = payload["submission_id"]
    assessment_id  = payload["assessment_id"]
    attempt_number = int(payload.get("attempt_number", 1))
    zip_key        = payload["zip_storage_key"]
    level          = int(payload.get("level", 1))
    agent_type     = payload.get("agent_type", "standard")
    check_results: list[CheckResult] = []
    sandbox_output_url = ""

    # ── 1. Fetch ZIP ─────────────────────────────────────────────────
    log.info("fetching_zip", submission_id=submission_id, key=zip_key)
    try:
        zip_bytes = fetch_zip_bytes(zip_key)
    except Exception as exc:
        return _reject(submission_id, assessment_id, attempt_number,
                       check_results, f"Could not fetch ZIP: {exc}")

    # ── 2. ZIP extraction ────────────────────────────────────────────
    log.info("check_start", check="zip_extraction")
    zip_result = ZipExtractorCheck().execute(zip_bytes=zip_bytes)
    check_results.append(zip_result)
    if zip_result.failed:
        return _reject(submission_id, assessment_id, attempt_number,
                       check_results, zip_result.detail)

    extracted: dict[str, str] = zip_result.metadata.get("extracted_contents", {})

    # ── 3. Required files ────────────────────────────────────────────
    log.info("check_start", check="required_files_present")
    files_result = RequiredFilesValidator().execute(
        extracted_contents=extracted,
        required_files=spec["required_files"],
    )
    check_results.append(files_result)
    if files_result.failed:
        return _reject(submission_id, assessment_id, attempt_number,
                       check_results, files_result.detail)

    # ── 4. .env structure ────────────────────────────────────────────
    log.info("check_start", check="env_structure")
    env_result = EnvStructureValidator().execute(
        env_content=extracted.get(".env", "")
    )
    check_results.append(env_result)
    if env_result.failed:
        return _reject(submission_id, assessment_id, attempt_number,
                       check_results, env_result.detail)

    # ── 5. Secret scan ───────────────────────────────────────────────
    settings = get_settings()
    if settings.secret_scan_enabled:
        log.info("check_start", check="secret_scan")
        text_files = {
            f: c for f, c in extracted.items()
            if not c.startswith("[binary:")
        }
        secret_result = SecretScannerCheck().execute(file_contents=text_files)
        check_results.append(secret_result)
        if secret_result.failed:
            log.warning("security_flag", submission_id=submission_id,
                        findings=secret_result.metadata.get("finding_count", 0))
            return _reject(submission_id, assessment_id, attempt_number,
                           check_results, secret_result.detail)

    # ── 6. Syntax check ──────────────────────────────────────────────
    log.info("check_start", check="agent_syntax")
    syntax_result = _check_python_syntax(extracted.get("agent.py", ""))
    check_results.append(syntax_result)
    if syntax_result.failed:
        return _reject(submission_id, assessment_id, attempt_number,
                       check_results, syntax_result.detail)

    # ── 7. Sandbox execution ─────────────────────────────────────────
    # Select test cases based on agent_type declared in the payload
    sandbox_result = None
    if spec.get("sandbox_enabled", False):
        test_cases = _select_test_cases(spec, agent_type)
        log.info("check_start", check="sandbox_execution",
                 agent_type=agent_type, test_count=len(test_cases))

        sandbox_result = Level1SandboxExecutor().execute(
            extracted_contents=extracted,
            test_cases=test_cases,
        )
        check_results.append(sandbox_result)

        if sandbox_result.status == CheckStatus.ERROR:
            # Docker issues → advisory, do not hard-reject
            log.warning("sandbox_error", submission_id=submission_id,
                        detail=sandbox_result.detail)
        elif sandbox_result.failed:
            return _reject(submission_id, assessment_id, attempt_number,
                           check_results, sandbox_result.detail)

        # ── 8. Harness pass-rate gate ─────────────────────────────────
        if sandbox_result.status not in (CheckStatus.ERROR, CheckStatus.SKIPPED):
            log.info("check_start", check="harness_pass_rate")
            harness_result = HarnessEvaluatorCheck().execute(
                sandbox_metadata=sandbox_result.metadata,
                min_pass_rate=spec.get("min_pass_rate", 0.6),
            )
            check_results.append(harness_result)
            if harness_result.failed:
                return _reject(submission_id, assessment_id, attempt_number,
                               check_results, harness_result.detail)

            # Upload sandbox output JSON
            if sandbox_result.metadata:
                key = f"sandbox_outputs/{submission_id}/output.json"
                sandbox_output_url = upload_json(
                    key, json.dumps(sandbox_result.metadata, indent=2)
                )
                log.info("sandbox_output_uploaded", url=sandbox_output_url)

    # ── 9. Advisory checks ────────────────────────────────────────────
    log.info("check_start", check="advisory_bundle")
    advisory = AdvisoryChecksBundle().execute(
        artifact_urls={f: f"extracted/{f}" for f in extracted},
        file_contents=extracted,
    )
    check_results.append(advisory)

    # ── 10. Upload extracted files ────────────────────────────────────
    log.info("uploading_extracted_files", submission_id=submission_id)
    artifact_urls = upload_extracted_files(
        submission_id=submission_id,
        extracted_contents=extracted,
    )

    # ── 11-12. Assemble tier1_results ─────────────────────────────────
    sandbox_summary = {}
    if sandbox_result and sandbox_result.metadata:
        m = sandbox_result.metadata
        sandbox_summary = {
            "tests_total":   m.get("tests_total", 0),
            "tests_passed":  m.get("tests_passed", 0),
            "pass_rate":     m.get("pass_rate", 0),
            "provider_used": m.get("provider_used", ""),
            "agent_type":    agent_type,
        }

    tier1_results = {
        "submission_id":       submission_id,
        "assessment_id":       assessment_id,
        "attempt_number":      attempt_number,
        "level":               level,
        "agent_type":          agent_type,
        "tier":                "tier1_automated_checks",
        "tier1_status":        "passed",
        "evaluated_at":        datetime.now(timezone.utc).isoformat(),
        "all_required_passed": True,
        "submission_type":     "zip_only",
        "extracted_files":     list(extracted.keys()),
        "sandbox_summary":     sandbox_summary,
        "artifact_urls":       artifact_urls,
        "sandbox_output_url":  sandbox_output_url,
        "checks":              [r.to_dict() for r in check_results],
    }

    # ── 13. Write to DB ───────────────────────────────────────────────
    _write_to_db(submission_id, tier1_results)

    # ── 14. Trigger Tier 2 ────────────────────────────────────────────
    _trigger_tier2({
        "submission_id":       submission_id,
        "assessment_id":       assessment_id,
        "attempt_number":      payload.get("attempt_number", 1),
        "rubric_version":      payload.get("rubric_version", "rubv_001"),
        "level":               level,
        "agent_type":          agent_type,
        "assessment_scenario": _build_tier2_scenario(spec, agent_type),
        "rubric_dimensions":   json.dumps(spec["rubric_dimensions"]),
        "pass_thresholds":     json.dumps(spec["pass_thresholds"]),
        "artifact_urls":       json.dumps(artifact_urls),
        "tier1_results":       json.dumps(tier1_results),
        "sandbox_output_url":  sandbox_output_url,
    })

    log.info("tier1_passed", submission_id=submission_id, level=level,
             agent_type=agent_type)
    return tier1_results


# ── Helpers ──────────────────────────────────────────────────────────────────

def _select_test_cases(spec: dict, agent_type: str) -> list[dict]:
    """
    Return the correct test case list based on agent_type.
    "web_search" → test_cases_web_search
    "standard"   → test_cases_standard (default)
    """
    if agent_type == "web_search":
        return spec.get("test_cases_web_search", spec.get("test_cases_standard", []))
    return spec.get("test_cases_standard", [])


def _build_tier2_scenario(spec: dict, agent_type: str) -> str:
    """
    Build the assessment scenario string for the Tier 2 judge.
    Appends the web-search addendum if agent_type is web_search.
    """
    base = spec.get("assessment_scenario", "")
    if agent_type == "web_search":
        addendum = spec.get("assessment_scenario_web_search_addendum", "")
        if addendum:
            return f"{base}\n\n{addendum}"
    return base


def _check_python_syntax(code: str) -> CheckResult:
    if not code or not code.strip():
        return CheckResult(check_id="agent_syntax", status=CheckStatus.FAILED,
                           detail="agent.py is empty", blocking=True)
    try:
        compile(code, filename="agent.py", mode="exec")
        return CheckResult(check_id="agent_syntax", status=CheckStatus.PASSED,
                           detail="agent.py is syntactically valid Python",
                           blocking=True)
    except SyntaxError as exc:
        return CheckResult(check_id="agent_syntax", status=CheckStatus.FAILED,
                           detail=f"agent.py syntax error on line {exc.lineno}: {exc.msg}",
                           blocking=True,
                           metadata={"line": exc.lineno, "error": exc.msg})


def _reject(
    submission_id: str, assessment_id: str, attempt_number: int,
    check_results: list[CheckResult], reason: str,
) -> dict[str, Any]:
    payload = {
        "submission_id":       submission_id,
        "assessment_id":       assessment_id,
        "attempt_number":      attempt_number,
        "tier":                "tier1_automated_checks",
        "tier1_status":        "rejected",
        "evaluated_at":        datetime.now(timezone.utc).isoformat(),
        "all_required_passed": False,
        "rejection_reason":    reason,
        "checks":              [r.to_dict() for r in check_results],
        "next_action":         "notify_learner",
    }
    try:
        _write_to_db(submission_id, payload)
    except Exception:
        pass
    log.info("tier1_rejected", submission_id=submission_id, reason=reason)
    return payload


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _write_to_db(submission_id: str, payload: dict[str, Any]) -> None:
    settings = get_settings()
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            f"{settings.assessment_api_base_url}"
            f"/submissions/{submission_id}/tier1_results",
            json=payload,
            headers={"Authorization": f"Bearer {settings.assessment_service_token}"},
        )
        resp.raise_for_status()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _trigger_tier2(payload: dict[str, Any]) -> None:
    settings = get_settings()
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            settings.tier2_webhook_url,
            json={"inputs": payload, "response_mode": "async"},
            headers={"Authorization": f"Bearer {settings.tier2_dify_token}",
                     "Content-Type":  "application/json"},
        )
        resp.raise_for_status()
    log.info("tier2_triggered", submission_id=payload.get("submission_id"))