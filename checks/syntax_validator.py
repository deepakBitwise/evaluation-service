from __future__ import annotations
import ast
import json
from typing import Any
from checks.base import BaseCheck, CheckResult, CheckStatus


SUPPORTED_VALIDATORS: dict[str, str] = {
    ".py":   "python",
    ".json": "json",
    ".yaml": "yaml",
    ".yml":  "yaml",
}


class SyntaxValidatorCheck(BaseCheck):
    """
    Parses code files without executing them.

    - .py  files: uses ast.compile() in exec mode
    - .json files: uses json.loads()
    - .yaml files: uses PyYAML safe_load if available, else skips

    Receives `file_contents` — a dict of role → raw text.
    Receives `artifact_urls` to determine file extensions from URLs/paths.
    """

    check_id = "syntax_validation"
    blocking  = True

    def run(
        self,
        file_contents: dict[str, str],
        artifact_urls: dict[str, str],
        **kwargs: Any,
    ) -> CheckResult:

        errors:   list[dict] = []
        checked:  list[str]  = []
        skipped:  list[str]  = []

        for role, content in file_contents.items():
            if not content or not content.strip():
                skipped.append(f"{role} (empty)")
                continue

            url_or_path = artifact_urls.get(role, "")
            ext = self._get_extension(url_or_path)

            if ext not in SUPPORTED_VALIDATORS:
                skipped.append(f"{role} (unsupported extension: {ext or 'none'})")
                continue

            lang = SUPPORTED_VALIDATORS[ext]
            error = self._validate(role, content, lang)

            if error:
                errors.append(error)
            else:
                checked.append(f"{role} ({lang})")

        if errors:
            summary = "; ".join(
                f"{e['file']} line {e['line']}: {e['message']}" for e in errors
            )
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail=f"Syntax error(s) in {len(errors)} file(s): {summary}",
                blocking=self.blocking,
                metadata={
                    "errors":   errors,
                    "checked":  checked,
                    "skipped":  skipped,
                },
            )

        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASSED,
            detail=(
                f"Syntax valid for {len(checked)} file(s)"
                + (f" ({len(skipped)} skipped)" if skipped else "")
            ),
            blocking=self.blocking,
            metadata={"checked": checked, "skipped": skipped},
        )

    def _validate(self, role: str, content: str, lang: str) -> dict | None:
        if lang == "python":
            return self._validate_python(role, content)
        if lang == "json":
            return self._validate_json(role, content)
        if lang == "yaml":
            return self._validate_yaml(role, content)
        return None

    @staticmethod
    def _validate_python(role: str, content: str) -> dict | None:
        try:
            compile(content, filename=role, mode="exec")
            return None
        except SyntaxError as exc:
            return {
                "file":    role,
                "lang":    "python",
                "line":    exc.lineno or 0,
                "message": exc.msg,
            }

    @staticmethod
    def _validate_json(role: str, content: str) -> dict | None:
        try:
            json.loads(content)
            return None
        except json.JSONDecodeError as exc:
            return {
                "file":    role,
                "lang":    "json",
                "line":    exc.lineno,
                "message": exc.msg,
            }

    @staticmethod
    def _validate_yaml(role: str, content: str) -> dict | None:
        try:
            import yaml
            yaml.safe_load(content)
            return None
        except ImportError:
            return None   # PyYAML not installed — skip gracefully
        except Exception as exc:
            return {
                "file":    role,
                "lang":    "yaml",
                "line":    getattr(exc, "problem_mark", type("", (), {"line": 0})()).line + 1,
                "message": str(exc),
            }

    @staticmethod
    def _get_extension(url_or_path: str) -> str:
        path = url_or_path.split("?")[0]   # strip query string from pre-signed URLs
        if "." in path.split("/")[-1]:
            return "." + path.rsplit(".", 1)[-1].lower()
        return ""
