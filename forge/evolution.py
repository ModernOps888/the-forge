"""
The Forge — Code Evolution Engine (Vitalis Hotpath Edition)

Uses Vitalis native Rust hotpaths (with exact pure-Python math fallbacks) for selection and exploration:
  - Boltzmann selection probabilities       (hotpath_boltzmann_select)
  - Quantum annealing acceptance            (hotpath_quantum_anneal_accept)
  - Bayesian UCB explore/exploit            (hotpath_bayesian_ucb)
  - Lévy flight mutation distances          (hotpath_levy_step)
  - CMA-ES distribution mean update        (hotpath_cma_es_mean_update)
  - Pareto dominance checks                 (hotpath_pareto_dominates)
  - Code novelty via cosine similarity      (hotpath_cosine_similarity)
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import zlib
from dataclasses import dataclass
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


# ── Function Structure Helpers ────────────────────────────────────────────────

def _split_function(source: str) -> tuple[list[str], list[str], list[str]]:
    """
    Split a .sl function into (header_lines, inner_body_lines, footer_lines)
    so crossover and mutation operators never corrupt the `fn ... {` signature
    or the closing `}` brace.
    """
    lines = source.strip().split("\n")
    if not lines:
        return [], [], []

    # Single-line function e.g. `fn main() -> i64 { 42 }`
    if len(lines) == 1 and "{" in lines[0] and "}" in lines[0]:
        open_idx = lines[0].index("{")
        close_idx = lines[0].rindex("}")
        header = [lines[0][: open_idx + 1]]
        inner = [lines[0][open_idx + 1 : close_idx].strip()] if lines[0][open_idx + 1 : close_idx].strip() else []
        footer = [lines[0][close_idx:]]
        return header, inner, footer

    bs = _body_start(lines)
    # Find the matching closing brace from the end
    fe = len(lines) - 1
    for i in range(len(lines) - 1, bs, -1):
        if "}" in lines[i]:
            fe = i
            break

    header = lines[: bs + 1]
    inner = lines[bs + 1 : fe]
    footer = lines[fe:]
    return header, inner, footer


def _reconstruct_function(header: list[str], inner: list[str], footer: list[str]) -> str:
    return "\n".join(header + inner + footer)


# ── Crossover Operators ───────────────────────────────────────────────────────

def crossover_uniform(a: str, b: str) -> str:
    """
    Uniform crossover: randomly pick each inner body line from parent A or B.
    Preserves parent A's function signature and closing brace.
    """
    ha, body_a, fa = _split_function(a)
    _, body_b, _ = _split_function(b)
    if not body_a or not body_b:
        return a

    max_len = max(len(body_a), len(body_b))
    child = []
    for i in range(max_len):
        if i < len(body_a) and i < len(body_b):
            child.append(random.choice([body_a[i], body_b[i]]))
        elif i < len(body_a):
            child.append(body_a[i])
        else:
            child.append(body_b[i])

    candidate = _reconstruct_function(ha, child, fa)
    # Ensure balanced braces; fallback to parent A if mismatched
    if candidate.count("{") != candidate.count("}"):
        return a
    return candidate


def crossover_single_point(a: str, b: str) -> str:
    """Single-point crossover at a random inner body line."""
    ha, body_a, fa = _split_function(a)
    _, body_b, _ = _split_function(b)
    if len(body_a) < 2 or len(body_b) < 2:
        return a
    pt_a = random.randint(1, len(body_a) - 1)
    pt_b = random.randint(1, len(body_b) - 1)
    candidate = _reconstruct_function(ha, body_a[:pt_a] + body_b[pt_b:], fa)
    if candidate.count("{") != candidate.count("}"):
        return a
    return candidate


def crossover_ast_aware(a: str, b: str) -> str:
    """
    AST-aware crossover: swaps entire logical brace-depth blocks between A and B
    inside the function body to prevent syntax collisions.
    """
    ha, body_a, fa = _split_function(a)
    _, body_b, _ = _split_function(b)

    if len(body_a) < 2 or len(body_b) < 2:
        return crossover_uniform(a, b)

    def get_blocks(lines: list[str]) -> list[tuple[int, int]]:
        blocks: list[tuple[int, int]] = []
        depth = 0
        start = -1
        for i, line in enumerate(lines):
            opens = line.count("{")
            closes = line.count("}")
            if opens > 0 and depth == 0:
                start = i
            depth += opens - closes
            if depth <= 0 and start != -1:
                blocks.append((start, i))
                start = -1
                depth = 0
        return blocks

    blocks_a = get_blocks(body_a)
    blocks_b = get_blocks(body_b)

    if not blocks_a or not blocks_b:
        return crossover_single_point(a, b)

    ba = random.choice(blocks_a)
    bb = random.choice(blocks_b)

    child_body = body_a[: ba[0]] + body_b[bb[0] : bb[1] + 1] + body_a[ba[1] + 1 :]
    candidate = _reconstruct_function(ha, child_body, fa)
    if candidate.count("{") != candidate.count("}"):
        return a
    return candidate


# ── Mutation Operators ────────────────────────────────────────────────────────

def mutate_swap_lines(src: str) -> tuple[str, str]:
    """Swap two adjacent inner body statements (excluding block headers/braces)."""
    header, body, footer = _split_function(src)
    if len(body) < 2:
        return src, "no_op"
    swappable = [
        i for i in range(len(body) - 1)
        if "{" not in body[i] and "}" not in body[i]
        and "{" not in body[i + 1] and "}" not in body[i + 1]
        and not body[i].strip().startswith("return ")
    ]
    if not swappable:
        return src, "no_op"
    idx = random.choice(swappable)
    body[idx], body[idx + 1] = body[idx + 1], body[idx]
    return _reconstruct_function(header, body, footer), f"swap@L{len(header) + idx}"


def mutate_insert_guard(src: str) -> tuple[str, str]:
    """Insert a type-safe early-return guard clause matching the function's parameters."""
    header, body, footer = _split_function(src)
    if not header:
        return src, "no_op"

    sig_line = " ".join(header)
    m_sig = re.search(r"\bfn\s+\w+\s*\(([^)]*)\)\s*(?:->\s*([^{]+))?", sig_line)
    if not m_sig:
        return src, "no_op"

    raw_params = m_sig.group(1).strip()
    ret_type = (m_sig.group(2) or "i64").strip()
    if not raw_params:
        return src, "no_op"

    first_param = raw_params.split(",")[0].strip()
    pname = first_param.split(":")[0].replace("mut ", "").strip()
    ptype = first_param.split(":")[1].strip() if ":" in first_param else "i64"

    if "[" in ptype and "[" in ret_type:
        guards = [
            f"    if {pname}.len() == 0 {{ return []; }}",
            f"    if {pname}.len() < 2 {{ return {pname}; }}",
        ]
    elif ptype in ("i64", "i32") and ret_type in ("i64", "i32"):
        guards = [
            f"    if {pname} <= 0 {{ return 0; }}",
            f"    if {pname} == 0 {{ return 0; }}",
        ]
    else:
        return src, "no_op"

    chosen = random.choice(guards)
    if chosen.strip() in [b.strip() for b in body]:
        return src, "no_op"

    body.insert(0, chosen)
    return _reconstruct_function(header, body, footer), f"guard@L{len(header)}"


def mutate_change_constant(src: str) -> tuple[str, str]:
    """Alter a numeric literal inside the function body by a Lévy-flight step."""
    header, body, footer = _split_function(src)
    if not body:
        return src, "no_op"
    body_str = "\n".join(body)
    matches = list(re.finditer(r"\b(\d+)\b", body_str))
    if not matches:
        return src, "no_op"
    m = random.choice(matches)
    old = int(m.group())
    u = random.gauss(0, 1)
    v = random.gauss(0, 1)
    raw_step = _c.hotpath_levy_step(u, v, beta=1.5, scale=2.0)
    step = (int(min(abs(raw_step), 10.0)) if math.isfinite(raw_step) else 1) + 1
    new = max(0, old + random.choice([-step, step]))
    new_body_str = body_str[: m.start()] + str(new) + body_str[m.end() :]
    return _reconstruct_function(header, new_body_str.split("\n"), footer), f"const_{old}->{new}"


def mutate_add_comment(src: str) -> tuple[str, str]:
    """Inject a descriptive comment inside the function body (drives novelty score)."""
    tags = ["optimized", "guard", "pivot", "accumulator", "base case", "hot path"]
    header, body, footer = _split_function(src)
    if not header:
        return src, "no_op"
    at = random.randint(0, max(0, len(body)))
    body.insert(at, f"    // {random.choice(tags)}")
    return _reconstruct_function(header, body, footer), f"comment@L{len(header) + at}"


def mutate_rename_variable(src: str) -> tuple[str, str]:
    """Rename a local `let` variable inside the function body without corrupting identifiers."""
    header, body, footer = _split_function(src)
    if not body:
        return src, "no_op"
    body_str = "\n".join(body)
    matches = list(re.finditer(r"\blet\s+(?:mut\s+)?([a-zA-Z_]\w*)\b", body_str))
    if not matches:
        return src, "no_op"
    m = random.choice(matches)
    old_name = m.group(1)
    # Strip only a trailing `_[vxrtk]` suffix if present, never arbitrary letters from the stem
    base_name = re.sub(r"_[vxrtk]$", "", old_name)
    if not base_name:
        return src, "no_op"
    suffixes = ["_v", "_x", "_r", "_t", "_k"]
    candidates = [base_name + s for s in suffixes if base_name + s != old_name]
    new_name = random.choice(candidates)
    new_body_str = re.sub(r"\b" + re.escape(old_name) + r"\b", new_name, body_str)
    return _reconstruct_function(header, new_body_str.split("\n"), footer), f"rename_{old_name}->{new_name}"


_MUTATIONS = [
    mutate_swap_lines,
    mutate_insert_guard,
    mutate_change_constant,
    mutate_add_comment,
    mutate_rename_variable,
]


# ── Selection — Vitalis Hotpath Powered ───────────────────────────────────────

def boltzmann_select(population: list[Submission], temperature: float = 1.2) -> Submission:
    """
    Boltzmann (softmax) selection — temperature controls exploration/exploitation.
    T→0: greedy. T→∞: uniform random.
    """
    if not population:
        raise ValueError("Cannot select from an empty population")
    scored = [s for s in population if s.fitness is not None]
    if not scored:
        return random.choice(population)

    fitnesses = [s.fitness.total for s in scored]
    probs = _c.hotpath_boltzmann_select(fitnesses, temperature)

    r = random.random()
    cumulative = 0.0
    for s, p in zip(scored, probs):
        cumulative += p
        if r <= cumulative:
            return s
    return scored[-1]


def bayesian_ucb_select(
    population: list[Submission],
    trial_counts: dict[str, int],
    total_trials: int,
    kappa: float = 1.414,
) -> Submission:
    """
    Bayesian UCB1 selection: balance fitness (exploitation) with under-tried (exploration).
    """
    if not population:
        raise ValueError("Cannot select from an empty population")
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
    return sorted(scored, key=lambda s: s.fitness.total, reverse=True)[: max(0, n)]


def quantum_anneal_accept(old_fitness: float, new_fitness: float, temperature: float) -> bool:
    """
    Quantum-inspired annealing: always accepts improvements,
    probabilistically accepts regressions (for escaping local optima).
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
    Produce the next generation:
    1. Elite passthrough (top N unchanged)
    2. Boltzmann + Bayesian UCB parent selection
    3. Crossover (uniform, single-point, AST-aware)
    4. Quantum-annealed acceptance
    5. Lévy-flight mutation
    """
    if not parents:
        return []

    trial_counts = trial_counts if trial_counts is not None else {}
    total_trials = sum(trial_counts.values()) or 1
    anneal_temp = config.anneal_start_temp * (config.anneal_decay ** generation_number)
    select_temp = max(0.1, config.boltzmann_temp * (config.anneal_decay ** (generation_number * 0.5)))
    offspring: list[Submission] = []

    # 1. Elites — pass through unchanged
    elites = elite_select(parents, config.elite_count)
    for e in elites:
        offspring.append(Submission(
            challenge_id=challenge_id,
            generation=generation_number,
            provider=e.provider,
            source_code=e.source_code,
            parent_a_id=e.id,
            mutations=["elite"],
            status=SubmissionStatus.PENDING,
        ))
        trial_counts[e.id] = trial_counts.get(e.id, 0) + 1

    # 2. Fill remaining slots
    remaining = max(0, config.population_size - len(offspring))
    for _ in range(remaining):
        if random.random() < 0.6:
            pa = boltzmann_select(parents, select_temp)
        else:
            pa = bayesian_ucb_select(parents, trial_counts, total_trials, config.ucb_kappa)

        pb = boltzmann_select(parents, select_temp)

        mutations_applied: list[str] = []
        child_code = pa.source_code

        # 3. Crossover
        if random.random() < config.crossover_rate and pa.id != pb.id:
            op = random.choices(
                [crossover_uniform, crossover_single_point, crossover_ast_aware],
                weights=[0.4, 0.2, 0.4],
            )[0]
            child_code = op(pa.source_code, pb.source_code)
            mutations_applied.append(f"xover_{op.__name__.split('_')[1]}")

        # 4. Quantum-annealed acceptance of the crossover
        if mutations_applied and pa.fitness is not None and pb.fitness is not None:
            baseline_fitness = min(pa.fitness.total, pb.fitness.total)
            est_child = (pa.fitness.total + pb.fitness.total) / 2.0
            if not quantum_anneal_accept(baseline_fitness, est_child, anneal_temp):
                best_parent = pa if pa.fitness.total >= pb.fitness.total else pb
                child_code = best_parent.source_code
                mutations_applied = ["anneal_rejected"]

        # 5. Mutation
        if random.random() < config.mutation_rate:
            u, v = random.gauss(0, 1), random.gauss(0, 1)
            raw_levy = _c.hotpath_levy_step(u, v, config.levy_beta, config.levy_scale)
            levy_mag = abs(raw_levy) if math.isfinite(raw_levy) else 1.0
            n_mutations = 1 + int(min(levy_mag, 3.0))
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
    Uses cosine similarity on character n-gram vectors.
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
    return max(0.0, min(1.0, 1.0 - avg_similarity))


def code_fingerprint(source: str) -> str:
    """SHA-256 fingerprint of normalized source (no whitespace/comments)."""
    lines = [
        l.strip() for l in source.strip().split("\n")
        if l.strip() and not l.strip().startswith("//")
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def _ngram_vector(source: str, n: int = 3, dim: int = 64) -> list[float]:
    """Deterministic character n-gram vector using CRC32 (reproducible across processes)."""
    vec = [0.0] * dim
    src = source.replace(" ", "").replace("\n", "")
    for i in range(len(src) - n + 1):
        gram = src[i : i + n]
        idx = zlib.crc32(gram.encode("utf-8")) % dim
        vec[idx] += 1.0
    mag = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / mag for x in vec]


def _body_start(lines: list[str]) -> int:
    """Find the line index of the first '{' (function body start)."""
    for i, line in enumerate(lines):
        if "{" in line:
            return i
    return 0
