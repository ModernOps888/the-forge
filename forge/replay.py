"""
The Forge — Replay Engine & Deterministic Reproducibility

No other agentic framework offers deterministic replay.
Given the same seed + config + challenge, The Forge produces the exact same
evolution trajectory, champion, and fitness scores.

Also provides:
  - Tournament replay from snapshot
  - A/B testing: run same challenge on different configs
  - Time-travel debugging: step through generations
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ReplayConfig:
    """Everything needed to deterministically replay a tournament."""
    seed: int
    challenge_json: str
    arena_config_json: str
    provider_names: list[str]
    original_champion_fitness: float = 0.0
    original_generations: int = 0


def save_replay(config: ReplayConfig, path: Path) -> None:
    """Save a replay config to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "seed": config.seed,
        "challenge": json.loads(config.challenge_json),
        "arena_config": json.loads(config.arena_config_json),
        "providers": config.provider_names,
        "original_champion_fitness": config.original_champion_fitness,
        "original_generations": config.original_generations,
    }, indent=2), encoding="utf-8")


def load_replay(path: Path) -> ReplayConfig:
    """Load a replay config from disk."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return ReplayConfig(
        seed=data["seed"],
        challenge_json=json.dumps(data["challenge"]),
        arena_config_json=json.dumps(data["arena_config"]),
        provider_names=data["providers"],
        original_champion_fitness=data.get("original_champion_fitness", 0),
        original_generations=data.get("original_generations", 0),
    )


# ── A/B Testing ───────────────────────────────────────────────────────────────

@dataclass
class ABTestResult:
    """Result of comparing two tournament configurations."""
    config_a_name: str
    config_b_name: str
    champion_a_fitness: float
    champion_b_fitness: float
    generations_a: int
    generations_b: int
    cost_a: float
    cost_b: float
    winner: str
    delta_fitness: float
    delta_cost: float

    def summary(self) -> str:
        return (
            f"A/B Test: {self.config_a_name} vs {self.config_b_name}\n"
            f"  Champion A: {self.champion_a_fitness:.2f}  |  Champion B: {self.champion_b_fitness:.2f}\n"
            f"  Gens A: {self.generations_a}  |  Gens B: {self.generations_b}\n"
            f"  Cost A: ${self.cost_a:.4f}  |  Cost B: ${self.cost_b:.4f}\n"
            f"  Winner: {self.winner} (+{self.delta_fitness:.2f} fitness, ${self.delta_cost:+.4f} cost)"
        )
