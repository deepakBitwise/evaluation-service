from __future__ import annotations
import re
from typing import Any
from checks.base import BaseCheck, CheckResult, CheckStatus

SECRET_PATTERNS: list[tuple[str, str]] = [
    ("openai_api_key",     r"sk-[A-Za-z0-9]{20,}"),
    ("anthropic_api_key",  r"sk-ant-[A-Za-z0-9\-]{30,}"),
    ("aws_access_key",     r"AKIA[0-9A-Z]{16}"),
    ("aws_secret_key",     r"(?i)aws.{0,20}secret.{0,20}['\"][0-9a-zA-Z/+=]{40}['\"]"),
    ("github_token",       r"ghp_[A-Za-z0-9]{36}"),
    ("github_oauth",       r"gho_[A-Za-z0-9]{36}"),
    ("google_api_key",     r"AIza[0-9A-Za-z\-_]{35}"),
    ("stripe_secret",      r"sk_live_[0-9a-zA-Z]{24}"),
    ("private_key_pem",    r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ("generic_password",   r"(?i)(password|passwd|pwd)\s*=\s*['\"][^'\"]{6,}['\"]"),
    ("generic_secret",     r"(?i)(secret|api_key|apikey|token)\s*=\s*['\"][^'\"]{8,}['\"]"),
    ("connection_string",  r"(?i)(mongodb|postgresql|mysql|redis)://[^\s\"']+:[^\s\"'@]+@"),
    ("bearer_token",       r"(?i)Authorization:\s*Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
]

_COMPILED: list[tuple[str, re.Pattern]] = [
    (name, re.compile(pattern)) for name, pattern in SECRET_PATTERNS
]

MAX_CONTENT_BYTES = 1_000_000   # scan first 1 MB of each file


class SecretScannerCheck(BaseCheck):
    """
    Scans the text content of each submitted file against a library of
    known secret patterns. Any match is a hard FAIL and a security flag.

    Receives `file_contents` — a dict of role → raw file text, fetched
    upstream by the worker before check execution.
    """

    check_id = "secret_scan"
    blocking  = True

    def run(
        self,
        file_contents: dict[str, str],
        **kwargs: Any,
    ) -> CheckResult:

        findings: list[dict] = []

        for role, content in file_contents.items():
            if not content:
                continue

            text = content[:MAX_CONTENT_BYTES]
            lines = text.splitlines()

            for line_no, line in enumerate(lines, start=1):
                for secret_type, pattern in _COMPILED:
                    match = pattern.search(line)
                    if match:
                        findings.append({
                            "file":        role,
                            "line":        line_no,
                            "secret_type": secret_type,
                            "snippet":     self._redact(line.strip()),
                        })

        if findings:
            affected_files = sorted({f["file"] for f in findings})
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail=(
                    f"Potential secret(s) detected in {len(affected_files)} "
                    f"file(s): {', '.join(affected_files)}. "
                    "Submission rejected for security review."
                ),
                blocking=self.blocking,
                metadata={
                    "finding_count":  len(findings),
                    "findings":       findings,
                    "affected_files": affected_files,
                    "security_flag":  True,
                },
            )

        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASSED,
            detail=f"No secrets detected across {len(file_contents)} file(s)",
            blocking=self.blocking,
            metadata={"files_scanned": list(file_contents.keys())},
        )

    @staticmethod
    def _redact(line: str) -> str:
        """Keep first 10 chars, redact the rest so logs show context not value."""
        if len(line) <= 10:
            return "***REDACTED***"
        return line[:10] + "***REDACTED***"
