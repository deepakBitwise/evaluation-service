from __future__ import annotations
from typing import Any
from checks.base import BaseCheck, CheckResult, CheckStatus
from config.settings import get_settings


DOCUMENTATION_ROLES = {"reflection", "readme", "design_document", "report", "writeup"}
DEPENDENCY_ROLES    = {"requirements", "requirements_txt", "package_json", "pyproject"}
EVAL_RESULT_ROLES   = {"evaluation_results", "eval_results", "test_results", "benchmark"}


class AdvisoryChecksBundle(BaseCheck):
    """
    Runs all non-blocking checks in one pass. Failures are recorded
    as ADVISORY status — they never stop evaluation but are forwarded
    to the Tier 2 LLM judge as evidence context.

    Checks bundled here:
    1. File size — any file above MAX_FILE_SIZE_MB
    2. Documentation presence — reflection / README / design doc
    3. Dependency manifest — requirements.txt / package.json
    4. Evaluation results artifact — test outputs / benchmark CSV
    """

    check_id = "advisory_bundle"
    blocking  = False

    def run(
        self,
        artifact_urls: dict[str, str],
        file_contents: dict[str, str],
        **kwargs: Any,
    ) -> CheckResult:

        settings   = get_settings()
        advisories = []

        # ── 1. File size ────────────────────────────────────────────
        max_bytes    = settings.max_file_size_mb * 1024 * 1024
        oversized    = []
        for role, content in file_contents.items():
            size = len(content.encode("utf-8")) if content else 0
            if size > max_bytes:
                oversized.append({"file": role, "size_mb": round(size / 1_048_576, 2)})

        advisories.append({
            "check_id": "file_size",
            "status":   CheckStatus.ADVISORY.value if oversized else CheckStatus.PASSED.value,
            "blocking": False,
            "detail":   (
                f"{len(oversized)} file(s) exceed {settings.max_file_size_mb} MB: "
                + ", ".join(f["file"] for f in oversized)
                if oversized
                else f"All files within {settings.max_file_size_mb} MB limit"
            ),
            "metadata": {"oversized_files": oversized},
        })

        # ── 2. Documentation presence ───────────────────────────────
        submitted_lower = {r.lower().replace("-", "_") for r in artifact_urls}
        has_docs = bool(submitted_lower & DOCUMENTATION_ROLES)

        advisories.append({
            "check_id": "documentation_present",
            "status":   CheckStatus.PASSED.value if has_docs else CheckStatus.ADVISORY.value,
            "blocking": False,
            "detail":   (
                "Documentation artifact found"
                if has_docs
                else "No reflection, README, or design document found — "
                     "judge will note sparse documentation"
            ),
            "metadata": {"has_documentation": has_docs},
        })

        # ── 3. Dependency manifest ──────────────────────────────────
        has_deps = bool(submitted_lower & DEPENDENCY_ROLES)

        advisories.append({
            "check_id": "dependency_manifest",
            "status":   CheckStatus.PASSED.value if has_deps else CheckStatus.ADVISORY.value,
            "blocking": False,
            "detail":   (
                "Dependency manifest found"
                if has_deps
                else "No requirements.txt or package.json found"
            ),
            "metadata": {"has_dependency_manifest": has_deps},
        })

        # ── 4. Evaluation results artifact ──────────────────────────
        has_eval = bool(submitted_lower & EVAL_RESULT_ROLES)

        advisories.append({
            "check_id": "evaluation_results_present",
            "status":   CheckStatus.PASSED.value if has_eval else CheckStatus.ADVISORY.value,
            "blocking": False,
            "detail":   (
                "Evaluation results artifact found"
                if has_eval
                else "No evaluation results artifact found — "
                     "judge will score correctness from sandbox output alone"
            ),
            "metadata": {"has_eval_results": has_eval},
        })

        warning_count = sum(1 for a in advisories if a["status"] == CheckStatus.ADVISORY.value)
        passed_count  = sum(1 for a in advisories if a["status"] == CheckStatus.PASSED.value)

        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASSED,   # advisory bundle never blocks
            detail=(
                f"Advisory checks: {passed_count} passed, "
                f"{warning_count} warning(s) (non-blocking)"
            ),
            blocking=False,
            metadata={
                "advisories":      advisories,
                "warning_count":   warning_count,
                "passed_count":    passed_count,
            },
        )
