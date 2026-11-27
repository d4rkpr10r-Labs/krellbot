"""Krell's guided tour: curated, versioned facts she delivers in her own voice.

General education only. No return figures, promises or hype. The owner
reviews this text before release.
"""

from __future__ import annotations

from dataclasses import dataclass

TOUR_VERSION = "1"


@dataclass(frozen=True)
class TourStep:
    id: str
    title: str
    facts: tuple[str, ...]


TOUR_STEPS: tuple[TourStep, ...] = (
    TourStep(
        id="algo-basics",
        title="How algo trading works",
        facts=(
            "Algorithmic trading means a program follows written rules to decide when to buy or sell.",
            "The rules are explicit: a signal (such as a price crossing an average), a size, and exit conditions.",
            "A program can watch markets around the clock and apply its rules the same way every time.",
            "Automation removes some human error, but it does not remove market risk, and a bad rule is carried out just as faithfully as a good one.",
        ),
    ),
    TourStep(
        id="rules-vs-discretion",
        title="Rules versus discretion",
        facts=(
            "A rule-based strategy writes its decisions down in advance; a discretionary trader decides in the moment.",
            "Rules are repeatable and testable. You can ask what the strategy would have done last month.",
            "Discretion can adapt to news a rule never anticipated, but it is harder to test and easier to sway with emotion.",
            "Many people mix the two: rules for entries and exits, a human deciding whether to run them at all.",
        ),
    ),
    TourStep(
        id="stops-and-risk",
        title="Stops and risk",
        facts=(
            "A stop-loss is a rule that exits a position once it has lost a set amount.",
            "Stops can fill at a worse price than planned when markets move fast or liquidity is thin.",
            "Position size matters as much as the signal: a smaller size limits how much any one trade can cost.",
            "A daily-loss limit pauses trading after a set loss, so one bad day cannot keep compounding.",
            "Risk limits reduce losses but cannot remove them.",
        ),
    ),
    TourStep(
        id="backtests-vs-reality",
        title="Backtests versus reality",
        facts=(
            "A backtest replays a strategy over past data. The result is simulated, not something that happened to real money.",
            "Overfitting happens when a strategy is tuned until it matches the past, and then fails on new data.",
            "Fees, slippage and delays are easy to underestimate in a backtest.",
            "Past behavior does not guarantee future behavior, so treat a backtest as a way to find flaws, not as a forecast.",
        ),
    ),
    TourStep(
        id="paper-first",
        title="Paper first",
        facts=(
            "Paper trading runs a strategy against live market data with pretend money.",
            "It shows how a strategy behaves in real time, including how often it trades and how it handles gaps.",
            "Paper fills are modeled, so real fills can differ.",
            "Running on paper first costs nothing and lets you check that the rules do what you meant.",
        ),
    ),
    TourStep(
        id="agentic-ai-in-crypto",
        title="What agentic AI changes in crypto markets",
        facts=(
            "Research gets faster: an AI agent can read data, draft a strategy and run tests in minutes instead of days.",
            "More automated participants may be active in markets, which can change how prices react and how crowded a strategy becomes.",
            "New risks appear. Prompt injection means text in a web page or message can try to steer an agent that reads it.",
            "Agents can also overfit by trying many ideas until one happens to fit past data, and they can state wrong things with confidence.",
            "That is why deterministic rules and hard limits matter: the model proposes, and fixed rules decide what is allowed.",
        ),
    ),
    TourStep(
        id="krellbot-keeps-agents-in-bounds",
        title="How Krellbot keeps agents in bounds",
        facts=(
            "Agents work on paper strategies. They cannot place orders, close positions or change anything live.",
            "Only you can arm a strategy for live trading, and agents cannot read or send your exchange keys.",
            "Hard limits that you set apply to every agent equally, and a refused action stays refused.",
            "Heavier or state-changing steps ask for your confirmation first.",
            "Agent activity is written to an audit trail you can read.",
        ),
    ),
)
