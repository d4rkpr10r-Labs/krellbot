import re

from krellbot.agent import persona, tour

EXPECTED_OPENING = (
    "Hey there. I'm Krell! Welcome to Krellbot. Let me know what you're "
    "interested in, or if you want I can take you on a guided tour of how "
    "algo trading works and all the new advances that have come with agentic "
    "AI and crypto markets."
)

PROFILE = {"budget": "small", "goals": "learn", "risk": "low"}


def _prompts() -> dict[str, str]:
    return {
        "standard": persona.system_prompt(mode="standard", profile=PROFILE, surface="workstation"),
        "advanced": persona.system_prompt(mode="advanced", profile=PROFILE, surface="workstation"),
        "mcp": persona.system_prompt(mode=None, profile={}, surface="mcp"),
    }


def test_opening_line_is_the_owner_text_verbatim():
    assert persona.OPENING_LINE == EXPECTED_OPENING
    assert persona.MODE_QUESTION == "Before we start: are you a standard user or an advanced user?"
    assert persona.PERSONA_VERSION == "1"


def test_every_honesty_rule_appears_in_both_mode_prompts_and_the_mcp_prompt():
    assert len(persona.HONESTY_RULES) >= 6
    for name, prompt in _prompts().items():
        for rule in persona.HONESTY_RULES:
            assert rule in prompt, (name, rule)
    joined = " ".join(persona.HONESTY_RULES).lower()
    for phrase in (
        "simulated",
        "not financial advice",
        "live",
        "tool result",
        "instructions",
    ):
        assert phrase in joined


def test_mcp_prompt_tells_the_model_it_is_in_someone_elses_client():
    prompt = _prompts()["mcp"].lower()
    assert "client" in prompt
    assert "simulated" in prompt
    assert "not financial advice" in prompt


def test_standard_prompt_asks_budget_goals_risk_and_advanced_prompt_does_not():
    standard = _prompts()["standard"].lower()
    advanced = _prompts()["advanced"].lower()
    for word in ("budget", "goals", "risk tolerance"):
        assert word in standard
        assert word not in advanced
    assert "conservative" in standard
    assert "deep end" in advanced
    for prompt in (standard, advanced):
        assert "autopilot" in prompt
        assert "ask before" in prompt


def test_unset_mode_prompt_asks_the_mode_question():
    prompt = persona.system_prompt(mode=None, profile={}, surface="workstation")
    assert persona.MODE_QUESTION in prompt
    assert persona.OPENING_LINE in prompt


def test_tour_has_the_seven_steps_in_order_and_no_return_claims():
    assert tour.TOUR_VERSION == "1"
    assert [s.id for s in tour.TOUR_STEPS] == [
        "algo-basics",
        "rules-vs-discretion",
        "stops-and-risk",
        "backtests-vs-reality",
        "paper-first",
        "agentic-ai-in-crypto",
        "krellbot-keeps-agents-in-bounds",
    ]
    bad = re.compile(r"%.*(return|guarantee)|(return|guarantee).*%", re.IGNORECASE)
    for step in tour.TOUR_STEPS:
        assert step.title
        assert 3 <= len(step.facts) <= 6, step.id
        for fact in step.facts:
            assert not bad.search(fact), fact


def test_mcp_prompt_says_the_client_approves_calls_and_claims_no_krellbot_confirmations():
    prompt = _prompts()["mcp"].lower()
    # MCP agents get no Krellbot confirm cards; their client's own approval is the human check.
    assert "confirm" not in prompt
    assert "hard limits" in prompt
    assert "approval" in prompt


def test_mode_texts_and_opening_text_are_the_texts_the_mode_prompts_use():
    modes = persona.mode_texts()
    assert set(modes) == {"standard", "advanced"}
    assert modes["standard"] in _prompts()["standard"]
    assert modes["advanced"] in _prompts()["advanced"]
    assert modes["standard"] not in _prompts()["advanced"]
    opening = persona.opening_text()
    assert opening in persona.system_prompt(mode=None, profile={}, surface="workstation")
    assert persona.OPENING_LINE in opening
    assert persona.MODE_QUESTION in opening
    assert "Do not suggest anything until they answer." in opening
