from __future__ import annotations
import re
from typing import Any
from checks.base import BaseCheck, CheckResult, CheckStatus

# Patterns that indicate a REAL secret value (not a placeholder)
REAL_SECRET_PATTERNS = [
    ("openai_key",    re.compile(r'sk-[A-Za-z0-9]{20,}')),
    ("anthropic_key", re.compile(r'sk-ant-[A-Za-z0-9\-]{30,}')),
    ("aws_key",       re.compile(r'AKIA[0-9A-Z]{16}')),
    ("github_token",  re.compile(r'ghp_[A-Za-z0-9]{36}')),
    ("google_key",    re.compile(r'AIza[0-9A-Za-z\-_]{35}')),
]

# Patterns that confirm a value is clearly a placeholder — safe
PLACEHOLDER_PATTERNS = re.compile(
    r'^(your[_\-]?.*|<.*>|xxx+|placeholder|changeme|replace[_\-]?me'
    r'|example|dummy|none|empty|\*+|\.\.\.+|add[_\-]?your|insert[_\-]?here)?$',
    re.IGNORECASE
)

# Keys we expect to find in a Level 1 .env
EXPECTED_ENV_KEYS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "COHERE_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "MISTRAL_API_KEY",
    "MODEL_NAME",
    "API_KEY",
    "LLM_API_KEY",
}


class EnvStructureValidator(BaseCheck):
    """
    Validates the .env file submitted with a Level 1 agent.

    The .env must:
      1. Be parseable (valid KEY=value format)
      2. Contain at least one recognisable API key or model variable
      3. NOT contain real secret values — only placeholders or empty values

    Why: The .env is submitted to show the learner knows what environment
    variables their agent needs. Real secrets should never be committed.
    Placeholder values like OPENAI_API_KEY=your_key_here are correct.
    """

    check_id = "env_structure"
    blocking  = True

    def run(
        self,
        env_content: str,
        **kwargs: Any,
    ) -> CheckResult:

        if not env_content or not env_content.strip():
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail=".env file is empty — must contain at least one variable definition",
                blocking=self.blocking,
            )

        lines         = env_content.splitlines()
        parsed_vars:  dict[str, str] = {}
        parse_errors: list[str]      = []
        real_secrets: list[dict]     = []

        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()

            # Skip blank lines and comments
            if not stripped or stripped.startswith("#"):
                continue

            if "=" not in stripped:
                parse_errors.append(
                    f"Line {lineno}: '{stripped[:40]}' is not a valid KEY=value line"
                )
                continue

            key, _, value = stripped.partition("=")
            key   = key.strip()
            value = value.strip()

            if not key:
                parse_errors.append(f"Line {lineno}: empty key")
                continue

            parsed_vars[key] = value

            # Check if the value is a real secret (not a placeholder)
            for secret_type, pattern in REAL_SECRET_PATTERNS:
                if pattern.search(value):
                    real_secrets.append({
                        "key":         key,
                        "line":        lineno,
                        "secret_type": secret_type,
                        "hint":        f"Value looks like a real {secret_type}. "
                                       f"Use a placeholder like 'your_{key.lower()}' instead.",
                    })

        # Hard fail: real secrets detected
        if real_secrets:
            affected = [f"{s['key']} (line {s['line']})" for s in real_secrets]
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail=(
                    f"Real secret value(s) detected in .env: {', '.join(affected)}. "
                    "Submit placeholder values only (e.g. OPENAI_API_KEY=your_key_here). "
                    "Never commit real credentials."
                ),
                blocking=self.blocking,
                metadata={
                    "real_secrets_found": True,
                    "affected_keys":      [s["key"] for s in real_secrets],
                },
            )

        # Soft fail: no variables at all parsed
        if not parsed_vars:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail=(
                    ".env has no valid KEY=value entries. "
                    "Expected at least one variable like OPENAI_API_KEY=your_key_here"
                ),
                blocking=self.blocking,
            )

        # Check at least one recognisable API/model key is present
        found_expected = [k for k in parsed_vars if k.upper() in EXPECTED_ENV_KEYS]
        has_any_api_key = bool(found_expected) or any(
            "API" in k.upper() or "KEY" in k.upper() or "MODEL" in k.upper()
            or "TOKEN" in k.upper()
            for k in parsed_vars
        )

        if not has_any_api_key:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail=(
                    f".env defines {len(parsed_vars)} variable(s) but none look like "
                    "an API key or model configuration. "
                    "Expected at least one of: OPENAI_API_KEY, ANTHROPIC_API_KEY, "
                    "MODEL_NAME, etc."
                ),
                blocking=self.blocking,
                metadata={"found_keys": list(parsed_vars.keys())},
            )

        summary = (
            f".env valid: {len(parsed_vars)} variable(s) defined with placeholder values"
        )
        if parse_errors:
            summary += f" ({len(parse_errors)} non-critical parse warning(s))"

        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASSED,
            detail=summary,
            blocking=self.blocking,
            metadata={
                "variable_count":  len(parsed_vars),
                "variable_names":  list(parsed_vars.keys()),
                "parse_warnings":  parse_errors,
                "recognised_keys": found_expected,
            },
        )
