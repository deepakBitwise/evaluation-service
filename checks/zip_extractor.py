from __future__ import annotations
import io
import zipfile
from typing import Any

from checks.base import BaseCheck, CheckResult, CheckStatus

ALLOWED_EXTENSIONS = {
    ".py", ".txt", ".env", ".md", ".json", ".yaml", ".yml",
    ".csv", ".html", ".ipynb", ".toml", ".cfg", ".ini",
    ".png", ".jpg", ".jpeg", ".gif", ".pdf"
}

MAX_UNZIPPED_BYTES  = 50 * 1024 * 1024   # 50 MB total
MAX_SINGLE_FILE_MB  = 20 * 1024 * 1024   # 20 MB per file

TEXT_EXTENSIONS = {
    ".py", ".txt", ".env", ".md", ".json", ".yaml",
    ".yml", ".csv", ".html", ".ipynb", ".toml", ".cfg", ".ini"
}


class ZipExtractorCheck(BaseCheck):
    """
    Validates and extracts a ZIP submission.

    Does NOT require a manifest.json. Finds files by their actual filename
    regardless of whether they sit at the ZIP root or inside a single
    top-level folder (e.g. level-1-basic-llm-agent/).

    Returns extracted_contents: dict[filename → text content]
    so downstream checks can read files directly without re-fetching.
    """

    check_id = "zip_extraction"
    blocking  = True

    def run(self, zip_bytes: bytes, **kwargs: Any) -> CheckResult:

        # ── 1. Valid ZIP? ───────────────────────────────────────────
        if not zipfile.is_zipfile(io.BytesIO(zip_bytes)):
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail="Uploaded file is not a valid ZIP archive",
                blocking=self.blocking,
            )

        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            all_entries = zf.infolist()
            all_names   = [e.filename for e in all_entries]

            # ── 2. Path traversal guard ─────────────────────────────
            for name in all_names:
                if name.startswith("/") or ".." in name:
                    return CheckResult(
                        check_id=self.check_id,
                        status=CheckStatus.FAILED,
                        detail=f"Dangerous path detected in ZIP: '{name}'",
                        blocking=self.blocking,
                    )

            # ── 3. ZIP-bomb guard ───────────────────────────────────
            total_size = sum(e.file_size for e in all_entries)
            if total_size > MAX_UNZIPPED_BYTES:
                return CheckResult(
                    check_id=self.check_id,
                    status=CheckStatus.FAILED,
                    detail=(
                        f"ZIP uncompressed size "
                        f"({total_size // 1_048_576} MB) exceeds 50 MB limit"
                    ),
                    blocking=self.blocking,
                )

            # ── 4. Detect top-level folder prefix ───────────────────
            # Handle both:
            #   flat/   agent.py, output.txt ...
            #   nested/ level-1-basic-llm-agent/agent.py ...
            prefix = self._detect_prefix(all_names)

            # ── 5. Extract files ────────────────────────────────────
            extracted: dict[str, str] = {}   # filename → content
            skipped:   list[str]      = []
            binary_files: list[str]   = []

            for entry in all_entries:
                name = entry.filename

                # Skip directory entries
                if name.endswith("/"):
                    continue

                # Strip the common prefix to get bare filename
                bare = name[len(prefix):] if prefix and name.startswith(prefix) else name

                # Skip files nested deeper than one level
                if "/" in bare:
                    skipped.append(f"{name} (nested too deep — skipped)")
                    continue

                if not bare:
                    continue

                ext = ("." + bare.rsplit(".", 1)[-1].lower()) if "." in bare else ""

                # .env has no extension — treat as text
                is_dotenv = bare == ".env"

                if ext not in ALLOWED_EXTENSIONS and not is_dotenv:
                    skipped.append(f"{bare} (extension '{ext}' not allowed)")
                    continue

                if entry.file_size > MAX_SINGLE_FILE_MB:
                    skipped.append(f"{bare} (exceeds 20 MB per-file limit)")
                    continue

                raw = zf.read(name)

                if ext in TEXT_EXTENSIONS or is_dotenv:
                    try:
                        extracted[bare] = raw.decode("utf-8", errors="replace")
                    except Exception:
                        skipped.append(f"{bare} (UTF-8 decode failed)")
                else:
                    binary_files.append(bare)
                    extracted[bare] = f"[binary: {bare}, {entry.file_size} bytes]"

        if not extracted:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail="ZIP contains no readable files after extraction",
                blocking=self.blocking,
            )

        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASSED,
            detail=(
                f"ZIP extracted: {len(extracted)} file(s) found"
                + (f", {len(skipped)} skipped" if skipped else "")
            ),
            blocking=self.blocking,
            metadata={
                "extracted_files":  list(extracted.keys()),
                "binary_files":     binary_files,
                "skipped_files":    skipped,
                "prefix_stripped":  prefix,
                "extracted_contents": extracted,
            },
        )

    @staticmethod
    def _detect_prefix(names: list[str]) -> str:
        """
        If every file shares a single top-level folder prefix, return it.
        Otherwise return empty string (flat ZIP).

        Example:
          ['level-1-basic-llm-agent/agent.py', 'level-1-basic-llm-agent/README.md']
          → 'level-1-basic-llm-agent/'
        """
        folders = set()
        for name in names:
            if "/" in name:
                folders.add(name.split("/")[0])

        if len(folders) == 1:
            prefix = folders.pop() + "/"
            # Confirm ALL non-directory entries share this prefix
            files = [n for n in names if not n.endswith("/")]
            if all(n.startswith(prefix) for n in files):
                return prefix

        return ""
