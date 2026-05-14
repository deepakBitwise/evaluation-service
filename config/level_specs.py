"""
Level specifications — one entry per level.

Each spec defines:
  submission_type    : "zip_only"
  required_files     : exact filenames inside the ZIP
  sandbox_enabled    : whether to run the sandbox executor
  agent_type         : "standard" | "web_search"
                       Controls which test cases and judge instructions apply.
  test_cases         : keyword-match test cases for sandbox
  min_pass_rate      : minimum sandbox pass rate to proceed to Tier 2
  rubric_dimensions  : Tier 2 LLM judge weights
  pass_thresholds    : scoring gate values
  assessment_scenario: brief shown to the LLM judge
"""

LEVEL_SPECS: dict[int, dict] = {

    1: {
        "level":           1,
        "title":           "Basic LLM Agent",
        "submission_type": "zip_only",

        # ── Required files ───────────────────────────────────────────
        "required_files": [
            "agent.py",
            "output.txt",
            ".env",
            "README.md",
        ],

        # ── Sandbox ──────────────────────────────────────────────────
        "sandbox_enabled": True,
        "min_pass_rate":   0.6,

        # agent_type controls which test suite runs.
        # "standard"   → conversational chatbot (no web search)
        # "web_search"  → agent uses a web search tool for real-time info
        # The worker reads this from the job payload (learner declares it
        # at submission time). Defaults to "standard" if not declared.
        "agent_type": "standard",

        # ── Test cases: Standard conversational agent ─────────────────
        # Keyword matching — case-insensitive, checks stdout + stderr.
        # match_type "any" = at least one keyword present (default)
        # match_type "all" = every keyword must be present
        "test_cases_standard": [
            {
                "test_id":     "l1_t001",
                "description": "Agent responds to a basic greeting",
                "input":       "Hello",
                "expected_keywords": [
                    "hello", "hi", "hey", "greet",
                    "assist", "help", "welcome",
                ],
                "match_type": "any",
            },
            {
                "test_id":     "l1_t002",
                "description": "Agent describes what it can help with",
                "input":       "What can you help me with?",
                "expected_keywords": [
                    "help", "assist", "can", "able",
                    "answer", "support", "provide", "question",
                ],
                "match_type": "any",
            },
            {
                "test_id":     "l1_t003",
                "description": "Agent produces a coherent non-empty response",
                "input":       "Tell me something interesting.",
                "expected_keywords": [
                    ".", "!", "the", "is", "are",
                    "was", "have", "this", "that", "which",
                ],
                "match_type": "any",
            },
            {
                "test_id":     "l1_t004",
                "description": "Agent handles nonsense input gracefully",
                "input":       "asjdhaksjdhaksjdh",
                "expected_keywords": [
                    "understand", "sorry", "clarify",
                    "rephrase", "clear", "sure", "hmm",
                    "could you", "please", "not sure",
                    "repeat", "didn", "don't",
                ],
                "match_type": "any",
            },
            {
                "test_id":     "l1_t005",
                "description": "Agent identifies itself when asked",
                "input":       "What are you?",
                "expected_keywords": [
                    "i am", "i'm", "assistant",
                    "language model", "llm", "ai",
                    "chatbot", "agent", "designed",
                    "built", "created", "model",
                ],
                "match_type": "any",
            },
        ],

        # ── Test cases: Web-search agent ─────────────────────────────
        # For agents that use a web search tool to return real-time info.
        # We cannot predict the exact content — instead we check for
        # structural indicators of a working web-search response:
        #   - Citation markers  (according to, source, reported)
        #   - Temporal markers  (today, recently, 2024, 2025, latest)
        #   - Response substance (has meaningful content)
        "test_cases_web_search": [
            {
                "test_id":     "l1_ws_t001",
                "description": "Agent responds to a recent event query with citations",
                "input":       "What are the latest developments in AI this week?",
                "expected_keywords": [
                    "according", "source", "reported",
                    "released", "announced", "recent",
                    "latest", "today", "week", "2024", "2025",
                    "model", "ai", "research",
                ],
                "match_type": "any",
            },
            {
                "test_id":     "l1_ws_t002",
                "description": "Agent response has meaningful length and substance",
                "input":       "Tell me something that happened in the news today.",
                "expected_keywords": [
                    ".", "the", "a", "is", "was",
                    "has", "have", "been", "will",
                ],
                "match_type": "any",
            },
            {
                "test_id":     "l1_ws_t003",
                "description": "Agent correctly signals uncertainty for very recent events",
                "input":       "What happened in the last 10 minutes in the world?",
                "expected_keywords": [
                    "cannot", "don't", "unable", "real-time",
                    "limited", "not able", "up-to-date",
                    "latest", "search", "found", "results",
                    "information", "data",
                ],
                "match_type": "any",
            },
            {
                "test_id":     "l1_ws_t004",
                "description": "Agent handles a structured factual question",
                "input":       "Who is the current CEO of Microsoft?",
                "expected_keywords": [
                    "satya", "nadella", "microsoft",
                    "ceo", "chief", "executive",
                    "according", "source",
                ],
                "match_type": "any",
            },
            {
                "test_id":     "l1_ws_t005",
                "description": "Agent gracefully handles ambiguous or broad queries",
                "input":       "latest news",
                "expected_keywords": [
                    "here", "today", "recent", "found",
                    "results", "news", "article", "report",
                    "according", "update",
                ],
                "match_type": "any",
            },
        ],

        # ── Rubric dimensions (from design document Level 1) ──────────
        "rubric_dimensions": {
            "correctness": {
                "weight": 0.35,
                "description": (
                    "Does the agent produce accurate, on-topic responses for its "
                    "stated purpose? Does output.txt show it running correctly? "
                    "Did the sandbox keyword checks pass? "
                    "For web-search agents: are the facts cited verifiable?"
                ),
            },
            "documentation_clarity": {
                "weight": 0.20,
                "description": (
                    "Is README.md clear with install and run instructions? "
                    "Can a reviewer reproduce the agent from the README alone? "
                    "Is the .env structure self-explanatory and well-labelled?"
                ),
            },
            "architecture_quality": {
                "weight": 0.15,
                "description": (
                    "Is agent.py well-structured? Is the system prompt or persona "
                    "clearly defined? Are model parameters (temperature, max_tokens, "
                    "model name / deployment name) explicitly chosen and justified? "
                    "For Azure OpenAI: is the client setup correct and clean?"
                ),
            },
            "groundedness": {
                "weight": 0.10,
                "description": (
                    "Does the agent avoid hallucination? Does it stay within its "
                    "stated knowledge domain? "
                    "For web-search agents: does it cite sources and avoid "
                    "fabricating recent events not found in search results?"
                ),
            },
            "robustness": {
                "weight": 0.10,
                "description": (
                    "Does the agent handle edge cases gracefully — empty input, "
                    "off-topic queries, nonsense input? Is there error handling "
                    "for API failures in agent.py?"
                ),
            },
            "observability": {
                "weight": 0.05,
                "description": (
                    "Are model parameters visible in the code? "
                    "Is the Azure deployment name / API version logged or documented? "
                    "Is there any cost or latency awareness?"
                ),
            },
            "tool_appropriateness": {
                "weight": 0.05,
                "description": (
                    "If a web search tool or other integration was used, was it "
                    "appropriate and correctly configured? "
                    "For a basic chatbot with no tools, score 3 if that was the "
                    "correct design choice for the stated use case."
                ),
            },
        },

        # ── Pass thresholds ──────────────────────────────────────────
        "pass_thresholds": {
            "min_weighted_score":           3.4,
            "min_per_dimension_score":      3,
            "human_review_band_lower":      3.0,
            "human_review_band_upper":      4.0,
            "max_dispersion_before_review": 1.5,
        },

        # ── Assessment scenario (Tier 2 judge brief) ──────────────────
        # The worker appends a web-search note automatically if agent_type
        # is "web_search" — see tasks.py _build_tier2_scenario().
        "assessment_scenario": (
            "You are evaluating a Level 1 FDE assessment submission. "
            "The learner was asked to build a basic LLM-powered chatbot or agent "
            "with a clear persona, disciplined model parameter choices, and clean "
            "implementation. "
            "The submission contains: agent.py (the implementation), output.txt "
            "(a sample run), .env (environment variable structure with placeholder "
            "values only), and README.md (setup and run instructions). "
            "A sandbox executed the agent against test inputs using keyword matching "
            "to verify functional correctness — results are in the Tier 1 checks. "
            "Evaluate the quality of the agent, correctness of its outputs, clarity "
            "of documentation, and the learner's understanding of LLM fundamentals "
            "based on all submitted evidence. "
            "The agent does NOT need to be deployed — assess from submitted files only."
        ),

        "assessment_scenario_web_search_addendum": (
            "IMPORTANT — This agent uses a web search tool to retrieve real-time "
            "information. When scoring the 'correctness' and 'groundedness' "
            "dimensions, verify the facts cited in output.txt against current "
            "web sources if you have web search capability enabled. "
            "The agent should cite sources, avoid fabricating events, and "
            "acknowledge uncertainty appropriately for very recent or breaking news. "
            "Keyword matching in Tier 1 tested structural quality only — "
            "content accuracy must be assessed here in Tier 2."
        ),

        "max_attempts":   3,
        "cooldown_hours": 12,
        "max_zip_mb":     50,
    },

}


def get_level_spec(level: int) -> dict:
    if level not in LEVEL_SPECS:
        raise ValueError(
            f"Level {level} spec not defined. "
            f"Available: {sorted(LEVEL_SPECS.keys())}"
        )
    return LEVEL_SPECS[level]
"""
Level specifications — one entry per level.

Each spec defines:
  submission_type       : "zip_only"
  required_files        : exact filenames inside the ZIP
  sandbox_enabled       : whether to run the sandbox executor
  sandbox_suite_weights : scoring weights per suite (must sum to 1.0)
  test_cases_standard   : 3 conversational checks (no web search needed)
  test_cases_web_search : 3 real-time checks (require live web search)
  min_pass_rate         : minimum weighted sandbox pass rate
  rubric_dimensions     : Tier 2 LLM judge weights
  pass_thresholds       : scoring gate values
  assessment_scenario   : brief for the LLM judge
"""

LEVEL_SPECS: dict[int, dict] = {

    1: {
        "level":           1,
        "title":           "Basic LLM Agent",
        "submission_type": "zip_only",

        # ── Required files ───────────────────────────────────────────
        "required_files": [
            "agent.py",
            "output.txt",
            ".env",
            "README.md",
        ],

        # ── Sandbox ──────────────────────────────────────────────────
        "sandbox_enabled": True,
        "min_pass_rate":   0.6,

        # Scoring weights when BOTH suites run (web_search agent_type).
        # Standard tests verify basic conversational ability still works.
        # Web-search tests verify real-time retrieval — higher weight
        # because that is the primary skill being assessed.
        "sandbox_suite_weights": {
            "standard":   0.40,
            "web_search": 0.60,
        },

        # ── Standard test cases (3) ───────────────────────────────────
        # Conversational checks — no web search needed.
        # Answers are deterministic enough for keyword matching.
        "test_cases_standard": [
            {
                "test_id":           "l1_t001",
                "description":       "Agent responds to a greeting",
                "input":             "Hello, who are you?",
                "expected_keywords": [
                    "hello", "hi", "assist", "help",
                    "welcome", "i am", "i'm", "assistant",
                ],
                "match_type":          "any",
                "requires_web_search": False,
            },
            {
                "test_id":           "l1_t002",
                "description":       "Agent describes what it can help with",
                "input":             "What can you help me with?",
                "expected_keywords": [
                    "help", "assist", "answer", "support",
                    "able", "can", "designed", "built",
                ],
                "match_type":          "any",
                "requires_web_search": False,
            },
            {
                "test_id":           "l1_t003",
                "description":       "Agent handles nonsense input gracefully",
                "input":             "asdjhkasjdhkajsdhk",
                "expected_keywords": [
                    "understand", "sorry", "clarify",
                    "rephrase", "not sure", "could you",
                    "please", "didn", "don't", "unclear",
                ],
                "match_type":          "any",
                "requires_web_search": False,
            },
        ],

        # ── Web-search test cases (3) ─────────────────────────────────
        # These questions are deliberately unanswerable from training
        # data alone — answers change frequently or are hyperlocal.
        # Keyword matching verifies structural quality of a web-search
        # response. Factual accuracy is evaluated in Tier 2.
        #
        # Domain spread:
        #   l1_ws_t001 — Indian politics  (Chief Minister, Tamil Nadu)
        #   l1_ws_t002 — Finance          (live gold rate in India)
        #   l1_ws_t003 — Technology       (latest AI model launch)
        "test_cases_web_search": [
            {
                "test_id":     "l1_ws_t001",
                "description": "Indian politics — current Bihar health minister and party",
                "input":       "who is the current health minister of bihar? ",
                "expected_keywords": [
                    "nitish", "kumar", "jdu", "nishant",
                    "health minister", "bihar", "party",
                    "government", "minister",
                ],
                "match_type":          "any",
                "requires_web_search": True,
            },
            {
                "test_id":     "l1_ws_t002",
                "description": "Finance — today's gold rate in India",
                "input":       "What is today's gold price per gram in India for 22 karat gold?",
                "expected_keywords": [
                    "gold", "gram", "rupees", "karat",
                    "price", "rate", "today", "per gram",
                    "22k", "22 karat", "rs", "mcx",
                ],
                "match_type":          "any",
                "requires_web_search": True,
            },
            {
                "test_id":     "l1_ws_t003",
                "description": "Technology — latest major AI model or product launch",
                "input":       "What is the most recent major AI model or product that was launched or announced this month?",
                "expected_keywords": [
                    "ai", "model", "launched", "announced",
                    "released", "2025", "2026", "openai",
                    "google", "anthropic", "microsoft",
                    "gemini", "claude", "gpt", "llm",
                ],
                "match_type":          "any",
                "requires_web_search": True,
            },
        ],

        # ── Rubric dimensions (from design document Level 1) ──────────
        "rubric_dimensions": {
            "correctness": {
                "weight": 0.35,
                "description": (
                    "Does the agent produce accurate, on-topic responses? "
                    "Does output.txt show correct results? "
                    "Did the sandbox keyword checks pass? "
                    "For web-search agents: are the facts cited verifiable?"
                ),
            },
            "documentation_clarity": {
                "weight": 0.20,
                "description": (
                    "Is README.md clear with install and run instructions? "
                    "Can a reviewer reproduce the agent from the README alone? "
                    "Is the .env structure self-explanatory?"
                ),
            },
            "architecture_quality": {
                "weight": 0.15,
                "description": (
                    "Is agent.py well-structured? Is the system prompt clearly "
                    "defined? Are model parameters (temperature, max_tokens, "
                    "deployment name) explicitly chosen and justified?"
                ),
            },
            "groundedness": {
                "weight": 0.10,
                "description": (
                    "Does the agent avoid hallucination? "
                    "For web-search agents: does it cite sources and avoid "
                    "fabricating events not found in search results?"
                ),
            },
            "robustness": {
                "weight": 0.10,
                "description": (
                    "Does the agent handle edge cases gracefully? "
                    "Is there error handling for API failures in agent.py? "
                    "Did the sandbox edge-case test (l1_t003) pass?"
                ),
            },
            "observability": {
                "weight": 0.05,
                "description": (
                    "Are model parameters visible in the code? "
                    "Is the Azure deployment name / API version documented?"
                ),
            },
            "tool_appropriateness": {
                "weight": 0.05,
                "description": (
                    "If tools were used, were they appropriate? "
                    "For a basic chatbot with no tools, score 3 if that "
                    "was the correct design choice."
                ),
            },
        },

        # ── Pass thresholds ──────────────────────────────────────────
        "pass_thresholds": {
            "min_weighted_score":           3.4,
            "min_per_dimension_score":      3,
            "human_review_band_lower":      3.0,
            "human_review_band_upper":      4.0,
            "max_dispersion_before_review": 1.5,
        },

        # ── Assessment scenario (Tier 2 judge brief) ──────────────────
        "assessment_scenario": (
            "You are evaluating a Level 1 FDE assessment submission. "
            "The learner was asked to build a basic LLM-powered chatbot or agent "
            "with a clear persona, disciplined model parameter choices, and clean "
            "implementation. "
            "The submission contains: agent.py (the implementation), output.txt "
            "(a sample run), .env (environment variable structure with placeholder "
            "values only), and README.md (setup and run instructions). "
            "A sandbox executed the agent against 3 standard + 3 web-search test "
            "inputs using keyword matching — results are in the Tier 1 checks. "
            "Evaluate the quality of the agent, correctness of outputs, clarity "
            "of documentation, and the learner's understanding of LLM fundamentals. "
            "The agent does NOT need to be deployed — assess from submitted files only."
        ),

        "assessment_scenario_web_search_addendum": (
            "IMPORTANT — This agent uses a web search tool to retrieve real-time "
            "information. When scoring 'correctness' and 'groundedness', verify "
            "facts cited in output.txt against current web sources if you have "
            "web search capability enabled. The agent should cite sources, avoid "
            "fabricating events, and acknowledge uncertainty for breaking news."
        ),

        "max_attempts":   3,
        "cooldown_hours": 12,
        "max_zip_mb":     50,
    },

}


def get_level_spec(level: int) -> dict:
    if level not in LEVEL_SPECS:
        raise ValueError(
            f"Level {level} spec not defined. "
            f"Available: {sorted(LEVEL_SPECS.keys())}"
        )
    return LEVEL_SPECS[level]