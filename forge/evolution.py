"""
The Forge — Code Evolution Engine

Crossover, mutation, and selection operators for evolving .sl source code.
Works at the AST level (line-based heuristics) to breed code from different
LLM parents and mutate offspring for diversity.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Optional

from .models import Submission, Provider, SubmissionStatus


@dataclass
class EvolutionConfig:
    """Configuration for the evolution engine."""
    population_size: int = 50
    elite_count: int = 10
    crossover_rate: float = 0.7
    mutation_rate: float = 0.3
    tournament_size: int = 5
    novelty_weight: float = 0.05
    seed: Optional[int] = None

    def __post_init__(self):
        if self.seed is not None:
            random.seed(self.seed)


# ── Crossover Operators ──────────────────────────────────────────

def crossover_single_point(parent_a: str, parent_b: str) -> tuple[str, str]:
    """
    Single-point crossover at function body level.
    Splits both parents at a random line and swaps segments.
    """
    lines_a = parent_a.strip().split("\n")
    lines_b = parent_b.strip().split("\n")

    # Find the function body (skip signature lines)
    body_start_a = _find_body_start(lines_a)
    body_start_b = _find_body_start(lines_b)

    body_a = lines_a[body_start_a:]
    body_b = lines_b[body_start_b:]

    if len(body_a) < 2 or len(body_b) < 2:
        return parent_a, parent_b

    # Crossover point in the body
    point_a = random.randint(1, len(body_a) - 1)
    point_b = random.randint(1, len(body_b) - 1)

    # Offspring 1: A's head + B's tail
    child_1_lines = lines_a[:body_start_a] + body_a[:point_a] + body_b[point_b:]
    # Offspring 2: B's head + A's tail
    child_2_lines = lines_b[:body_start_b] + body_b[:point_b] + body_a[point_a:]

    return "\n".join(child_1_lines), "\n".join(child_2_lines)


def crossover_uniform(parent_a: str, parent_b: str) -> str:
    """
    Uniform crossover: for each line in the body, randomly pick from A or B.
    Produces one offspring.
    """
    lines_a = parent_a.strip().split("\n")
    lines_b = parent_b.strip().split("\n")

    body_start_a = _find_body_start(lines_a)
    body_start_b = _find_body_start(lines_b)

    # Use parent A's signature
    header = lines_a[:body_start_a]
    body_a = lines_a[body_start_a:]
    body_b = lines_b[body_start_b:]

    max_len = max(len(body_a), len(body_b))
    child_body = []

    for i in range(max_len):
        if i < len(body_a) and i < len(body_b):
            child_body.append(random.choice([body_a[i], body_b[i]]))
        elif i < len(body_a):
            child_body.append(body_a[i])
        else:
            child_body.append(body_b[i])

    return "\n".join(header + child_body)


# ── Mutation Operators ───────────────────────────────────────────

def mutate_swap_lines(source: str) -> tuple[str, str]:
    """Swap two random adjacent lines in the function body."""
    lines = source.strip().split("\n")
    body_start = _find_body_start(lines)
    body = lines[body_start:]

    if len(body) < 3:
        return source, "no_op"

    # Don't swap the last line (closing brace)
    idx = random.randint(0, len(body) - 3)
    body[idx], body[idx + 1] = body[idx + 1], body[idx]

    result = "\n".join(lines[:body_start] + body)
    return result, f"swap_lines@L{body_start + idx}"


def mutate_insert_guard(source: str) -> tuple[str, str]:
    """Insert an early-return guard clause for empty/trivial inputs."""
    guards = [
        '    if arr.len() == 0 { return []; }',
        '    if arr.len() < 2 { return arr; }',
        '    if n < 0 { return 0; }',
        '    if n == 0 { return 1; }',
    ]

    lines = source.strip().split("\n")
    body_start = _find_body_start(lines)

    # Insert a random guard after the opening brace
    guard = random.choice(guards)
    lines.insert(body_start + 1, guard)

    result = "\n".join(lines)
    return result, f"insert_guard@L{body_start + 1}"


def mutate_change_constant(source: str) -> tuple[str, str]:
    """Randomly change a numeric constant in the code."""
    import re
    
    # Find all integer constants
    matches = list(re.finditer(r'\b(\d+)\b', source))
    if not matches:
        return source, "no_op"

    match = random.choice(matches)
    old_val = int(match.group())
    
    # Mutate the value
    mutations = [
        old_val + 1,
        old_val - 1,
        old_val * 2,
        old_val // 2 if old_val > 1 else 1,
        random.randint(0, 100),
    ]
    new_val = random.choice(mutations)

    result = source[:match.start()] + str(new_val) + source[match.end():]
    return result, f"change_const_{old_val}_to_{new_val}"


def mutate_add_comment(source: str) -> tuple[str, str]:
    """Add a descriptive comment (cosmetic mutation for novelty)."""
    comments = [
        "    // optimized path",
        "    // guard clause",
        "    // main logic",
        "    // accumulator",
    ]
    lines = source.strip().split("\n")
    body_start = _find_body_start(lines)

    if len(lines) - body_start < 2:
        return source, "no_op"

    insert_at = random.randint(body_start + 1, len(lines) - 1)
    lines.insert(insert_at, random.choice(comments))

    return "\n".join(lines), f"add_comment@L{insert_at}"


# ── Selection ────────────────────────────────────────────────────

def tournament_select(population: list[Submission], k: int = 5) -> Submission:
    """
    Tournament selection: pick k random individuals, return the fittest.
    """
    candidates = random.sample(population, min(k, len(population)))
    scored = [s for s in candidates if s.fitness is not None]
    if not scored:
        return random.choice(candidates)
    return max(scored, key=lambda s: s.fitness.total)


def elite_select(population: list[Submission], n: int = 10) -> list[Submission]:
    """Return the top N individuals by fitness."""
    scored = [s for s in population if s.fitness is not None]
    return sorted(scored, key=lambda s: s.fitness.total, reverse=True)[:n]


# ── Generation Pipeline ─────────────────────────────────────────

def evolve_generation(
    parents: list[Submission],
    challenge_id: str,
    generation_number: int,
    config: EvolutionConfig,
) -> list[Submission]:
    """
    Produce the next generation from the current population.
    
    1. Elite selection (top N survive unchanged)
    2. Tournament selection for crossover parents
    3. Crossover to produce offspring
    4. Mutation on offspring
    5. Return new population
    """
    if not parents:
        return []

    offspring: list[Submission] = []
    
    # 1. Elites pass through unchanged
    elites = elite_select(parents, config.elite_count)
    for elite in elites:
        offspring.append(Submission(
            challenge_id=challenge_id,
            generation=generation_number,
            provider=Provider.LOCAL,
            source_code=elite.source_code,
            parent_a_id=elite.id,
            mutations=["elite_passthrough"],
            status=SubmissionStatus.PENDING,
        ))

    # 2. Fill remaining slots with crossover + mutation
    remaining = config.population_size - len(offspring)

    for _ in range(remaining):
        parent_a = tournament_select(parents, config.tournament_size)
        parent_b = tournament_select(parents, config.tournament_size)

        mutations_applied = []

        # Crossover
        if random.random() < config.crossover_rate and parent_a.id != parent_b.id:
            child_code = crossover_uniform(parent_a.source_code, parent_b.source_code)
            mutations_applied.append("crossover_uniform")
        else:
            child_code = parent_a.source_code

        # Mutation
        if random.random() < config.mutation_rate:
            mutation_ops = [mutate_swap_lines, mutate_insert_guard, mutate_change_constant, mutate_add_comment]
            op = random.choice(mutation_ops)
            child_code, mutation_desc = op(child_code)
            if mutation_desc != "no_op":
                mutations_applied.append(mutation_desc)

        offspring.append(Submission(
            challenge_id=challenge_id,
            generation=generation_number,
            provider=Provider.LOCAL,
            source_code=child_code,
            parent_a_id=parent_a.id,
            parent_b_id=parent_b.id if parent_a.id != parent_b.id else None,
            mutations=mutations_applied,
            status=SubmissionStatus.PENDING,
        ))

    return offspring


def code_distance(a: str, b: str) -> float:
    """
    Compute normalized code distance between two solutions (0=identical, 1=completely different).
    Uses line-level Jaccard distance.
    """
    lines_a = set(a.strip().split("\n"))
    lines_b = set(b.strip().split("\n"))

    if not lines_a and not lines_b:
        return 0.0

    intersection = lines_a & lines_b
    union = lines_a | lines_b

    return 1.0 - (len(intersection) / len(union)) if union else 0.0


def code_fingerprint(source: str) -> str:
    """SHA-256 fingerprint of normalized source (ignoring whitespace/comments)."""
    # Normalize: strip comments and whitespace
    lines = []
    for line in source.strip().split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("//"):
            lines.append(stripped)
    normalized = "\n".join(lines)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ── Helpers ──────────────────────────────────────────────────────

def _find_body_start(lines: list[str]) -> int:
    """Find the line index where the function body starts (first '{')."""
    for i, line in enumerate(lines):
        if "{" in line:
            return i
    return 0
