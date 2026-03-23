"""
Agent consensus engine — weighted voting across all 5 agents.

Combines agent votes with accuracy-weighted importance to determine
the consensus direction and strength.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from agents.agent_base import AgentVote, Vote, BaseAgent
from agents.meta_learner import MetaLearner
from core.logger import get_logger
from strategy.signals import CompositeSignal

logger = get_logger(__name__)


@dataclass
class ConsensusResult:
    """The result of the agent consensus vote."""
    direction: str              # 'UP', 'DOWN', or 'NEUTRAL'
    strength: float             # 0.0 to 1.0
    agreement_ratio: float      # Fraction of non-abstaining agents that agree (0-1)
    votes: list[AgentVote]
    up_weight: float
    down_weight: float
    abstain_count: int

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "strength": round(self.strength, 3),
            "agreement_ratio": round(self.agreement_ratio, 3),
            "up_weight": round(self.up_weight, 3),
            "down_weight": round(self.down_weight, 3),
            "abstain_count": self.abstain_count,
            "votes": [v.to_dict() for v in self.votes],
        }


class ConsensusEngine:
    """
    Aggregates votes from all agents using accuracy-weighted voting.

    Each agent contributes:
        weighted_vote = conviction × agent_accuracy × base_weight
    """

    def __init__(self, agents: list[BaseAgent], meta_learner: MetaLearner) -> None:
        self._agents = agents
        self._meta = meta_learner

        # Register all agents with meta-learner
        for agent in agents:
            self._meta.register_agent(agent.name)

    async def get_consensus(
        self,
        window_delta_pct: float,
        signal: CompositeSignal,
        df_1m: pd.DataFrame,
        df_5s: pd.DataFrame | None,
        ob_imbalance: float | None,
        oracle_delta_pct: float,
        atr_pct: float,
        oracle_latency_sec: float = 0.0,
    ) -> ConsensusResult:
        """
        Run all agents and compute the consensus.
        """
        votes: list[AgentVote] = []

        for agent in self._agents:
            try:
                vote = await agent.vote(
                    window_delta_pct=window_delta_pct,
                    df_1m=df_1m,
                    df_5s=df_5s,
                    ob_imbalance=ob_imbalance,
                    oracle_delta_pct=oracle_delta_pct,
                    atr_pct=atr_pct,
                    oracle_latency_sec=oracle_latency_sec,
                )
                votes.append(vote)
            except Exception as exc:
                logger.error(f"Agent {agent.name} failed: {exc}", exc_info=True)
                # Fallback: ABSTAIN
                votes.append(AgentVote(
                    agent_name=agent.name,
                    vote=Vote.ABSTAIN,
                    conviction=0.0,
                    reasoning=f"Error: {exc}",
                ))

        # Apply meta-learner weights
        self._meta.apply_to_votes(votes)

        return self._compute_consensus(votes)

    def _compute_consensus(self, votes: list[AgentVote]) -> ConsensusResult:
        """
        Compute weighted consensus from all votes.

        Weighted vote = conviction × agent_accuracy × base_weight
        """
        up_weight = 0.0
        down_weight = 0.0
        abstain_count = 0
        active_votes = []

        for vote in votes:
            if vote.vote == Vote.ABSTAIN or vote.is_muted:
                abstain_count += 1
                continue

            effective_weight = vote.effective_conviction
            active_votes.append(vote)

            if vote.vote == Vote.UP:
                up_weight += effective_weight
            else:
                down_weight += effective_weight

        total_weight = up_weight + down_weight

        # No meaningful votes
        if total_weight < 0.01:
            return ConsensusResult(
                direction="NEUTRAL",
                strength=0.0,
                agreement_ratio=0.0,
                votes=votes,
                up_weight=0.0,
                down_weight=0.0,
                abstain_count=abstain_count,
            )

        # Determine direction
        if up_weight > down_weight:
            direction = "UP"
            dominant_weight = up_weight
            agreeing = [v for v in active_votes if v.vote == Vote.UP]
        else:
            direction = "DOWN"
            dominant_weight = down_weight
            agreeing = [v for v in active_votes if v.vote == Vote.DOWN]

        # Strength: normalized dominance
        strength = dominant_weight / total_weight

        # Agreement ratio: fraction of active agents that agree
        agreement_ratio = len(agreeing) / len(active_votes) if active_votes else 0.0

        logger.debug(
            f"Consensus: {direction} strength={strength:.2f}, "
            f"agreement={agreement_ratio:.0%}, "
            f"up={up_weight:.2f}, down={down_weight:.2f}, "
            f"abstain={abstain_count}"
        )

        return ConsensusResult(
            direction=direction,
            strength=strength,
            agreement_ratio=agreement_ratio,
            votes=votes,
            up_weight=up_weight,
            down_weight=down_weight,
            abstain_count=abstain_count,
        )

    def record_outcome(self, actual_direction: str, votes: list[AgentVote]) -> None:
        """
        Record trade outcome for all agents (for meta-learner).
        Call this after each window resolves.
        """
        for vote in votes:
            if vote.vote != Vote.ABSTAIN and not vote.is_muted:
                self._meta.record_outcome(
                    agent_name=vote.agent_name,
                    vote=vote.vote.value,
                    actual_direction=actual_direction,
                    conviction=vote.conviction,
                )
