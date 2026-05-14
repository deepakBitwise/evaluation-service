"""
Standalone test for Level1SandboxExecutor.
3 standard + 3 web-search test cases. Weighted scoring for web-search agents.
Sources (URLs + citations) are extracted and logged per test case.

Run from inside tier1_worker/:
    python test_level1_sandbox.py
"""
import sys, os

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checks.level1_sandbox_executor import Level1SandboxExecutor
from checks.base import CheckStatus
from config.level_specs import get_level_spec

spec          = get_level_spec(1)
suite_weights = spec.get("sandbox_suite_weights",
                         {"standard": 0.40, "web_search": 0.60})

# ── Standard agent (Azure OpenAI) ─────────────────────────────────────────────
STANDARD_AGENT = '''
import os, sys
from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv()

client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
)

SYSTEM_PROMPT = """You are a friendly and concise AI assistant.
Answer questions helpfully in 1-3 sentences.
If you do not know something, say so honestly."""

def chat(user_message):
    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.7,
        max_tokens=150,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
    )
    return response.choices[0].message.content

if __name__ == "__main__":
    user_input = sys.stdin.readline().strip()
    if user_input:
        print(chat(user_input))
'''

# ── Web-search agent (Azure OpenAI) ──────────────────────────────────────────
# Simulates citation-style responses since Bing grounding may not be
# available in all Azure deployments.
WEB_SEARCH_AGENT = '''
import os, sys
from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv()

client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
)

SYSTEM_PROMPT = """You are a research assistant with web search capability.
When answering questions about current events or real-time data:
1. Always cite your sources using the format: According to [Source Name]:
2. Include relevant URLs when available
3. Add a note about the date of information
4. Acknowledge if information may be outdated beyond your training
Answer in 3-5 sentences and always include at least one source citation."""

def chat(user_message):
    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.3,
        max_tokens=300,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
    )
    return response.choices[0].message.content

if __name__ == "__main__":
    user_input = sys.stdin.readline().strip()
    if user_input:
        print(chat(user_input))
'''

ENV_CONTENT = (
    "AZURE_OPENAI_API_KEY=your_azure_openai_key\n"
    "AZURE_OPENAI_ENDPOINT=https://your-resource.cognitiveservices.azure.com/\n"
    "AZURE_OPENAI_API_VERSION=2024-12-01-preview\n"
)


def sep(title=""):
    print("\n" + "=" * 62)
    if title:
        print(f"  {title}")
        print("=" * 62)


def run_test(label, agent_code, agent_type):
    sep(label)
    s_count = len(spec["test_cases_standard"])
    w_count = len(spec["test_cases_web_search"])

    if agent_type == "web_search":
        print(f"  Suites    : standard ({s_count}) + web-search ({w_count})")
        print(f"  Weights   : standard {suite_weights['standard']:.0%}  |  "
              f"web-search {suite_weights['web_search']:.0%}")
        print(f"  Scoring   : weighted")
    else:
        print(f"  Suites    : standard only ({s_count} tests)")
        print(f"  Scoring   : simple pass rate")
    print()

    result = Level1SandboxExecutor().execute(
        extracted_contents={"agent.py": agent_code, ".env": ENV_CONTENT},
        test_cases_standard=spec["test_cases_standard"],
        test_cases_web_search=spec["test_cases_web_search"],
        agent_type=agent_type,
        suite_weights=suite_weights,
    )

    print(f"\n  STATUS   : {result.status.value.upper()}")
    print(f"  DETAIL   : {result.detail}")

    meta = result.metadata

    if result.status == CheckStatus.ERROR:
        print(f"\n  ERROR — {str(meta.get('stderr', str(meta)))[:300]}")
        return

    # ── Score breakdown ──────────────────────────────────────────────
    breakdown = meta.get("scoring_breakdown", {})
    print(f"\n  ── Score Breakdown ─────────────────────────────────")
    print(f"  Final score  : {meta.get('pass_rate', 0):.1%}")
    for suite_name, data in breakdown.items():
        label_s = "Standard     " if suite_name == "standard" else "Web-search   "
        print(
            f"  {label_s} : "
            f"{data['tests_passed']}/{data['tests_total']} passed "
            f"({data['pass_rate']:.0%}) "
            f"× weight {data['weight']:.0%} "
            f"= {data['contribution']:.0%}"
        )

    # ── Per-test results ──────────────────────────────────────────────
    print(f"\n  ── Per-Test Results ────────────────────────────────")
    current_suite = None

    for t in meta.get("test_results", []):
        suite = t.get("suite", "standard")
        if suite != current_suite:
            current_suite = suite
            hdr = "STANDARD SUITE" if suite == "standard" else "WEB-SEARCH SUITE"
            print(f"\n  [{hdr}]")

        icon        = "PASS" if t["passed"] else "FAIL"
        requires_ws = t.get("requires_web_search", False)
        ws_used     = t.get("web_search_used", False)
        ws_markers  = t.get("web_search_markers", [])
        sources     = t.get("sources_found", {"urls": [], "citations": []})

        # Web-search indicator
        if requires_ws:
            ws_line = ("   WEB-SEARCH DETECTED"
                       if ws_used else "  ⚠  WEB-SEARCH NOT DETECTED")
            if ws_markers:
                ws_line += f"  ({', '.join(ws_markers[:2])})"
        else:
            ws_line = "  —  No web search required"

        print(f"\n  [{icon}] {t['test_id']} — {t['description']}")
        print(f"    Input    : {t['input']}")
        print(ws_line)
        print(f"    Matched  : {t['keywords_matched']}")

        # Source logging
        urls      = sources.get("urls", [])
        citations = sources.get("citations", [])
        if requires_ws:
            if urls:
                print(f"    URLs     :")
                for u in urls:
                    print(f"               {u}")
            if citations:
                print(f"    Citations:")
                for c in citations:
                    print(f"               {c}")
            if not urls and not citations:
                print(f"    Sources  : none detected in output")

        if not t["passed"]:
            out = t.get("output_excerpt", "")[:150]
            print(f"    Output   : {out}")
        print(f"    Time     : {t.get('execution_sec', 0):.1f}s")


# ── Run ───────────────────────────────────────────────────────────────────────
sep("LEVEL 1 SANDBOX TEST  —  Azure OpenAI  —  3 + 3 test cases")

run_test("STANDARD AGENT  (simple scoring)",    STANDARD_AGENT,   "standard")
run_test("WEB-SEARCH AGENT  (weighted scoring)", WEB_SEARCH_AGENT, "web_search")

sep("ALL TESTS COMPLETE")
print()
