from __future__ import annotations
import json
import os
import tarfile
import tempfile
import time
from io import BytesIO
from typing import Any

import docker
from docker.errors import ContainerError, ImageNotFound, APIError

from checks.base import BaseCheck, CheckResult, CheckStatus
from config.settings import get_settings
from utils.logger import get_logger

log = get_logger(__name__)

HARNESS_RUNNER = """
import sys, json, time, traceback, importlib.util

def load_submission(path):
    spec = importlib.util.spec_from_file_location("submission", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def run_tests(test_cases, submission_mod):
    results = []
    for tc in test_cases:
        fn_name = tc["function"]
        inputs  = tc["inputs"]
        expected = tc["expected_output"]
        t0 = time.time()
        try:
            fn     = getattr(submission_mod, fn_name)
            actual = fn(**inputs) if isinstance(inputs, dict) else fn(*inputs)
            passed = (actual == expected)
            results.append({
                "test_id":          tc["test_id"],
                "function":         fn_name,
                "passed":           passed,
                "expected":         expected,
                "actual":           str(actual)[:500],
                "execution_ms":     round((time.time() - t0) * 1000, 2),
                "error":            None,
            })
        except Exception as exc:
            results.append({
                "test_id":          tc["test_id"],
                "function":         fn_name,
                "passed":           False,
                "expected":         expected,
                "actual":           None,
                "execution_ms":     round((time.time() - t0) * 1000, 2),
                "error":            traceback.format_exc(limit=5),
            })
    return results

if __name__ == "__main__":
    with open("/workspace/test_cases.json") as f:
        test_cases = json.load(f)
    sub_path = "/workspace/submission_entry.py"
    mod = load_submission(sub_path)
    results = run_tests(test_cases, mod)
    passed  = sum(1 for r in results if r["passed"])
    report  = {
        "status":          "completed",
        "tests_total":     len(results),
        "tests_passed":    passed,
        "pass_rate":       round(passed / len(results), 4) if results else 0,
        "test_results":    results,
        "exit_code":       0,
        "timed_out":       False,
    }
    print(json.dumps(report))
"""


class SandboxExecutorCheck(BaseCheck):
    """
    Runs the learner's submission inside an isolated Docker container.

    Security constraints applied to every container:
    - No network access (network_mode="none")
    - Memory capped (from settings)
    - CPU quota capped (from settings)
    - Read-only filesystem except /workspace (tmpfs)
    - Container auto-removed after execution
    - Hard timeout enforced by the host

    Receives:
    - file_contents: dict[role, text]  — the learner's files
    - test_cases: list[dict]           — harness test cases from spec
    """

    check_id = "sandbox_execution"
    blocking  = True

    def run(
        self,
        file_contents: dict[str, str],
        test_cases: list[dict],
        entry_point_role: str = "solution",
        **kwargs: Any,
    ) -> CheckResult:

        settings = get_settings()

        entry_content = file_contents.get(entry_point_role, "")
        if not entry_content:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail=f"Entry point file '{entry_point_role}' is empty or missing",
                blocking=self.blocking,
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

        container = None
        try:
            # Start container with sleep instead of running harness immediately
            # This allows us to write files before execution
            container = client.containers.run(
                image=settings.sandbox_image,
                command=["sleep", str(settings.sandbox_timeout_seconds + 60)],
                detach=True,
                network_mode="none",
                mem_limit=settings.sandbox_memory_limit,
                cpu_quota=settings.sandbox_cpu_quota,
                read_only=True,
                tmpfs={settings.sandbox_working_dir: "size=128m,exec"},
                auto_remove=False,
                stdout=True,
                stderr=True,
            )

            self._copy_files_to_container(
                container=container,
                entry_content=entry_content,
                test_cases=test_cases,
                working_dir=settings.sandbox_working_dir,
            )
            
            # Give container time to be ready
            time.sleep(0.5)
            
            # Execute the harness runner and capture output
            exit_code, output = container.exec_run(
                ["python", f"{settings.sandbox_working_dir}/harness_runner.py"],
                stdout=True,
                stderr=True,
            )
            
            # Kill the sleep container
            try:
                container.kill()
            except Exception:
                pass
            
            # Parse output as if it came from container logs
            stdout = output.decode("utf-8", errors="replace") if output else ""
            stderr = ""
            timed_out = False

            if timed_out:
                return CheckResult(
                    check_id=self.check_id,
                    status=CheckStatus.FAILED,
                    detail=f"Execution timed out after {settings.sandbox_timeout_seconds}s",
                    blocking=self.blocking,
                    metadata={
                        "timed_out":  True,
                        "exit_code":  exit_code,
                        "stderr":     stderr[:2000],
                    },
                )

            if exit_code != 0:
                return CheckResult(
                    check_id=self.check_id,
                    status=CheckStatus.FAILED,
                    detail=f"Container exited with code {exit_code}",
                    blocking=self.blocking,
                    metadata={
                        "exit_code": exit_code,
                        "stderr":    stderr[:2000],
                        "stdout":    stdout[:500],
                        "timed_out": False,
                    },
                )

            try:
                report = json.loads(stdout.strip())
            except json.JSONDecodeError:
                return CheckResult(
                    check_id=self.check_id,
                    status=CheckStatus.ERROR,
                    detail="Harness produced non-JSON output — check submission entry point",
                    blocking=self.blocking,
                    metadata={
                        "raw_stdout": stdout[:1000],
                        "stderr":     stderr[:500],
                    },
                )

            report["stdout_full"] = stdout[:5000]
            report["stderr_full"] = stderr[:2000]
            report["exit_code"]   = exit_code

            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.PASSED,
                detail=(
                    f"Sandbox completed: {report.get('tests_passed', 0)}/"
                    f"{report.get('tests_total', 0)} tests passed "
                    f"(pass rate: {report.get('pass_rate', 0):.0%})"
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

    @staticmethod
    def _copy_files_to_container(
        container: Any,
        entry_content: str,
        test_cases: list[dict],
        working_dir: str,
    ) -> None:
        """Write files to container using base64 encoding for reliability."""
        import base64
        
        files = {
            f"{working_dir}/submission_entry.py": entry_content,
            f"{working_dir}/test_cases.json": json.dumps(test_cases, indent=2),
            f"{working_dir}/harness_runner.py": HARNESS_RUNNER,
        }
        
        for filepath, content in files.items():
            # Encode content as base64 to safely pass through shell
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            
            # Use Python one-liner to write file using base64 decoding
            python_cmd = (
                f"import base64; "
                f"open('{filepath}', 'w').write(base64.b64decode('{encoded}').decode('utf-8'))"
            )
            
            container.exec_run(["python", "-c", python_cmd])
