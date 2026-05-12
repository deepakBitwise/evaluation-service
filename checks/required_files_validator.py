from __future__ import annotations
from typing import Any
from checks.base import BaseCheck, CheckResult, CheckStatus


class RequiredFilesValidator(BaseCheck):
    """
    Checks that all required files for a level are present in the
    extracted ZIP contents and are non-empty.

    For Level 1 the required files are:
      agent.py      — the chatbot implementation
      output.txt    — sample run output proving the agent works
      .env          — environment variable structure (no real secrets)
      README.md     — setup and run instructions

    Receives:
      extracted_contents : dict[filename → text content]
      required_files     : list[str] from the level spec
    """

    check_id = "required_files_present"
    blocking  = True

    def run(
        self,
        extracted_contents: dict[str, str],
        required_files: list[str],
        **kwargs: Any,
    ) -> CheckResult:

        missing:  list[str] = []
        empty:    list[str] = []
        present:  list[str] = []

        for filename in required_files:
            content = extracted_contents.get(filename)

            if content is None:
                missing.append(filename)
            elif not content.strip():
                empty.append(filename)
            else:
                present.append(filename)

        errors = []
        if missing:
            errors.append(
                f"Missing file(s): {', '.join(missing)}"
            )
        if empty:
            errors.append(
                f"Empty file(s) (no content): {', '.join(empty)}"
            )

        if errors:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail=" | ".join(errors),
                blocking=self.blocking,
                metadata={
                    "required":  required_files,
                    "present":   present,
                    "missing":   missing,
                    "empty":     empty,
                },
            )

        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASSED,
            detail=f"All {len(required_files)} required file(s) present and non-empty",
            blocking=self.blocking,
            metadata={
                "required": required_files,
                "present":  present,
            },
        )
