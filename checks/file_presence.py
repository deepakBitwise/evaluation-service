from __future__ import annotations
from typing import Any
from checks.base import BaseCheck, CheckResult, CheckStatus


class FilePresenceCheck(BaseCheck):
    """
    Verifies that every role listed in `required_deliverables` has a
    corresponding entry in `artifact_urls`. This is purely a key-match
    check — no network call needed, no file content required.

    Fail-fast: a single missing file stops evaluation immediately.
    """

    check_id = "file_presence"
    blocking  = True

    def run(
        self,
        artifact_urls: dict[str, str],
        required_deliverables: list[str],
        **kwargs: Any,
    ) -> CheckResult:

        submitted_roles = set(artifact_urls.keys())
        required_roles  = set(required_deliverables)
        missing         = sorted(required_roles - submitted_roles)
        extra           = sorted(submitted_roles - required_roles)

        if missing:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail=(
                    f"Missing {len(missing)} required file(s): "
                    f"{', '.join(missing)}"
                ),
                blocking=self.blocking,
                metadata={
                    "missing_files":   missing,
                    "submitted_files": sorted(submitted_roles),
                    "required_files":  sorted(required_roles),
                    "extra_files":     extra,
                },
            )

        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASSED,
            detail=(
                f"All {len(required_roles)} required file(s) present"
                + (f" ({len(extra)} extra file(s) ignored)" if extra else "")
            ),
            blocking=self.blocking,
            metadata={
                "submitted_files": sorted(submitted_roles),
                "required_files":  sorted(required_roles),
                "extra_files":     extra,
            },
        )
