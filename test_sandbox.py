import sys
import os
import json

# So Python can find your project modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checks.sandbox_executor import SandboxExecutorCheck
from checks.base import CheckStatus

# ── 1. The learner's solution code ─────────────────────────────────
# This is what a learner would submit as their solution.py
# Write a simple function that the harness will call and test.

SOLUTION_CODE = """
def add_numbers(a, b):
    return a + b

# def add_numbers(a, b)   # missing colon
#     return a + b

# def add_numbers(a, b):
#     while True:
#         pass

def multiply_numbers(a, b):
    return a * b

def is_palindrome(text):
    cleaned = text.lower().replace(" ", "")
    return cleaned == cleaned[::-1]
"""

# ── 2. The test cases the harness will run ─────────────────────────
# Each test case calls a function from the solution with given inputs
# and checks if the output matches expected_output.

TEST_CASES = [
    {
        "test_id":         "t001",
        "function":        "add_numbers",
        "inputs":          {"a": 2, "b": 3},
        "expected_output": 5
    },
    {
        "test_id":         "t002",
        "function":        "add_numbers",
        "inputs":          {"a": -1, "b": 1},
        "expected_output": 0
    },
    {
        "test_id":         "t003",
        "function":        "multiply_numbers",
        "inputs":          {"a": 4, "b": 5},
        "expected_output": 20
    },
    {
        "test_id":         "t004",
        "function":        "is_palindrome",
        "inputs":          {"text": "racecar"},
        "expected_output": True
    },
    {
        "test_id":         "t005",
        "function":        "is_palindrome",
        "inputs":          {"text": "hello"},
        "expected_output": False
    },
    # This one will intentionally FAIL to verify failure detection
    {
        "test_id":         "t006",
        "function":        "add_numbers",
        "inputs":          {"a": 10, "b": 10},
        "expected_output": 999   # Wrong expected value — should fail
    },
]

# ── 3. Run the sandbox ─────────────────────────────────────────────

def run_test():
    print("\n" + "="*60)
    print("  SANDBOX EXECUTOR TEST")
    print("="*60)

    file_contents = {
        "solution": SOLUTION_CODE
    }

    print("\n[1] Initialising SandboxExecutorCheck...")
    check = SandboxExecutorCheck()

    print("[2] Launching Docker container...")
    print("    (pulling python:3.11-slim if not cached — may take a moment)\n")

    result = check.execute(
        file_contents=file_contents,
        test_cases=TEST_CASES,
        entry_point_role="solution"
    )

    print("="*60)
    print(f"  CHECK STATUS  : {result.status.value.upper()}")
    print(f"  DETAIL        : {result.detail}")
    print("="*60)

    metadata = result.metadata

    if result.status == CheckStatus.ERROR:
        print("\n  ERROR — sandbox did not complete.")
        print(f"  Reason: {metadata}")
        return

    print(f"\n  Tests total   : {metadata.get('tests_total', 'N/A')}")
    print(f"  Tests passed  : {metadata.get('tests_passed', 'N/A')}")
    print(f"  Pass rate     : {metadata.get('pass_rate', 0):.0%}")
    print(f"  Exit code     : {metadata.get('exit_code', 'N/A')}")
    print(f"  Timed out     : {metadata.get('timed_out', False)}")

    test_results = metadata.get("test_results", [])
    if test_results:
        print("\n  Per-test results:")
        print("  " + "-"*50)
        for t in test_results:
            status_icon = "PASS" if t["passed"] else "FAIL"
            print(f"  [{status_icon}] {t['test_id']} · {t['function']}")
            if not t["passed"]:
                print(f"         expected : {t['expected']}")
                print(f"         actual   : {t['actual']}")
                if t.get("error"):
                    print(f"         error    : {t['error'][:200]}")

    stdout = metadata.get("stdout_full", "")
    stderr = metadata.get("stderr_full", "")

    if stderr and stderr.strip():
        print(f"\n  STDERR:\n{stderr[:500]}")

    print("\n" + "="*60)
    print("  TEST COMPLETE")
    print("="*60 + "\n")


if __name__ == "__main__":
    run_test()