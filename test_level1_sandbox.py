"""
Standalone test for Level1SandboxExecutor.
Tests both standard and web-search agent types.

Run from inside tier1_worker/:
    python test_level1_sandbox.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checks.level1_sandbox_executor import Level1SandboxExecutor
from checks.base import CheckStatus
from config.level_specs import get_level_spec

spec = get_level_spec(1)


# ── Standard agent using Azure OpenAI ────────────────────────────────────────
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

def chat(user_message: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o",          # Azure deployment name
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
        reply = chat(user_input)
        print(reply)
'''

# ── Web-search agent using Azure OpenAI ──────────────────────────────────────
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
When asked about recent events, search for current information and cite sources.
Always indicate the date of information and acknowledge uncertainty for very recent events."""

def chat_with_search(user_message: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.3,
        max_tokens=300,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        # Azure OpenAI built-in web search (grounding)
        extra_body={
            "data_sources": [{
                "type": "bing_search",
                "parameters": {
                    "endpoint": os.getenv("BING_SEARCH_ENDPOINT", ""),
                    "key":      os.getenv("BING_SEARCH_KEY", ""),
                }
            }]
        } if os.getenv("BING_SEARCH_KEY") else {}
    )
    return response.choices[0].message.content

if __name__ == "__main__":
    user_input = sys.stdin.readline().strip()
    if user_input:
        reply = chat_with_search(user_input)
        print(reply)
'''

ENV_CONTENT = (
    "AZURE_OPENAI_API_KEY=your_azure_openai_key\n"
    "AZURE_OPENAI_ENDPOINT=https://your-resource.cognitiveservices.azure.com/\n"
    "AZURE_OPENAI_API_VERSION=2024-12-01-preview\n"
)


def run_test(label: str, agent_code: str, test_cases: list):
    print("\n" + "=" * 60)
    print(f"  {label}")
    print("=" * 60)
    print(f"\n  Running {len(test_cases)} test cases...")
    print("  (Docker pulls python:3.11-slim if not cached)\n")

    result = Level1SandboxExecutor().execute(
        extracted_contents={
            "agent.py": agent_code,
            ".env":     ENV_CONTENT,
        },
        test_cases=test_cases,
    )

    print(f"  STATUS  : {result.status.value.upper()}")
    print(f"  DETAIL  : {result.detail}")

    meta = result.metadata
    if result.status == CheckStatus.ERROR:
        print(f"\n  ERROR — {str(meta.get('stderr', str(meta)))[:300]}")
        return

    print(f"\n  Tests total  : {meta.get('tests_total')}")
    print(f"  Tests passed : {meta.get('tests_passed')}")
    print(f"  Pass rate    : {meta.get('pass_rate', 0):.0%}")
    print(f"  Provider     : {meta.get('provider_used', 'N/A')}")
    print(f"\n  Per-test results:")
    print("  " + "-" * 50)

    for t in meta.get("test_results", []):
        icon = "PASS" if t["passed"] else "FAIL"
        print(f"  [{icon}] {t['test_id']} — {t['description']}")
        print(f"         Input    : {t['input']}")
        print(f"         Matched  : {t['keywords_matched']}")
        if not t["passed"]:
            print(f"         Expected : {t['keywords_expected']}")
            out = t.get('output_excerpt', '')
            print(f"         Output   : {out[:120]}")
        print()


# ── Run both test suites ──────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("  LEVEL 1 SANDBOX TEST — Azure OpenAI")
print("=" * 60)

# Standard agent
run_test(
    "STANDARD AGENT",
    STANDARD_AGENT,
    spec["test_cases_standard"],
)

# Web-search agent
run_test(
    "WEB-SEARCH AGENT",
    WEB_SEARCH_AGENT,
    spec["test_cases_web_search"],
)

print("\n" + "=" * 60)
print("  ALL TESTS COMPLETE")
print("=" * 60 + "\n")