"""
Meta-Learner — tracks each agent's accuracy and adjusts their weights.

Maintains a rolling window of trade outcomes per agent.
Agents below 45% accuracy get temporarily muted (weight → 0).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)
cfg = get_config()


def _get_session_bucket(utc_hour: int) -> str:
    """Map a UTC hour to a trading session bucket."""
    if 13 <= utc_hour < 21:
        return "us_open"    # US market hours (most liquid for BTC)
    if 7 <= utc_hour < 13:
        return "europe"     # European session
    return "asia"           # Asian session / overnight


@dataclass
class TradeOutcome:
    """A single trade outcome record for an agent."""
    agent_name: str
    vote: str           # 'UP' or 'DOWN'
    actual: str         # 'UP' or 'DOWN' (what actually happened)
    conviction: float
    timestamp: float
    utc_hour: int = 0   # UTC hour at trade time, for session bucketing
    correct: bool = field(init=False)

    def __post_init__(self) -> None:
        self.correct = self.vote == self.actual

    @property
    def session_bucket(self) -> str:
        return _get_session_bucket(self.utc_hour)


@dataclass
class AgentStats:
    """Rolling statistics for an agent."""
    agent_name: str
    accuracy: float = 0.5
    trade_count: int = 0
    recent_correct: int = 0
    recent_total: int = 0
    weight: float = 1.0
    is_muted: bool = False
    trend: str = "→"    # "↑", "↓", "→"
    session_accuracy: dict = field(default_factory=dict)  # bucket → accuracy

    def to_dict(self) -> dict:
        return {
            "agent": self.agent_name,
            "accuracy": round(self.accuracy, 3),
            "trade_count": self.trade_count,
            "weight": round(self.weight, 3),
            "is_muted": self.is_muted,
            "trend": self.trend,
            "session_accuracy": {
                k: round(v, 3) for k, v in self.session_accuracy.items()
            },
        }


class MetaLearner:
    """
    Tracks agent accuracy over a rolling window and adjusts weights.

    - Agents with accuracy > 60%: weight bonus
    - Agents with accuracy < 45%: muted (weight = 0)
    - Default accuracy = 50% (random chance for prediction markets)
    """

    WINDOW_SIZE = 50            # Rolling window of trades
    MUTE_THRESHOLD = 0.45       # Below this → muted
    BONUS_THRESHOLD = 0.60      # Above this → weight bonus
    MIN_TRADES_FOR_WEIGHT = 20  # Don't adjust weight until N trades — needs enough data
    SESSION_MIN_TRADES = 5      # Minimum trades in a bucket before using its accuracy
    SESSION_BLEND = 0.3         # Weight given to session accuracy vs global (max)
    STATE_FILE = "logs/meta_learner.json"

    MANUAL_OVERRIDE_ROUNDS = 10  # Rounds before manual override expires

    def __init__(self) -> None:
        self._histories: dict[str, deque[TradeOutcome]] = {}
        self._stats: dict[str, AgentStats] = {}
        # Manual overrides from dashboard: agent_name → rounds_remaining
        # Positive = force-unmuted, negative = force-muted
        self._manual_overrides: dict[str, int] = {}
        self._load_state()

    def register_agent(self, agent_name: str) -> None:
        """Register an agent. Must be called before recording outcomes."""
        if agent_name not in self._histories:
            self._histories[agent_name] = deque(maxlen=self.WINDOW_SIZE)
            self._stats[agent_name] = AgentStats(agent_name=agent_name)

    def record_outcome(
        self,
        agent_name: str,
        vote: str,
        actual_direction: str,
        conviction: float,
    ) -> None:
        """Record the outcome of a trade for an agent."""
        if agent_name not in self._histories:
            self.register_agent(agent_name)

        utc_hour = datetime.now(timezone.utc).hour
        outcome = TradeOutcome(
            agent_name=agent_name,
            vote=vote,
            actual=actual_direction,
            conviction=conviction,
            timestamp=time.time(),
            utc_hour=utc_hour,
        )
        self._histories[agent_name].append(outcome)
        self._update_stats(agent_name)

    def _update_stats(self, agent_name: str) -> None:
        """Recompute stats and weight for an agent."""
        history = self._histories[agent_name]
        stats = self._stats[agent_name]

        stats.trade_count = len(history)
        if not history:
            stats.accuracy = 0.5
            stats.weight = 1.0
            stats.is_muted = False
            return

        correct = sum(1 for o in history if o.correct)
        stats.accuracy = correct / len(history)
        stats.recent_correct = correct
        stats.recent_total = len(history)

        # Trend: compare recent 10 vs older
        if len(history) >= 20:
            recent_10 = list(history)[-10:]
            older_10 = list(history)[-20:-10]
            recent_acc = sum(1 for o in recent_10 if o.correct) / 10
            older_acc = sum(1 for o in older_10 if o.correct) / 10
            if recent_acc > older_acc + 0.05:
                stats.trend = "↑"
            elif recent_acc < older_acc - 0.05:
                stats.trend = "↓"
            else:
                stats.trend = "→"

        # Session accuracy: compute per-bucket accuracy for time-of-day weighting
        buckets: dict[str, list[bool]] = {}
        for o in history:
            b = o.session_bucket
            buckets.setdefault(b, []).append(o.correct)
        stats.session_accuracy = {
            b: sum(outcomes) / len(outcomes)
            for b, outcomes in buckets.items()
            if len(outcomes) >= self.SESSION_MIN_TRADES
        }

        # Effective accuracy: blend global with current session if enough data
        current_bucket = _get_session_bucket(datetime.now(timezone.utc).hour)
        session_acc = stats.session_accuracy.get(current_bucket)
        if session_acc is not None:
            # Blend: 70% global accuracy + 30% session accuracy
            effective_accuracy = (
                stats.accuracy * (1 - self.SESSION_BLEND)
                + session_acc * self.SESSION_BLEND
            )
        else:
            effective_accuracy = stats.accuracy

        # ── Manual override handling ──────────────────────────────────────────
        override = self._manual_overrides.get(agent_name, 0)
        if override != 0:
            # Decrement countdown (toward zero) on each stats update
            new_val = override - 1 if override > 0 else override + 1
            if new_val == 0:
                del self._manual_overrides[agent_name]
                logger.info(f"Agent {agent_name} manual override expired after {self.MANUAL_OVERRIDE_ROUNDS} rounds")
            else:
                self._manual_overrides[agent_name] = new_val

            if override > 0:
                # Force-unmuted: keep active regardless of accuracy
                stats.weight = max(0.5, stats.weight) if stats.weight > 0 else 0.5
                stats.is_muted = False
                return
            else:
                # Force-muted: keep muted regardless of accuracy
                stats.weight = 0.0
                stats.is_muted = True
                return

        # Adjust weight based on accuracy
        if stats.trade_count < self.MIN_TRADES_FOR_WEIGHT:
            # Not enough data — use default weight
            stats.weight = 1.0
            stats.is_muted = False
        elif effective_accuracy < self.MUTE_THRESHOLD:
            # Poor accuracy (global + session blend) — mute, but only fully below 40%.
            # Between 40-45%: reduce weight to 0.3 (dampens signal, not silenced).
            # Fully mute below 40%: these agents are actively harmful.
            if effective_accuracy < 0.40:
                stats.weight = 0.0
                stats.is_muted = True
                logger.warning(
                    f"Agent {agent_name} fully muted: effective_accuracy={effective_accuracy:.1%} "
                    f"(below 40%)"
                )
            else:
                stats.weight = 0.3
                stats.is_muted = False
                logger.warning(
                    f"Agent {agent_name} weight reduced to 0.3: "
                    f"effective_accuracy={effective_accuracy:.1%} "
                    f"(below {self.MUTE_THRESHOLD:.0%})"
                )
        elif effective_accuracy >= self.BONUS_THRESHOLD:
            # High accuracy — give weight bonus
            bonus = (effective_accuracy - self.BONUS_THRESHOLD) / (1.0 - self.BONUS_THRESHOLD)
            stats.weight = 1.0 + bonus * 0.5   # Up to 1.5x weight
            stats.is_muted = False
        else:
            # Normal range [45%, 60%): weight scales linearly from 0.9 to 1.0
            stats.weight = 0.9 + (effective_accuracy - self.MUTE_THRESHOLD) / (
                self.BONUS_THRESHOLD - self.MUTE_THRESHOLD
            ) * 0.1
            stats.is_muted = False

        self._save_state()

    def get_stats(self, agent_name: str) -> AgentStats:
        """Get current stats for an agent."""
        if agent_name not in self._stats:
            self.register_agent(agent_name)
        return self._stats[agent_name]

    def get_all_stats(self) -> dict[str, AgentStats]:
        return dict(self._stats)

    def force_mute(self, agent_name: str) -> bool:
        """Manually mute an agent from the dashboard for MANUAL_OVERRIDE_ROUNDS rounds."""
        if agent_name not in self._stats:
            return False
        # Negative = force-muted, counts up to 0
        self._manual_overrides[agent_name] = -self.MANUAL_OVERRIDE_ROUNDS
        stats = self._stats[agent_name]
        stats.is_muted = True
        stats.weight = 0.0
        self._save_state()
        logger.info(f"Agent {agent_name} manually muted for {self.MANUAL_OVERRIDE_ROUNDS} rounds")
        return True

    def force_unmute(self, agent_name: str) -> bool:
        """Manually unmute an agent from the dashboard for MANUAL_OVERRIDE_ROUNDS rounds."""
        if agent_name not in self._stats:
            return False
        # Positive = force-unmuted, counts down to 0
        self._manual_overrides[agent_name] = self.MANUAL_OVERRIDE_ROUNDS
        stats = self._stats[agent_name]
        stats.is_muted = False
        stats.weight = 0.5
        self._save_state()
        logger.info(f"Agent {agent_name} manually unmuted for {self.MANUAL_OVERRIDE_ROUNDS} rounds")
        return True

    def apply_to_votes(self, votes: list) -> list:
        """Apply meta-learner weights, mute status, and stats to agent votes."""
        for vote in votes:
            stats = self.get_stats(vote.agent_name)
            vote.accuracy = stats.accuracy
            vote.weight = stats.weight
            vote.is_muted = stats.is_muted
            vote.trend = stats.trend
            vote.session_accuracy = dict(stats.session_accuracy)
        return votes

    def warmup_from_db(self, records: list[dict]) -> None:
        """
        Pre-warm the rolling histories from the SQLite trade DB.

        Replaces any JSON-loaded state (DB is authoritative — it has the full
        history, not just the last session). If DB has no records, the JSON
        state loaded in __init__ is kept as-is.

        records: list of dicts from trade_db.load_resolved_trades(), newest first.
        Each row must have: actual_direction, agent_votes (JSON str), window_ts.
        """
        if not records:
            return

        # Clear JSON-loaded history and stale manual overrides — DB is authoritative
        for agent_name in self._histories:
            self._histories[agent_name].clear()
        self._manual_overrides.clear()

        # Records arrive newest-first; reverse so we append chronologically
        # (deque(maxlen=50) keeps the tail = most recent, which is what we want)
        for rec in reversed(records):
            actual = rec.get("actual_direction")
            if not actual:
                continue

            try:
                votes = json.loads(rec.get("agent_votes") or "[]")
            except Exception:
                continue

            window_ts = rec.get("window_ts") or 0
            try:
                utc_hour = datetime.fromtimestamp(window_ts, tz=timezone.utc).hour
            except Exception:
                utc_hour = 0

            for vote_data in votes:
                agent = vote_data.get("agent")
                vote = vote_data.get("vote")
                conviction = float(vote_data.get("conviction", 0.5))

                # Only count directional votes (ABSTAIN carries no signal)
                if not agent or vote not in ("UP", "DOWN"):
                    continue

                # Skip votes that were muted at the time — they didn't influence
                # decisions and replaying them would inflate the agent's accuracy
                if vote_data.get("is_muted", False):
                    continue

                if agent not in self._histories:
                    self.register_agent(agent)

                outcome = TradeOutcome(
                    agent_name=agent,
                    vote=vote,
                    actual=actual,
                    conviction=conviction,
                    timestamp=float(window_ts),
                    utc_hour=utc_hour,
                )
                self._histories[agent].append(outcome)

        # Recompute weights for all agents that now have history
        loaded: dict[str, int] = {}
        for agent_name in list(self._histories.keys()):
            n = len(self._histories[agent_name])
            if n > 0:
                self._update_stats(agent_name)
                loaded[agent_name] = n

        if loaded:
            stats_summary = ", ".join(
                f"{a}={n}trades/{self._stats[a].accuracy:.0%}"
                f"{'[MUTED]' if self._stats[a].is_muted else ''}"
                for a, n in loaded.items()
            )
            logger.info(f"Meta-learner warmed from DB [{len(records)} trades]: {stats_summary}")
            self._save_state()
        else:
            logger.info("Meta-learner DB warmup: no resolved agent votes found, keeping JSON state")

    def _save_state(self) -> None:
        """Persist meta-learner state to disk."""
        try:
            histories = {}
            for name, history in self._histories.items():
                histories[name] = [
                    {
                        "vote": o.vote,
                        "actual": o.actual,
                        "conviction": o.conviction,
                        "timestamp": o.timestamp,
                        "utc_hour": o.utc_hour,
                    }
                    for o in history
                ]
            state = {
                "histories": histories,
                "manual_overrides": self._manual_overrides,
                "muted_agents": [n for n, s in self._stats.items() if s.is_muted],
            }
            state_path = Path(self.STATE_FILE)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", dir=state_path.parent, delete=False, suffix=".tmp"
            ) as tmp:
                json.dump(state, tmp)
                tmp_path = tmp.name
            os.replace(tmp_path, state_path)
        except Exception as exc:
            logger.warning(f"Failed to save meta-learner state: {exc}")

    def _load_state(self) -> None:
        """Load meta-learner state from disk."""
        try:
            if not Path(self.STATE_FILE).exists():
                return
            with open(self.STATE_FILE) as f:
                raw = json.load(f)

            # Support both old format (flat dict of histories) and new format
            if "histories" in raw:
                histories = raw["histories"]
                self._manual_overrides = {
                    k: int(v) for k, v in raw.get("manual_overrides", {}).items()
                }
            else:
                # Backward-compatible: old format was just histories at top level
                histories = raw
                self._manual_overrides = {}

            muted_agents: set[str] = set(raw.get("muted_agents", []))

            for name, outcomes in histories.items():
                self.register_agent(name)
                for o in outcomes:
                    outcome = TradeOutcome(
                        agent_name=name,
                        vote=o["vote"],
                        actual=o["actual"],
                        conviction=o["conviction"],
                        timestamp=o["timestamp"],
                        utc_hour=o.get("utc_hour", 0),
                    )
                    self._histories[name].append(outcome)
                self._update_stats(name)
                # Restore muted status for agents that have insufficient post-mute
                # history — prevents a restart from silently resetting weight to 1.0
                if name in muted_agents and self._stats[name].trade_count < self.MIN_TRADES_FOR_WEIGHT:
                    self._stats[name].is_muted = True
                    self._stats[name].weight = 0.0
                    logger.info(f"Agent {name} kept muted on load (insufficient history)")

            logger.info(
                f"Meta-learner state loaded: {list(histories.keys())}, "
                f"overrides: {self._manual_overrides}, "
                f"persisted-muted: {list(muted_agents)}"
            )
        except Exception as exc:
            logger.warning(f"Failed to load meta-learner state: {exc}")
