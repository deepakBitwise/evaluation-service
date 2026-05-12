from __future__ import annotations
import json
import re
import tarfile
from io import BytesIO
from typing import Any

import docker
from docker.errors import APIError, ImageNotFound

from checks.base import BaseCheck, CheckResult, CheckStatus
from config.settings import get_settings
from utils.logger import get_logger

log = get_logger(__name__)

# ── Platform key injection map ───────────────────────────────────────────────
# Keys: exact env variable names learners use in their .env
# Values: attribute name on the Settings object holding the platform key
PLATFORM_KEY_MAP: dict[str, str] = {
    # Azure OpenAI — primary
    "AZURE_OPENAI_API_KEY":     "platform_azure_openai_api_key",
    "AZURE_OPENAI_ENDPOINT":    "platform_azure_openai_endpoint",
    "AZURE_OPENAI_API_VERSION": "platform_azure_openai_api_version",

    # Standard OpenAI
    "OPENAI_API_KEY":           "platform_openai_api_key",

    # Other providers
    "ANTHROPIC_API_KEY":        "platform_anthropic_api_key",
    "GROQ_API_KEY":             "platform_groq_api_key",
    "COHERE_API_KEY":           "platform_cohere_api_key",
    "GOOGLE_API_KEY":           "platform_google_api_key",
    "GEMINI_API_KEY":           "platform_google_api_key",
    "MISTRAL_API_KEY":          "platform_mistral_api_key",
    "LLM_API_KEY":              "platform_openai_api_key",
    "API_KEY":                  "platform_openai_api_key",
}

# ── Harness injected into the container ─────────────────────────────────────
AGENT_HARNESS = r'''
import subprocess, json, sys, os, time

def run_agent(test_input, timeout=15):
    proc = subprocess.Popen(
        [sys.executable, "/tmp/agent.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
        cwd="/tmp",
    )
    try:
        stdout, stderr = proc.communicate(
            input=(test_input.strip() + "\n").encode("utf-8"),
            timeout=timeout,
        )
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            proc.returncode,
            False,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        return "", "Process timed out", -1, True


def keyword_match(text, keywords, match_type="any"):
    text_lower = text.lower()
    matched = [kw for kw in keywords if kw.lower() in text_lower]
    if match_type == "all":
        return len(matched) == len(keywords), matched
    return len(matched) > 0, matched


with open("/tmp/test_cases.json") as f:
    test_cases = json.load(f)

results = []

for tc in test_cases:
    t0 = time.time()
    stdout, stderr, exit_code, timed_out = run_agent(tc["input"])
    elapsed = round(time.time() - t0, 2)

    combined   = (stdout + " " + stderr).strip()
    keywords   = tc.get("expected_keywords", [])
    match_type = tc.get("match_type", "any")
    passed, matched = keyword_match(combined, keywords, match_type)

    results.append({
        "test_id":           tc["test_id"],
        "description":       tc.get("description", ""),
        "input":             tc["input"],
        "passed":            passed,
        "keywords_expected": keywords,
        "keywords_matched":  matched,
        "match_type":        match_type,
        "output_excerpt":    combined[:600],
        "exit_code":         exit_code,
        "execution_sec":     elapsed,
        "timed_out":         timed_out,
        "error":             stderr[:300] if not passed else "",
    })

passed_count = sum(1 for r in results if r["passed"])
total        = len(results)

report = {
    "status":       "completed",
    "tests_total":  total,
    "tests_passed": passed_count,
    "pass_rate":    round(passed_count / total, 4) if total else 0.0,
    "test_results": results,
    "exit_code":    0,
    "timed_out":    False,
}
print(json.dumps(report))
'''


class Level1SandboxExecutor(BaseCheck):
    """
    Runs the learner's agent.py inside a Docker container with:
      - Network access ON  (required for LLM API calls — Azure or otherwise)
      - Platform API keys injected (replaces learner placeholders)
      - One subprocess per test case, input piped via stdin
      - Keyword matching on combined stdout + stderr

    Supports: Azure OpenAI, standard OpenAI, Anthropic, Groq, Cohere,
              Google Gemini, Mistral — detected from learner's .env variable names.
    """

    check_id = "sandbox_execution"
    blocking  = True

    def run(
        self,
        extracted_contents: dict[str, str],
        test_cases: list[dict],
        **kwargs: Any,
    ) -> CheckResult:

        settings   = get_settings()
        agent_code = extracted_contents.get("agent.py", "")

        if not agent_code or not agent_code.strip():
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail="agent.py is empty — cannot run sandbox",
                blocking=self.blocking,
            )

        if not test_cases:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.SKIPPED,
                detail="No test cases configured — sandbox skipped",
                blocking=False,
            )

        # ── Docker client ────────────────────────────────────────────
        try:
            client = docker.from_env()
        except Exception as exc:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.ERROR,
                detail=f"Docker daemon unavailable: {exc!r}",
                blocking=self.blocking,
            )

        # ── Build container env (inject platform keys) ───────────────
        container_env = self._build_env(
            extracted_contents.get(".env", ""), settings
        )

        # ── Detect provider from injected keys ───────────────────────
        provider_note = self._detect_provider(container_env)
        log.info("sandbox_provider", provider=provider_note)

        # ── Detect pip requirements ──────────────────────────────────
        requirements = self._get_requirements(extracted_contents)

        container = None
        try:
            container = client.containers.run(
                image=settings.sandbox_image,
                command=["sh", "-c", self._startup_cmd(requirements)],
                detach=True,
                network_mode="bridge",
                mem_limit=settings.sandbox_memory_limit,
                cpu_quota=settings.sandbox_cpu_quota,
                environment=container_env,
                auto_remove=False,
                stdout=True,
                stderr=True,
            )

            self._copy_files(container, agent_code, test_cases)

            try:
                result    = container.wait(timeout=settings.sandbox_timeout_seconds)
                exit_code = result.get("StatusCode", -1)
                timed_out = False
            except Exception:
                exit_code = -1
                timed_out = True
                try:
                    container.kill()
                except Exception:
                    pass

            stdout = container.logs(stdout=True,  stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

            if timed_out:
                return CheckResult(
                    check_id=self.check_id,
                    status=CheckStatus.FAILED,
                    detail=f"Sandbox timed out after {settings.sandbox_timeout_seconds}s",
                    blocking=self.blocking,
                    metadata={"timed_out": True, "stderr": stderr[:1000]},
                )

            report = self._extract_report(stdout)
            if report is None:
                return CheckResult(
                    check_id=self.check_id,
                    status=CheckStatus.ERROR,
                    detail=(
                        "Harness produced no JSON report. "
                        "agent.py likely crashed on startup — "
                        "check for missing imports or wrong env variable names."
                    ),
                    blocking=self.blocking,
                    metadata={
                        "stdout_tail":     stdout[-1000:],
                        "stderr":          stderr[:1000],
                        "exit_code":       exit_code,
                        "provider_used":   provider_note,
                    },
                )

            report["stderr_excerpt"] = stderr[:500]
            report["exit_code"]      = exit_code
            report["provider_used"]  = provider_note

            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.PASSED,
                detail=(
                    f"Sandbox completed [{provider_note}]: "
                    f"{report.get('tests_passed', 0)}/"
                    f"{report.get('tests_total', 0)} keyword checks passed "
                    f"({report.get('pass_rate', 0):.0%})"
                ),
                blocking=self.blocking,
                metadata=report,
            )

        except (ImageNotFound, APIError) as exc:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.ERROR,
                detail=f"Docker error: {exc!r}",
                blocking=self.blocking,
            )
        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _build_env(self, env_content: str, settings: Any) -> dict[str, str]:
        """
        Parse learner's .env variable NAMES.
        Inject platform keys for recognised providers.
        For Azure OpenAI — always inject all three Azure variables together
        if the learner declared any one of them.
        """
        env: dict[str, str] = {}
        declared_keys: list[str] = []

        for line in env_content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip().upper()
            declared_keys.append(key)

        # Detect if learner is using Azure
        uses_azure = any(
            k.startswith("AZURE_OPENAI") for k in declared_keys
        )

        for key in declared_keys:
            settings_attr = PLATFORM_KEY_MAP.get(key)
            if settings_attr:
                real_val = getattr(settings, settings_attr, "")
                if real_val:
                    env[key] = real_val
                    continue
            # Keep original variable name with empty value for unmatched
            env[key] = ""

        # If learner uses Azure, always inject all three required vars
        if uses_azure:
            if settings.platform_azure_openai_api_key:
                env["AZURE_OPENAI_API_KEY"] = settings.platform_azure_openai_api_key
            if settings.platform_azure_openai_endpoint:
                env["AZURE_OPENAI_ENDPOINT"] = settings.platform_azure_openai_endpoint
            if settings.platform_azure_openai_api_version:
                env["AZURE_OPENAI_API_VERSION"] = settings.platform_azure_openai_api_version

        return env

    @staticmethod
    def _detect_provider(container_env: dict[str, str]) -> str:
        """Return a readable provider name based on injected keys."""
        if container_env.get("AZURE_OPENAI_API_KEY"):
            endpoint = container_env.get("AZURE_OPENAI_ENDPOINT", "")
            resource = endpoint.split("//")[-1].split(".")[0] if endpoint else "unknown"
            return f"Azure OpenAI ({resource})"
        if container_env.get("OPENAI_API_KEY"):
            return "OpenAI"
        if container_env.get("ANTHROPIC_API_KEY"):
            return "Anthropic"
        if container_env.get("GROQ_API_KEY"):
            return "Groq"
        if container_env.get("GOOGLE_API_KEY"):
            return "Google Gemini"
        return "Unknown provider"

    def _get_requirements(self, extracted_contents: dict[str, str]) -> str:
        """
        Use requirements.txt if submitted.
        Otherwise infer from agent.py import statements.
        """
        if "requirements.txt" in extracted_contents:
            return extracted_contents["requirements.txt"]

        agent_code = extracted_contents.get("agent.py", "")
        stdlib = {
            "os", "sys", "json", "re", "time", "datetime", "math",
            "random", "string", "io", "abc", "typing", "dataclasses",
            "pathlib", "collections", "itertools", "functools",
            "subprocess", "threading", "logging", "traceback",
            "urllib", "http", "base64", "hashlib", "copy", "enum",
            "ast", "inspect", "textwrap", "argparse", "shutil",
        }
        pattern = re.compile(
            r"^\s*(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
            re.MULTILINE,
        )
        import_to_pip = {
            "openai":       "openai",
            "anthropic":    "anthropic",
            "groq":         "groq",
            "cohere":       "cohere",
            "google":       "google-generativeai",
            "dotenv":       "python-dotenv",
            "langchain":    "langchain",
            "llama_index":  "llama-index",
            "tiktoken":     "tiktoken",
            "requests":     "requests",
            "httpx":        "httpx",
            "pydantic":     "pydantic",
            "azure":        "azure-ai-inference",
        }
        pkgs = []
        for match in pattern.finditer(agent_code):
            pkg = match.group(1).split(".")[0]
            if pkg not in stdlib and pkg not in pkgs:
                pip_name = import_to_pip.get(pkg, pkg)
                pkgs.append(pip_name)

        return "\n".join(pkgs)

    def _startup_cmd(self, requirements: str) -> str:
        """Shell command: pip install (if needed) then run harness."""
        lines = [
            ln.strip() for ln in requirements.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if lines:
            pkgs    = " ".join(f'"{p}"' for p in lines)
            pip_cmd = f"pip install --quiet --no-cache-dir {pkgs} && "
        else:
            pip_cmd = ""
        return f"{pip_cmd}python /tmp/harness_runner.py"

    @staticmethod
    def _copy_files(
        container: Any,
        agent_code: str,
        test_cases: list[dict],
    ) -> None:
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            def add(name: str, content: str) -> None:
                data = content.encode("utf-8")
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, BytesIO(data))

            add("agent.py",          agent_code)
            add("harness_runner.py", AGENT_HARNESS)
            add("test_cases.json",   json.dumps(test_cases))
        buf.seek(0)
        container.put_archive("/tmp", buf)

    @staticmethod
    def _extract_report(stdout: str) -> dict | None:
        """Find harness JSON in stdout — pip output appears before it."""
        lines = stdout.strip().splitlines()
        for i in range(len(lines) - 1, -1, -1):
            for candidate in ("\n".join(lines[i:]), lines[i].strip()):
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict) and "test_results" in parsed:
                        return parsed
                except json.JSONDecodeError:
                    pass
        return None