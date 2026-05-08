from __future__ import annotations
from typing import Any
from checks.base import BaseCheck, CheckResult, CheckStatus


class HarnessEvaluatorCheck(BaseCheck):
    """
    Reads the sandbox execution metadata (already stored in the
    SandboxExecutorCheck result) and applies the minimum pass-rate
    threshold from the assessment spec.

    This is a pure logic check — no I/O. It receives the sandbox
    result metadata dict and the threshold, and produces a verdict.
    """

    check_id = "harness_pass_rate"
    blocking  = True

    def run(
        self,
        sandbox_metadata: dict[str, Any],
        min_pass_rate: float,
        **kwargs: Any,
    ) -> CheckResult:

        if sandbox_metadata.get("timed_out"):
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail="Harness could not be evaluated: sandbox timed out",
                blocking=self.blocking,
                metadata={"timed_out": True},
            )

        tests_total  = int(sandbox_metadata.get("tests_total", 0))
        tests_passed = int(sandbox_metadata.get("tests_passed", 0))
        pass_rate    = float(sandbox_metadata.get("pass_rate", 0.0))
        test_results = sandbox_metadata.get("test_results", [])

        if tests_total == 0:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.ERROR,
                detail="Harness reported 0 tests — check harness configuration",
                blocking=self.blocking,
            )

        meets_threshold = pass_rate >= min_pass_rate

        failed_tests = [
            {
                "test_id":  t.get("test_id"),
                "function": t.get("function"),
                "error":    t.get("error"),
                "expected": t.get("expected"),
                "actual":   t.get("actual"),
            }
            for t in test_results
            if not t.get("passed")
        ]

        if not meets_threshold:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAILED,
                detail=(
                    f"Pass rate {pass_rate:.0%} ({tests_passed}/{tests_total}) "
                    f"is below the required {min_pass_rate:.0%}"
                ),
                blocking=self.blocking,
                metadata={
                    "pass_rate":       pass_rate,
                    "tests_passed":    tests_passed,
                    "tests_total":     tests_total,
                    "min_pass_rate":   min_pass_rate,
                    "failed_tests":    failed_tests,
                },
            )

        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASSED,
            detail=(
                f"Harness passed: {tests_passed}/{tests_total} tests "
                f"({pass_rate:.0%} >= required {min_pass_rate:.0%})"
            ),
            blocking=self.blocking,
            metadata={
                "pass_rate":     pass_rate,
                "tests_passed":  tests_passed,
                "tests_total":   tests_total,
                "min_pass_rate": min_pass_rate,
                "failed_tests":  failed_tests,
            },
        )
