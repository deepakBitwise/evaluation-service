from __future__ import annotations
import copy
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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


# ── Event status helper ──────────────────────────────────────────────────────

def _send_status_event(submission_id: str, event_type: str, value: str) -> None:
    settings = get_settings()
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{settings.assessment_api_base_url}/api/v1/submission/{submission_id}/events",
                json={"type": event_type, "value": value},
                headers={
                    "Authorization": f"Bearer {settings.assessment_service_token}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            log.info("status_event_sent", submission_id=submission_id,
                     event_type=event_type, value=value)
    except Exception as exc:
        log.warning("status_event_failed", submission_id=submission_id,
                    event_type=event_type, error=str(exc))


# ── Main Celery task ─────────────────────────────────────────────────────────

@celery_app.task(
    name="worker.tasks.run_tier1_checks",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def run_tier1_checks(self, payload: dict[str, Any]) -> dict[str, Any]:
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
    sandbox_result = None
    if spec.get("sandbox_enabled", False):
        log.info("check_start", check="sandbox_execution",
                 agent_type=agent_type,
                 standard_tests=len(spec.get("test_cases_standard", [])),
                 web_search_tests=len(spec.get("test_cases_web_search", [])))

        sandbox_result = Level1SandboxExecutor().execute(
            extracted_contents=extracted,
            test_cases_standard=spec.get("test_cases_standard", []),
            test_cases_web_search=spec.get("test_cases_web_search", []),
            agent_type=agent_type,
            suite_weights=spec.get("sandbox_suite_weights",
                                   {"standard": 0.40, "web_search": 0.60}),
        )
        check_results.append(sandbox_result)

        if sandbox_result.status == CheckStatus.ERROR:
            log.warning("sandbox_error", submission_id=submission_id,
                        detail=sandbox_result.detail)
            _send_status_event(submission_id, "WARNING", "Code quality is weak")
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
    #
    # Three transformations before sending to DIFY:
    #
    # A) _slim_tier1_for_dify  — strips extracted_contents from check
    #    metadata (15-20K chars of embedded file text). Already accessible
    #    via artifact_urls — no information loss.
    #
    # B) _fix_urls_for_dify    — rewrites minio:9000 → host.docker.internal:9000
    #    so DIFY (on Windows host) can reach MinIO. NOTE: DIFY's ssrf_proxy
    #    container may still block host.docker.internal — see note below.
    #
    # C) judge_model + judge_temperature forwarded from payload so the DIFY
    #    Start node receives the LLM selection for the judge runs.
    #
    tier1_results_slim = _slim_tier1_for_dify(tier1_results)
    dify_artifact_urls = _fix_urls_for_dify(artifact_urls)
    dify_sandbox_url   = _fix_url_for_dify(sandbox_output_url)

    _trigger_tier2({
        "submission_id":       submission_id,
        "assessment_id":       assessment_id,
        "attempt_number":      payload.get("attempt_number", 1),
        "rubric_version":      payload.get("rubric_version", "rubv_001"),
        "level":               level,
        "assessment_scenario": _build_tier2_scenario(spec, agent_type),
        "rubric_dimensions":   json.dumps(spec["rubric_dimensions"]),
        "pass_thresholds":     json.dumps(spec["pass_thresholds"]),
        "artifact_urls":       json.dumps(dify_artifact_urls),
        "tier1_results":       json.dumps(tier1_results_slim),
        "sandbox_output_url":  dify_sandbox_url,
    })

    log.info("tier1_passed", submission_id=submission_id, level=level,
             agent_type=agent_type)
    _send_status_event(submission_id, "SUCCESS", "Automated checks passed")
    return tier1_results


# ── General helpers ───────────────────────────────────────────────────────────

def _build_tier2_scenario(spec: dict, agent_type: str) -> str:
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
    _send_status_event(submission_id, "FAILURE", "Automated checks Failed")
    log.info("tier1_rejected", submission_id=submission_id, reason=reason)
    return payload


# ── DIFY payload helpers ──────────────────────────────────────────────────────

def _slim_tier1_for_dify(tier1_results: dict[str, Any]) -> dict[str, Any]:
    """
    Strip extracted_contents from check metadata before sending to DIFY.
    Reduces tier1_results from ~18K chars to ~2K chars.
    File contents are accessible via artifact_urls — no information loss.
    """
    slimmed = copy.deepcopy(tier1_results)
    for check in slimmed.get("checks", []):
        check.get("metadata", {}).pop("extracted_contents", None)
    return slimmed


def _fix_urls_for_dify(artifact_urls: dict[str, str]) -> dict[str, str]:
    """
    Rewrite Docker-internal hostnames to host.docker.internal in all URLs.

    IMPORTANT: DIFY runs docker-ssrf_proxy which may still block
    host.docker.internal (resolves to a private IP). If Node 3 fails
    with a connection error, check docker/docker-compose.yaml for
    SSRF_PROXY_ALLOWED_HOSTS or configure MinIO with a public URL.
    """
    return {role: _fix_url_for_dify(url) for role, url in artifact_urls.items()}


def _fix_url_for_dify(url: str) -> str:
    """Replace minio:9000 with host.docker.internal:9000 in a single URL."""
    if not url:
        return url
    return (
        url
        .replace("http://minio:9000",  "http://host.docker.internal:9000")
        .replace("https://minio:9000", "http://host.docker.internal:9000")
    )


# ── DB + DIFY network helpers ─────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _write_to_db(submission_id: str, payload: dict[str, Any]) -> None:
    settings = get_settings()
    result_paths = (
        f"/api/v1/submissions/{submission_id}/tier1_results",
        f"/submissions/{submission_id}/tier1_results",
    )
    try:
        with httpx.Client(timeout=15) as client:
            for path in result_paths:
                resp = client.post(
                    f"{settings.assessment_api_base_url}{path}",
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.assessment_service_token}"},
                )
                if resp.status_code == 404:
                    log.warning("tier1_results_route_not_found",
                                submission_id=submission_id, path=path)
                    continue
                resp.raise_for_status()
                return

            status = _tier1_status_for_assessment_api(payload)
            if status:
                resp = client.patch(
                    f"{settings.assessment_api_base_url}/api/v1/submissions/{submission_id}/status",
                    json={"automated_check": status},
                    headers={"Authorization": f"Bearer {settings.assessment_service_token}"},
                )
                resp.raise_for_status()
                log.info("submission_status_updated",
                         submission_id=submission_id, automated_check=status)
                return

            log.warning("tier1_results_not_persisted",
                        submission_id=submission_id,
                        reason="no supported status mapping")
    except Exception as exc:
        log.warning("tier1_results_not_persisted",
                    submission_id=submission_id, error=str(exc))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _trigger_tier2(payload: dict[str, Any]) -> None:
    settings = get_settings()
    timeout  = httpx.Timeout(connect=10, read=300, write=30, pool=30)

    print("\n" + "=" * 20 + " COPY THIS FOR DIFY INPUTS " + "=" * 20)
    print(json.dumps(payload, indent=2))
    print("=" * 67 + "\n")

    request_payload = {
        "inputs":        payload,
        "response_mode": "streaming",
        "user":          f"tier1-worker-{payload.get('submission_id', 'unknown')}",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST",
                settings.tier2_webhook_url,
                json=request_payload,
                headers={
                    "Authorization": f"Bearer {settings.tier2_dify_token}",
                    "Content-Type":  "application/json",
                },
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.strip():
                        log.info("tier2_first_event", event_line=line[:200])
                        break

        log.info(
            "tier2_triggered",
            submission_id=payload.get("submission_id"),
            dify_url=settings.tier2_webhook_url,
            response_mode="streaming",
        )
        _log_latest_tier2_workflow_run(settings)

    except httpx.ReadTimeout as exc:
        log.warning(
            "tier2_trigger_failed",
            submission_id=payload.get("submission_id"),
            dify_url=settings.tier2_webhook_url,
            error=str(exc),
            note="DIFY may still be running — check DIFY Logs tab",
        )
    except httpx.HTTPStatusError as exc:
        log.warning(
            "tier2_trigger_failed",
            submission_id=payload.get("submission_id"),
            dify_url=settings.tier2_webhook_url,
            status_code=exc.response.status_code,
            response_body=exc.response.text[:500],
            error=str(exc),
        )
    except Exception as exc:
        log.warning(
            "tier2_trigger_failed",
            submission_id=payload.get("submission_id"),
            dify_url=settings.tier2_webhook_url,
            error=str(exc),
        )


def _log_latest_tier2_workflow_run(settings: Any) -> None:
    logs_url = _dify_workflow_logs_url(settings.tier2_webhook_url)
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(
                logs_url,
                headers={"Authorization": f"Bearer {settings.tier2_dify_token}"},
                params={"page": 1, "limit": 1},
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        log.warning("tier2_logs_fetch_failed",
                    dify_logs_url=logs_url, error=str(exc))
        return

    latest       = (body.get("data") or [None])[0]
    workflow_run = (latest or {}).get("workflow_run") or {}
    log.info(
        "tier2_latest_workflow_log",
        dify_logs_url=logs_url,
        total=body.get("total"),
        workflow_run_id=workflow_run.get("id"),
        status=workflow_run.get("status"),
        error=workflow_run.get("error"),
        elapsed_time=workflow_run.get("elapsed_time"),
        total_steps=workflow_run.get("total_steps"),
    )


def _dify_workflow_logs_url(webhook_url: str) -> str:
    parsed = urlsplit(webhook_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/v1/workflows/logs", "", ""))


def _tier1_status_for_assessment_api(payload: dict[str, Any]) -> str | None:
    status = payload.get("tier1_status")
    if status == "passed":
        return "PASSED"
    if status == "rejected":
        return "REJECTED"
    return None