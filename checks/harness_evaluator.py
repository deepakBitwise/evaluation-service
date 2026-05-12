from __future__ import annotations
from typing import Any
from checks.base import BaseCheck, CheckResult, CheckStatus


class HarnessEvaluatorCheck(BaseCheck):
    """
    Evaluates sandbox execution results for both:

    - Code-based levels (exact return-value matching):
        sandbox_metadata contains test_results with passed: true/false
        based on exact output comparison.

    - Agent-based levels like Level 1 (keyword matching):
        sandbox_metadata contains test_results with passed: true/false
        based on keyword presence in agent stdout.

    In both cases the structure is identical — this evaluator
    applies the min_pass_rate threshold and produces a verdict.
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
                detail="Harness could not be evaluated — sandbox timed out",
                blocking=self.blocking,
                metadata={"timed_out": True},
            )

        tests_total  = int(sandbox_metadata.get("tests_total",  0))
        tests_passed = int(sandbox_metadata.get("tests_passed", 0))
        pass_rate    = float(sandbox_metadata.get("pass_rate",  0.0))
        test_results = sandbox_metadata.get("test_results", [])

        if tests_total == 0:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.ERROR,
                detail="Sandbox reported 0 tests — check harness configuration",
                blocking=self.blocking,
            )

        meets_threshold = pass_rate >= float(min_pass_rate)

        failed_tests = [
            {
                "test_id":           t.get("test_id"),
                "description":       t.get("description", ""),
                "input":             t.get("input", ""),
                "keywords_expected": t.get("keywords_expected",
                                           t.get("expected_output", "")),
                "keywords_matched":  t.get("keywords_matched", []),
                "output_excerpt":    t.get("output_excerpt",
                                           str(t.get("actual", ""))[:300]),
                "error":             t.get("error", ""),
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
                    f"is below the required {float(min_pass_rate):.0%}"
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

        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASSED,
            detail=(
                f"Harness passed: {tests_passed}/{tests_total} "
                f"({pass_rate:.0%} >= required {float(min_pass_rate):.0%})"
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
