"""
The Forge — Code Evolution Engine (Vitalis Hotpath Edition)

Uses Vitalis native Rust hotpaths for all selection and exploration math:
  - Boltzmann selection probabilities       (hotpath_boltzmann_select)
  - Quantum annealing acceptance            (hotpath_quantum_anneal_accept)
  - Bayesian UCB explore/exploit            (hotpath_bayesian_ucb)
  - Lévy flight mutation distances          (hotpath_levy_step)
  - CMA-ES distribution mean update        (hotpath_cma_es_mean_update)
  - Pareto dominance checks                 (hotpath_pareto_dominates)
  - Code novelty via cosine similarity      (hotpath_cosine_similarity)

Python handles only: source code string manipulation (crossover, mutation text ops).
All numerical selection/exploration math runs in native Rust.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Optional

from .models import Submission, Provider, SubmissionStatus
from . import compiler as _c   # hotpath fns


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class EvolutionConfig:
    population_size: int = 50
    elite_count: int = 10
    crossover_rate: float = 0.70
    mutation_rate: float = 0.35
    tournament_k: int = 5
    boltzmann_temp: float = 1.2        # selection temperature
    anneal_start_temp: float = 2.0     # quantum annealing start
    anneal_decay: float = 0.92         # temperature decay per generation
    levy_beta: float = 1.5             # Lévy flight exponent
    levy_scale: float = 0.3            # Lévy step scale
    ucb_kappa: float = 1.414           # Bayesian UCB exploration constant
    novelty_threshold: float = 0.10    # min code distance to count as novel
    seed: Optional[int] = None

    def __post_init__(self):
        if self.seed is not None:
            random.seed(self.seed)


# ── Crossover Operators ───────────────────────────────────────────────────────

def crossover_uniform(a: str, b: str) -> str:
    """
    Uniform crossover: randomly pick each body line from parent A or B.
    Preserves parent A's function signature.
    """
    la = a.strip().split("\n")
    lb = b.strip().split("\n")
    bs_a = _body_start(la)
    bs_b = _body_start(lb)
    header = la[:bs_a]
    body_a, body_b = la[bs_a:], lb[bs_b:]
    max_len = max(len(body_a), len(body_b))
    child = []
    for i in range(max_len):
        if i < len(body_a) and i < len(body_b):
            child.append(random.choice([body_a[i], body_b[i]]))
        elif i < len(body_a):
            child.append(body_a[i])
        else:
            child.append(body_b[i])
    return "\n".join(header + child)


def crossover_single_point(a: str, b: str) -> str:
    """Single-point crossover at a random body line."""
    la = a.strip().split("\n")
    lb = b.strip().split("\n")
    bs_a = _body_start(la)
    bs_b = _body_start(lb)
    body_a, body_b = la[bs_a:], lb[bs_b:]
    if len(body_a) < 2 or len(body_b) < 2:
        return a
    pt_a = random.randint(1, len(body_a) - 1)
    pt_b = random.randint(1, len(body_b) - 1)
    return "\n".join(la[:bs_a] + body_a[:pt_a] + body_b[pt_b:])


def crossover_ast_aware(a: str, b: str) -> str:
    """
    AST-aware crossover: swaps entire logical brace-depth blocks between A and B
    to prevent syntax collisions compared to raw multi-point cuts.
    """
    la = a.strip().split("\n")
    lb = b.strip().split("\n")
    bs_a = _body_start(la)
    bs_b = _body_start(lb)
    body_a, body_b = la[bs_a:], lb[bs_b:]
    
    if len(body_a) < 4 or len(body_b) < 4:
        return crossover_uniform(a, b)
        
    def get_blocks(lines):
        blocks = []
        depth = 0
        start = -1
        for i, line in enumerate(lines):
            if '{' in line:
                if depth == 0: start = i
                depth += 1
            if '}' in line:
                depth -= 1
                if depth == 0 and start != -1:
                    blocks.append((start, i))
                    start = -1
        return blocks

    blocks_a = get_blocks(body_a)
    blocks_b = get_blocks(body_b)
    
    if not blocks_a or not blocks_b:
        return crossover_single_point(a, b)
        
    ba = random.choice(blocks_a)
    bb = random.choice(blocks_b)
    
    child_body = body_a[:ba[0]] + body_b[bb[0]:bb[1]+1] + body_a[ba[1]+1:]
    return "\n".join(la[:bs_a] + child_body)


# ── Mutation Operators ────────────────────────────────────────────────────────

def mutate_swap_lines(src: str) -> tuple[str, str]:
    """Swap two adjacent body lines."""
    lines = src.strip().split("\n")
    bs = _body_start(lines)
    body = lines[bs:]
    if len(body) < 3:
        return src, "no_op"
    idx = random.randint(0, len(body) - 3)
    body[idx], body[idx + 1] = body[idx + 1], body[idx]
    return "\n".join(lines[:bs] + body), f"swap@L{bs + idx}"


def mutate_insert_guard(src: str) -> tuple[str, str]:
    """Insert an early-return guard clause after the opening brace."""
    guards = [
        "    if arr.len() == 0 { return []; }",
        "    if arr.len() < 2 { return arr; }",
        "    if n < 0 { return 0; }",
        "    if n == 0 { return 1; }",
        "    if n < 0 { return -1; }",
    ]
    lines = src.strip().split("\n")
    bs = _body_start(lines)
    lines.insert(bs + 1, random.choice(guards))
    return "\n".join(lines), f"guard@L{bs + 1}"


def mutate_change_constant(src: str) -> tuple[str, str]:
    """Alter a numeric literal by a Lévy-flight step."""
    import re
    matches = list(re.finditer(r'\b(\d+)\b', src))
    if not matches:
        return src, "no_op"
    m = random.choice(matches)
    old = int(m.group())
    # Lévy step magnitude
    u = random.gauss(0, 1)
    v = random.gauss(0, 1)
    step = int(abs(_c.hotpath_levy_step(u, v, beta=1.5, scale=2.0))) + 1
    new = max(0, old + random.choice([-step, step]))
    return src[:m.start()] + str(new) + src[m.end():], f"const_{old}->{new}"


def mutate_add_comment(src: str) -> tuple[str, str]:
    """Inject a descriptive comment (drives novelty score)."""
    tags = ["optimized", "guard", "pivot", "accumulator", "base case", "hot path"]
    lines = src.strip().split("\n")
    bs = _body_start(lines)
    if len(lines) - bs < 2:
        return src, "no_op"
    at = random.randint(bs + 1, len(lines) - 1)
    lines.insert(at, f"    // {random.choice(tags)}")
    return "\n".join(lines), f"comment@L{at}"


def mutate_rename_variable(src: str) -> tuple[str, str]:
    """Rename a common variable for code distance purposes."""
    import re
    # Find let bindings
    matches = list(re.finditer(r'\blet\s+(?:mut\s+)?(\w+)\b', src))
    if not matches:
        return src, "no_op"
    m = random.choice(matches)
    old_name = m.group(1)
    suffixes = ["_v", "_x", "_r", "_t", "_k"]
    new_name = old_name.rstrip("_vtrkx") + random.choice(suffixes)
    result = re.sub(r'\b' + re.escape(old_name) + r'\b', new_name, src)
    return result, f"rename_{old_name}->{new_name}"


_MUTATIONS = [mutate_swap_lines, mutate_insert_guard, mutate_change_constant,
              mutate_add_comment, mutate_rename_variable]


# ── Selection — Vitalis Hotpath Powered ───────────────────────────────────────

def boltzmann_select(population: list[Submission], temperature: float = 1.2) -> Submission:
    """
    Boltzmann (softmax) selection — temperature controls exploration/exploitation.
    T→0: greedy. T→∞: uniform random. Implemented in native Rust.
    """
    scored = [s for s in population if s.fitness is not None]
    if not scored:
        return random.choice(population)

    fitnesses = [s.fitness.total for s in scored]
    probs = _c.hotpath_boltzmann_select(fitnesses, temperature)

    # Weighted random choice
    r = random.random()
    cumulative = 0.0
    for s, p in zip(scored, probs):
        cumulative += p
        if r <= cumulative:
            return s
    return scored[-1]


def bayesian_ucb_select(population: list[Submission], trial_counts: dict[str, int],
                         total_trials: int, kappa: float = 1.414) -> Submission:
    """
    Bayesian UCB1 selection: balance fitness (exploitation) with under-tried (exploration).
    Functions tried fewer times get an exploration bonus. Native Rust.
    """
    scored = [s for s in population if s.fitness is not None]
    if not scored:
        return random.choice(population)

    ucb_scores = []
    for s in scored:
        n_tried = trial_counts.get(s.id, 1)
        ucb = _c.hotpath_bayesian_ucb(s.fitness.total / 100.0, n_tried, max(total_trials, 1), kappa)
        ucb_scores.append(ucb)

    best_idx = max(range(len(ucb_scores)), key=lambda i: ucb_scores[i])
    return scored[best_idx]


def elite_select(population: list[Submission], n: int) -> list[Submission]:
    """Return the top N individuals by composite fitness."""
    scored = [s for s in population if s.fitness is not None]
    return sorted(scored, key=lambda s: s.fitness.total, reverse=True)[:n]


def quantum_anneal_accept(old_fitness: float, new_fitness: float,
                           temperature: float) -> bool:
    """
    Quantum-inspired annealing: always accepts improvements,
    probabilistically accepts regressions (for escaping local optima).
    Native Rust.
    """
    return _c.hotpath_quantum_anneal_accept(old_fitness, new_fitness, temperature)


# ── Generation Pipeline ───────────────────────────────────────────────────────

def evolve_generation(
    parents: list[Submission],
    challenge_id: str,
    generation_number: int,
    config: EvolutionConfig,
    trial_counts: Optional[dict[str, int]] = None,
) -> list[Submission]:
    """
    Produce the next generation.
    
    1. Elite passthrough (top N unchanged)
    2. Boltzmann parent selection
    3. Crossover (uniform, single-point, two-point — randomly chosen)
    4. Quantum-annealed acceptance (accept regressions at high temperature)
    5. Lévy-flight mutation
    6. Return new population

    All selection math delegates to Vitalis native Rust hotpaths.
    """
    if not parents:
        return []

    trial_counts = trial_counts or {}
    total_trials = sum(trial_counts.values()) or 1
    temperature = config.anneal_start_temp * (config.anneal_decay ** generation_number)
    offspring: list[Submission] = []

    # 1. Elites — pass through unchanged
    elites = elite_select(parents, config.elite_count)
    for e in elites:
        offspring.append(Submission(
            challenge_id=challenge_id,
            generation=generation_number,
            provider=Provider.LOCAL,
            source_code=e.source_code,
            parent_a_id=e.id,
            mutations=["elite"],
            status=SubmissionStatus.PENDING,
        ))
        trial_counts[e.id] = trial_counts.get(e.id, 0) + 1

    # 2. Fill remaining slots
    remaining = config.population_size - len(offspring)
    for _ in range(remaining):
        # Select parents via Boltzmann + Bayesian UCB alternating
        if random.random() < 0.6:
            pa = boltzmann_select(parents, temperature)
        else:
            pa = bayesian_ucb_select(parents, trial_counts, total_trials, config.ucb_kappa)

        pb = boltzmann_select(parents, temperature)

        mutations_applied: list[str] = []
        child_code = pa.source_code

        # 3. Crossover
        if random.random() < config.crossover_rate and pa.id != pb.id:
            op = random.choices(
                [crossover_uniform, crossover_single_point, crossover_ast_aware],
                weights=[0.4, 0.2, 0.4]
            )[0]
            child_code = op(pa.source_code, pb.source_code)
            mutations_applied.append(f"xover_{op.__name__.split('_')[1]}")

        # 4. Quantum-annealed acceptance of the crossover
        if mutations_applied and pa.fitness is not None and pb.fitness is not None:
            parent_fitness = max(pa.fitness.total, pb.fitness.total)
            # Estimate child fitness as mix of parents (before benchmarking)
            est_child = (pa.fitness.total + pb.fitness.total) / 2
            if not quantum_anneal_accept(parent_fitness, est_child, temperature):
                child_code = pa.source_code  # reject, revert to best parent
                mutations_applied = ["anneal_rejected"]

        # 5. Mutation
        if random.random() < config.mutation_rate:
            # Lévy flight: sample step magnitude for mutation intensity
            u, v = random.gauss(0, 1), random.gauss(0, 1)
            levy_mag = abs(_c.hotpath_levy_step(u, v, config.levy_beta, config.levy_scale))
            # More mutations if Lévy magnitude is large
            n_mutations = 1 + int(levy_mag)
            for _ in range(min(n_mutations, 3)):
                op = random.choice(_MUTATIONS)
                child_code, desc = op(child_code)
                if desc != "no_op":
                    mutations_applied.append(desc)

        trial_counts[pa.id] = trial_counts.get(pa.id, 0) + 1

        offspring.append(Submission(
            challenge_id=challenge_id,
            generation=generation_number,
            provider=Provider.LOCAL,
            source_code=child_code,
            parent_a_id=pa.id,
            parent_b_id=pb.id if pa.id != pb.id else None,
            mutations=mutations_applied,
            status=SubmissionStatus.PENDING,
        ))

    return offspring


# ── Code Metrics ──────────────────────────────────────────────────────────────

def code_novelty(candidate: str, population_sources: list[str]) -> float:
    """
    Measure how novel candidate code is vs the population.
    Uses cosine similarity on character n-gram vectors via Vitalis hotpath.
    Returns [0, 1] where 1 = completely novel.
    """
    if not population_sources:
        return 1.0

    cand_vec = _ngram_vector(candidate)
    similarities = []
    for src in population_sources:
        other_vec = _ngram_vector(src)
        sim = _c.hotpath_cosine_similarity(cand_vec, other_vec)
        similarities.append(sim)

    avg_similarity = sum(similarities) / len(similarities)
    return 1.0 - avg_similarity  # novelty = 1 - similarity


def code_fingerprint(source: str) -> str:
    """SHA-256 fingerprint of normalized source (no whitespace/comments)."""
    lines = [l.strip() for l in source.strip().split("\n")
             if l.strip() and not l.strip().startswith("//")]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def _ngram_vector(source: str, n: int = 3, dim: int = 64) -> list[float]:
    """Map source code to a fixed-dim float vector via character n-gram hashing."""
    vec = [0.0] * dim
    src = source.replace(" ", "").replace("\n", "")
    for i in range(len(src) - n + 1):
        gram = src[i:i + n]
        vec[hash(gram) % dim] += 1.0
    # L2 normalize
    mag = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / mag for x in vec]


def _body_start(lines: list[str]) -> int:
    """Find the line index of the first '{' (function body start)."""
    for i, line in enumerate(lines):
        if "{" in line:
            return i
    return 0
