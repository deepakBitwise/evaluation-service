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
PLATFORM_KEY_MAP: dict[str, str] = {
    "AZURE_OPENAI_API_KEY":     "platform_azure_openai_api_key",
    "AZURE_OPENAI_ENDPOINT":    "platform_azure_openai_endpoint",
    "AZURE_OPENAI_API_VERSION": "platform_azure_openai_api_version",
    "OPENAI_API_KEY":           "platform_openai_api_key",
    "ANTHROPIC_API_KEY":        "platform_anthropic_api_key",
    "GROQ_API_KEY":             "platform_groq_api_key",
    "COHERE_API_KEY":           "platform_cohere_api_key",
    "GOOGLE_API_KEY":           "platform_google_api_key",
    "GEMINI_API_KEY":           "platform_google_api_key",
    "MISTRAL_API_KEY":          "platform_mistral_api_key",
    "LLM_API_KEY":              "platform_openai_api_key",
    "API_KEY":                  "platform_openai_api_key",
}

# ── Harness injected into container ─────────────────────────────────────────
AGENT_HARNESS = r'''
import subprocess, json, sys, os, time, re

WEB_SEARCH_MARKERS = [
    "according to", "source:", "reported by", "as of", "retrieved",
    "https://", "http://", "search results", "bing", "google",
    "web search", "found online", "based on search", "latest news",
    "recent reports", "citation", "[1]", "[2]", "ref:",
]

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


def detect_web_search_used(output_text):
    text_lower = output_text.lower()
    found = [m for m in WEB_SEARCH_MARKERS if m.lower() in text_lower]
    return len(found) > 0, found


def extract_sources(output_text):
    """
    Extract URLs and named citations from the agent output.
    Returns a dict with:
      urls      : list of http/https URLs found in the response
      citations : list of named sources (e.g. 'According to Reuters')
    """
    # Extract all URLs
    urls = re.findall(r"https?://[^\s'\"<>)]+", output_text)
    urls = list(dict.fromkeys(urls))[:5]   # dedupe, cap at 5

    # Extract "According to X" / "Source: X" / "Reported by X" patterns
    citation_patterns = [
        r'according to ([A-Z][^,.\n]{2,40})',
        r'source[s]?:?\s*([A-Z][^,.\n]{2,40})',
        r'reported by ([A-Z][^,.\n]{2,40})',
        r'via ([A-Z][^,.\n]{2,40})',
        r'per ([A-Z][^,.\n]{2,40})',
        r'from ([A-Z][^,.\n]{2,40})',
        r'\[([0-9]+)\]\s*([^\n]{5,80})',   # numbered references
    ]
    citations = []
    for pattern in citation_patterns:
        matches = re.findall(pattern, output_text, re.IGNORECASE)
        for m in matches:
            src = m if isinstance(m, str) else " ".join(m)
            src = src.strip()
            src_lower = src.lower()
            if " import " in src_lower or src_lower.startswith(("file ", "module ")):
                continue
            if src and src not in citations:
                citations.append(src)
    citations = citations[:5]   # cap at 5

    return {"urls": urls, "citations": citations}


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

    web_search_used, web_markers_found = detect_web_search_used(combined)
    sources = extract_sources(combined)

    results.append({
        "test_id":             tc["test_id"],
        "suite":               tc.get("suite", "standard"),
        "requires_web_search": tc.get("requires_web_search", False),
        "description":         tc.get("description", ""),
        "input":               tc["input"],
        "passed":              passed,
        "keywords_expected":   keywords,
        "keywords_matched":    matched,
        "match_type":          match_type,
        "web_search_used":     web_search_used,
        "web_search_markers":  web_markers_found,
        "sources_found":       sources,
        "output_excerpt":      combined[:600],
        "exit_code":           exit_code,
        "execution_sec":       elapsed,
        "timed_out":           timed_out,
        "error":               stderr[:300] if not passed else "",
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
    Runs the learner's agent.py inside a Docker container.

    For standard agents   → runs standard suite only, simple pass rate.
    For web-search agents → runs BOTH suites, calculates weighted score:
        weighted = (standard_pass_rate × standard_weight)
                 + (web_search_pass_rate × web_search_weight)

    Weights come from the level spec's sandbox_suite_weights.
    Logs clearly whether web search was detected in each test case output.
    """

    check_id = "sandbox_execution"
    blocking  = True

    def run(
        self,
        extracted_contents: dict[str, str],
        test_cases_standard: list[dict],
        test_cases_web_search: list[dict],
        agent_type: str,
        suite_weights: dict[str, float],
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

        # Tag each test case with its suite name and web-search flag
        tagged_standard = [
            {**tc, "suite": "standard", "requires_web_search": False}
            for tc in test_cases_standard
        ]
        tagged_web = [
            {**tc, "suite": "web_search", "requires_web_search": True}
            for tc in test_cases_web_search
        ]

        # Always run both suites. Standard agents still need to demonstrate how
        # they behave when asked for fresh, source-backed information.
        all_test_cases = tagged_standard + tagged_web
        scoring_mode = "weighted" if tagged_web else "simple"

        if not all_test_cases:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.SKIPPED,
                detail="No test cases configured — sandbox skipped",
                blocking=False,
            )

        try:
            client = docker.from_env()
        except Exception as exc:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.ERROR,
                detail=f"Docker daemon unavailable: {exc!r}",
                blocking=self.blocking,
            )

        container_env = self._build_env(
            extracted_contents.get(".env", ""), settings
        )
        provider_note = self._detect_provider(container_env)
        log.info("sandbox_provider", provider=provider_note,
                 agent_type=agent_type, scoring_mode=scoring_mode,
                 total_tests=len(all_test_cases))

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

            self._copy_files(container, agent_code, all_test_cases)

            try:
                result    = container.wait(timeout=settings.sandbox_timeout_seconds)
                exit_code = result.get("StatusCode", -1)
                timed_out = False
            except Exception:
                exit_code = -1
                timed_out = True
                log.warning(
                    "container_wait_timeout",
                    container_id=container.short_id,
                    timeout_seconds=settings.sandbox_timeout_seconds,
                    hint=(
                        "pip install is likely slow. "
                        "Build and use tier1-sandbox:latest — "
                        "see Dockerfile.sandbox"
                    ),
                )
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
                        "stdout_tail":   stdout[-1000:],
                        "stderr":        stderr[:1000],
                        "exit_code":     exit_code,
                        "provider_used": provider_note,
                    },
                )

            # ── Calculate score based on scoring mode ────────────────
            report = self._calculate_score(
                report=report,
                scoring_mode=scoring_mode,
                suite_weights=suite_weights,
            )

            # ── Log per-test web-search detection ────────────────────
            self._log_test_results(report["test_results"], provider_note)

            report["stderr_excerpt"] = stderr[:500]
            report["exit_code"]      = exit_code
            report["provider_used"]  = provider_note
            report["scoring_mode"]   = scoring_mode
            report["agent_type"]     = agent_type

            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.PASSED,
                detail=self._build_detail(report, scoring_mode),
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

    # ── Score calculation ─────────────────────────────────────────────────────

    @staticmethod
    def _calculate_score(
        report: dict,
        scoring_mode: str,
        suite_weights: dict[str, float],
    ) -> dict:
        """
        Calculates the final pass_rate.

        Simple mode (standard agent):
            pass_rate = standard_passed / standard_total

        Weighted mode (web-search agent):
            standard_rate  = standard_passed / standard_total
            web_rate       = web_passed / web_total
            pass_rate      = (standard_rate × weight_s) + (web_rate × weight_w)
        """
        results = report.get("test_results", [])

        standard_results   = [r for r in results if r.get("suite") == "standard"]
        web_search_results = [r for r in results if r.get("suite") == "web_search"]

        if scoring_mode == "weighted" and web_search_results:
            s_total   = len(standard_results)
            s_passed  = sum(1 for r in standard_results if r["passed"])
            s_rate    = round(s_passed / s_total, 4) if s_total else 0.0

            w_total   = len(web_search_results)
            w_passed  = sum(1 for r in web_search_results if r["passed"])
            w_rate    = round(w_passed / w_total, 4) if w_total else 0.0

            wt_s = suite_weights.get("standard",   0.40)
            wt_w = suite_weights.get("web_search",  0.60)

            weighted = round((s_rate * wt_s) + (w_rate * wt_w), 4)

            report["scoring_breakdown"] = {
                "standard": {
                    "tests_total":  s_total,
                    "tests_passed": s_passed,
                    "pass_rate":    s_rate,
                    "weight":       wt_s,
                    "contribution": round(s_rate * wt_s, 4),
                },
                "web_search": {
                    "tests_total":  w_total,
                    "tests_passed": w_passed,
                    "pass_rate":    w_rate,
                    "weight":       wt_w,
                    "contribution": round(w_rate * wt_w, 4),
                },
            }
            report["tests_total"]  = s_total + w_total
            report["tests_passed"] = s_passed + w_passed
            report["pass_rate"]    = weighted

        else:
            # Simple mode — standard suite only, used when no web-search suite exists.
            total  = len(standard_results) or len(results)
            passed = sum(1 for r in standard_results or results if r["passed"])
            report["tests_total"]  = total
            report["tests_passed"] = passed
            report["pass_rate"]    = round(passed / total, 4) if total else 0.0
            report["scoring_breakdown"] = {
                "standard": {
                    "tests_total":  total,
                    "tests_passed": passed,
                    "pass_rate":    report["pass_rate"],
                    "weight":       1.0,
                    "contribution": report["pass_rate"],
                }
            }

        return report

    # ── Logging ───────────────────────────────────────────────────────────────

    @staticmethod
    def _log_test_results(test_results: list[dict], provider: str) -> None:
        """
        Log each test case result with:
          - web-search detection (markers found in output)
          - extracted sources (URLs + named citations)
        """
        log.info("sandbox_results_summary", provider=provider,
                 total=len(test_results))

        for t in test_results:
            suite            = t.get("suite", "standard")
            requires_web     = t.get("requires_web_search", False)
            web_search_used  = t.get("web_search_used", False)
            web_markers      = t.get("web_search_markers", [])
            passed           = t.get("passed", False)
            keywords_matched = t.get("keywords_matched", [])
            sources          = t.get("sources_found", {"urls": [], "citations": []})

            # Web-search status string
            if requires_web:
                if web_search_used:
                    ws_status = f"DETECTED ({', '.join(web_markers[:2])})"
                else:
                    ws_status = "NOT DETECTED in output"
            else:
                ws_status = "N/A (standard test)"

            # Source summary
            urls      = sources.get("urls", [])
            citations = sources.get("citations", [])
            if urls or citations:
                src_summary = (
                    ([f"URL: {u}" for u in urls] +
                     [f"Citation: {c}" for c in citations])
                )
            else:
                src_summary = ["none detected"]

            log.info(
                "test_case_result",
                test_id          = t.get("test_id"),
                suite            = suite,
                passed           = passed,
                requires_web     = requires_web,
                web_search_status= ws_status,
                keywords_matched = keywords_matched,
                sources          = src_summary,
                execution_sec    = t.get("execution_sec"),
                timed_out        = t.get("timed_out", False),
            )

            # Log sources on a separate line if web-search test
            if requires_web:
                if urls:
                    log.info("sources_urls",
                             test_id=t.get("test_id"),
                             urls=urls)
                if citations:
                    log.info("sources_citations",
                             test_id=t.get("test_id"),
                             citations=citations)
                if not urls and not citations:
                    log.warning("no_sources_found",
                                test_id=t.get("test_id"),
                                note="agent gave no URLs or named citations — "
                                     "may not be using web search")

    # ── Detail string ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_detail(report: dict, scoring_mode: str) -> str:
        breakdown = report.get("scoring_breakdown", {})
        if scoring_mode == "weighted":
            s = breakdown.get("standard",  {})
            w = breakdown.get("web_search", {})
            return (
                f"Weighted sandbox score: {report['pass_rate']:.0%} — "
                f"Standard {s.get('tests_passed')}/{s.get('tests_total')} "
                f"({s.get('pass_rate', 0):.0%} × {s.get('weight', 0):.0%} weight) + "
                f"Web-search {w.get('tests_passed')}/{w.get('tests_total')} "
                f"({w.get('pass_rate', 0):.0%} × {w.get('weight', 0):.0%} weight)"
            )
        return (
            f"Sandbox score: {report['pass_rate']:.0%} — "
            f"{report['tests_passed']}/{report['tests_total']} standard checks passed"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_env(self, env_content: str, settings: Any) -> dict[str, str]:
        env: dict[str, str] = {}
        declared_keys: list[str] = []

        for line in env_content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, _ = stripped.partition("=")
            declared_keys.append(key.strip().upper())

        uses_azure = any(k.startswith("AZURE_OPENAI") for k in declared_keys)

        for key in declared_keys:
            settings_attr = PLATFORM_KEY_MAP.get(key)
            if settings_attr:
                real_val = getattr(settings, settings_attr, "")
                if real_val:
                    env[key] = real_val
                    continue
            env[key] = ""

        if uses_azure:
            if settings.platform_azure_openai_api_key:
                env["AZURE_OPENAI_API_KEY"]     = settings.platform_azure_openai_api_key
            if settings.platform_azure_openai_endpoint:
                env["AZURE_OPENAI_ENDPOINT"]    = settings.platform_azure_openai_endpoint
            if settings.platform_azure_openai_api_version:
                env["AZURE_OPENAI_API_VERSION"] = settings.platform_azure_openai_api_version

        return env

    @staticmethod
    def _detect_provider(container_env: dict[str, str]) -> str:
        if container_env.get("AZURE_OPENAI_API_KEY"):
            endpoint = container_env.get("AZURE_OPENAI_ENDPOINT", "")
            resource = endpoint.split("//")[-1].split(".")[0] if endpoint else "unknown"
            return f"Azure OpenAI ({resource})"
        if container_env.get("OPENAI_API_KEY"):    return "OpenAI"
        if container_env.get("ANTHROPIC_API_KEY"): return "Anthropic"
        if container_env.get("GROQ_API_KEY"):      return "Groq"
        if container_env.get("GOOGLE_API_KEY"):    return "Google Gemini"
        return "Unknown provider"

    def _get_requirements(self, extracted_contents: dict[str, str]) -> str:
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
        import_to_pip = {
            "openai":      "openai",
            "anthropic":   "anthropic",
            "groq":        "groq",
            "cohere":      "cohere",
            "google":      "google-generativeai",
            "dotenv":      "python-dotenv",
            "langchain":   "langchain",
            "llama_index": "llama-index",
            "tiktoken":    "tiktoken",
            "requests":    "requests",
            "httpx":       "httpx",
            "pydantic":    "pydantic",
            "azure":       "azure-ai-inference",
        }
        pattern = re.compile(
            r"^\s*(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.MULTILINE
        )
        pkgs = []
        for match in pattern.finditer(agent_code):
            pkg = match.group(1).split(".")[0]
            if pkg not in stdlib and pkg not in pkgs:
                pkgs.append(import_to_pip.get(pkg, pkg))
        return "\n".join(pkgs)

    def _startup_cmd(self, requirements: str) -> str:
        """
        Build the shell command that runs inside the container.

        If a pre-built sandbox image is used (tier1-sandbox:latest) the
        common packages are already installed. pip install is only called
        for any ADDITIONAL packages the learner declared that are not in
        the base image.

        Pre-installed packages are skipped via --ignore-installed=False
        and pip's own resolver — packages already present are no-ops.
        """
        lines = [
            ln.strip() for ln in requirements.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if lines:
            pkgs    = " ".join(f'"{p}"' for p in lines)
            # --quiet        : suppress progress bars
            # --no-cache-dir : keep container lean
            # Use && so dependency installation failures stop before agent.py runs.
            pip_cmd = f"python -m pip install --quiet --no-cache-dir {pkgs} && "
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
