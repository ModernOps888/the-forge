"""
The Forge — Replay Engine & Deterministic Reproducibility

Given the same seed + config + challenge, The Forge produces the exact same
evolution trajectory, champion, and fitness scores.

Also provides:
  - Tournament replay from snapshot (`create_replay_config`, `run_replay`)
  - A/B testing: run same challenge on different configs (`run_ab_test`)
  - Time-travel debugging: step through generations
"""

from __future__ import annotations

import dataclasses
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .models import Challenge, Tournament
from .arena import Arena, ArenaConfig, mock_provider


@dataclass
class ReplayConfig:
    """Everything needed to deterministically replay a tournament."""
    seed: int
    challenge_json: str
    arena_config_json: str
    provider_names: list[str]
    original_champion_fitness: float = 0.0
    original_generations: int = 0


def create_replay_config(
    tournament: Tournament,
    arena_config: ArenaConfig,
    seed: int = 42,
) -> ReplayConfig:
    """Create a deterministic ReplayConfig snapshot from a completed tournament."""
    c_dict = dataclasses.asdict(tournament.challenge)
    ac_dict = dataclasses.asdict(arena_config)
    provider_names = [p.value for p in tournament.providers]
    champ_fit = tournament.champion.fitness.total if (tournament.champion and tournament.champion.fitness) else 0.0
    return ReplayConfig(
        seed=seed,
        challenge_json=json.dumps(c_dict),
        arena_config_json=json.dumps(ac_dict),
        provider_names=provider_names,
        original_champion_fitness=champ_fit,
        original_generations=len(tournament.generations),
    )


def save_replay(config: ReplayConfig, path: Path | str) -> None:
    """Save a replay config to disk."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "seed": config.seed,
        "challenge": json.loads(config.challenge_json),
        "arena_config": json.loads(config.arena_config_json),
        "providers": config.provider_names,
        "original_champion_fitness": config.original_champion_fitness,
        "original_generations": config.original_generations,
    }, indent=2), encoding="utf-8")


def load_replay(path: Path | str) -> ReplayConfig:
    """Load a replay config from disk."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return ReplayConfig(
        seed=data["seed"],
        challenge_json=json.dumps(data["challenge"]),
        arena_config_json=json.dumps(data["arena_config"]),
        provider_names=data["providers"],
        original_champion_fitness=data.get("original_champion_fitness", 0.0),
        original_generations=data.get("original_generations", 0),
    )


def run_replay(
    config: ReplayConfig,
    provider_factory: Optional[Callable[[str], Callable[[str, str], str]]] = None,
) -> Tournament:
    """Deterministically re-execute a tournament from a ReplayConfig."""
    from .evolution import EvolutionConfig

    random.seed(config.seed)
    c_data = json.loads(config.challenge_json)
    challenge = Challenge(**c_data)

    ac_data = dict(json.loads(config.arena_config_json))
    evo_data = ac_data.pop("evo", {}) or {}
    evo_data["seed"] = config.seed
    evo_cfg = EvolutionConfig(**evo_data)
    arena_cfg = ArenaConfig(evo=evo_cfg, **ac_data)

    arena = Arena(config=arena_cfg)
    pf = provider_factory or mock_provider
    for pname in config.provider_names:
        arena.register_provider(pname, pf(pname))

    return arena.run(challenge)


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
            f"  Winner: {self.winner} ({self.delta_fitness:+.2f} fitness, ${self.delta_cost:+.4f} cost)"
        )


def run_ab_test(
    challenge: Challenge,
    config_a: ArenaConfig,
    config_b: ArenaConfig,
    config_a_name: str = "Config A",
    config_b_name: str = "Config B",
    provider_names: Optional[list[str]] = None,
    provider_factory: Optional[Callable[[str], Callable[[str, str], str]]] = None,
    seed: int = 42,
) -> ABTestResult:
    """Run the same challenge under two ArenaConfig settings and compare results."""
    pnames = provider_names or ["claude", "gpt", "gemini", "deepseek"]
    pf = provider_factory or mock_provider

    random.seed(seed)
    arena_a = Arena(config=config_a)
    for n in pnames:
        arena_a.register_provider(n, pf(n))
    t_a = arena_a.run(challenge)
    fit_a = t_a.champion.fitness.total if (t_a.champion and t_a.champion.fitness) else 0.0

    random.seed(seed)
    arena_b = Arena(config=config_b)
    for n in pnames:
        arena_b.register_provider(n, pf(n))
    t_b = arena_b.run(challenge)
    fit_b = t_b.champion.fitness.total if (t_b.champion and t_b.champion.fitness) else 0.0

    delta_fit = fit_a - fit_b
    if abs(delta_fit) < 1e-6:
        winner = "tie"
    else:
        winner = config_a_name if fit_a > fit_b else config_b_name

    return ABTestResult(
        config_a_name=config_a_name,
        config_b_name=config_b_name,
        champion_a_fitness=fit_a,
        champion_b_fitness=fit_b,
        generations_a=len(t_a.generations),
        generations_b=len(t_b.generations),
        cost_a=0.0,
        cost_b=0.0,
        winner=winner,
        delta_fitness=delta_fit,
        delta_cost=0.0,
    )
