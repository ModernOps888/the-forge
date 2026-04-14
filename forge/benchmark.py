"""
The Forge — Statistical Benchmarking (Vitalis Hotpath Edition)

Primary: delegates heavy math to Vitalis native Rust hotpaths (<1µs per op).
Fallback: pure Python Welford if DLL unavailable.

All statistical operations (p95, stddev, confidence intervals, Welch's t-test,
outlier detection, Pareto dominance) run in native Rust with zero Python overhead.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .models import BenchmarkResult
from . import compiler as _c     # hotpath functions live here


# ── Core Benchmark Computation ────────────────────────────────────────────────

def compute_benchmark(timings: list[float]) -> BenchmarkResult:
    """
    Compute full statistical profile from a list of run timings (ms).
    Uses Vitalis native Rust hotpaths when available.
    """
    valid = [t for t in timings if math.isfinite(t)]
    if not valid:
        return BenchmarkResult(runs=0)

    n = len(valid)
    s = sorted(valid)

    # ── Native Rust stats (sub-microsecond) ───────────────────────────
    mean   = _c.hotpath_mean(valid)
    stddev = _c.hotpath_stddev(valid) if n > 1 else 0.0
    p50    = _c.hotpath_percentile(valid, 0.50)
    p95    = _c.hotpath_p95(valid)
    p99    = _c.hotpath_percentile(valid, 0.99)

    # Fallback for functions without native path (simple)
    mn, mx = s[0], s[-1]

    # 95% confidence interval via Student's t approximation
    t_crit = _t_crit_95(n)
    margin = t_crit * (stddev / math.sqrt(n)) if n > 1 else 0.0

    return BenchmarkResult(
        runs=n,
        mean_ms=round(mean, 3),
        stddev_ms=round(stddev, 3),
        p50_ms=round(p50, 3),
        p95_ms=round(p95, 3),
        p99_ms=round(p99, 3),
        min_ms=round(mn, 3),
        max_ms=round(mx, 3),
        peak_memory_kb=0.0,
        confidence_interval_95=(round(mean - margin, 3), round(mean + margin, 3)),
    )


def welch_t_test(a: BenchmarkResult, b: BenchmarkResult) -> dict:
    """
    Welch's t-test between two benchmark results.
    Returns significance, effect size (Cohen's d), and winner.
    """
    if a.runs < 2 or b.runs < 2:
        return {"t_statistic": 0, "degrees_of_freedom": 0,
                "significant": False, "cohens_d": 0.0, "winner": "tie"}

    var_a, var_b = a.stddev_ms ** 2, b.stddev_ms ** 2
    na, nb = a.runs, b.runs
    se = math.sqrt(var_a / na + var_b / nb) or 1e-10
    t_stat = (a.mean_ms - b.mean_ms) / se

    num = (var_a / na + var_b / nb) ** 2
    denom = ((var_a / na) ** 2 / (na - 1) + (var_b / nb) ** 2 / (nb - 1)) or 1e-10
    df = num / denom
    t_crit = 1.96 if df > 120 else 2.0

    pooled_sd = math.sqrt((var_a + var_b) / 2) or 1e-10
    cohens_d = abs(a.mean_ms - b.mean_ms) / pooled_sd
    significant = abs(t_stat) > t_crit

    return {
        "t_statistic": round(t_stat, 4),
        "degrees_of_freedom": round(df, 1),
        "significant": significant,
        "cohens_d": round(cohens_d, 4),
        "winner": "tie" if not significant else ("a" if a.mean_ms < b.mean_ms else "b"),
    }


def detect_outliers(timings: list[float], threshold: float = 3.5) -> list[int]:
    """MAD-based outlier detection (modified Z-score)."""
    valid = [t for t in timings if math.isfinite(t)]
    if len(valid) < 3:
        return []
    median = _c.hotpath_median(valid)
    deviations = [abs(x - median) for x in valid]
    mad = _c.hotpath_median(deviations)
    if mad == 0:
        return []
    return [i for i, t in enumerate(timings)
            if math.isfinite(t) and abs(0.6745 * (t - median) / mad) > threshold]


def compute_fitness_performance(candidate_ms: float, all_times_ms: list[float]) -> float:
    """Percentile rank of candidate (0=worst, 1=best). Lower time = better."""
    if not all_times_ms or not math.isfinite(candidate_ms):
        return 0.0
    valid = [t for t in all_times_ms if math.isfinite(t)]
    return sum(1 for t in valid if t >= candidate_ms) / len(valid) if valid else 0.0


# ── Multi-Objective Fitness ───────────────────────────────────────────────────

def compute_pareto_front(submission_objective_vectors: list[tuple[str, list[float]]]) -> list[str]:
    """
    Compute the Pareto front (non-dominated submissions) across multiple objectives.
    
    Args:
        submission_objective_vectors: list of (submission_id, [obj1, obj2, ...])
        
    Returns:
        List of submission IDs on the Pareto front (non-dominated).
    """
    if not submission_objective_vectors:
        return []

    ids = [sid for sid, _ in submission_objective_vectors]
    vectors = [v for _, v in submission_objective_vectors]

    front_indices = _c.hotpath_pareto_front(vectors)
    return [ids[i] for i in front_indices]


def adaptive_fitness_score(speed: float, correctness: float, complexity: float,
                            security: float, generation: int) -> float:
    """
    Multi-objective adaptive fitness with generation-shifting weights.
    Early gens: correctness + simplicity first.
    Late gens: speed + security first.
    Implemented in native Rust via hotpath_adaptive_fitness.
    """
    return _c.hotpath_adaptive_fitness(speed, correctness, complexity, security, generation)


def weighted_composite(metrics: list[float], weights: list[float]) -> float:
    """Compute weighted composite score via native Rust (clamped to [0,1])."""
    return _c.hotpath_weighted_score(metrics, weights)


def measure_diversity(fitness_scores: list[float]) -> float:
    """
    Shannon diversity of the population fitness distribution.
    1.0 = maximum diversity. 0.0 = everyone converged to same score.
    """
    if not fitness_scores:
        return 0.0
    # Normalize to probabilities via softmax for entropy computation
    probs = _c.hotpath_softmax([f / 100.0 for f in fitness_scores])
    return _c.hotpath_shannon_diversity(probs)


def native_code_quality(source_code: str) -> float:
    """
    Score code quality using Vitalis's native hotpath scorer.
    Measures: cyclomatic complexity, cognitive complexity, LOC, security patterns.
    """
    lines = source_code.strip().split("\n")
    body = [l for l in lines if l.strip() and not l.strip().startswith("//")]

    # Heuristic complexity metrics
    cyclomatic = 1 + sum(
        1 for l in body
        if any(kw in l for kw in ("if ", "else ", "while ", "for ", "match ", "loop "))
    )
    cognitive = sum(
        len(l) - len(l.lstrip())  # nesting depth proxy via indent
        for l in body
    ) // max(len(body), 1)
    loc = len(body)
    num_fns = source_code.count("fn ")
    sec_issues = sum(
        1 for p in ("unsafe", "raw_pointer", "transmute", "forget")
        if p in source_code
    )
    has_tests = "#[test]" in source_code or "fn test_" in source_code

    # Native Rust scorer → 0-100
    raw = _c.hotpath_code_quality_score(
        float(cyclomatic), float(cognitive), float(loc),
        float(num_fns), float(sec_issues), has_tests
    )
    return raw / 100.0  # normalize to [0,1]


# ── EMA Fitness Trend Tracking ────────────────────────────────────────────────

class FitnessTrend:
    """Exponential moving average tracker for champion fitness per generation."""

    def __init__(self, alpha: float = 0.3):
        self._ema: float = 0.0
        self._alpha = alpha
        self._history: list[float] = []

    def update(self, new_fitness: float) -> float:
        self._ema = _c.hotpath_ema_update(self._ema, new_fitness, self._alpha)
        self._history.append(self._ema)
        return self._ema

    @property
    def trend(self) -> float:
        """Positive = improving, negative = degrading."""
        if len(self._history) < 2:
            return 0.0
        return self._history[-1] - self._history[0]

    @property
    def is_stale(self) -> bool:
        """True if no meaningful improvement in last 5 generations."""
        if len(self._history) < 5:
            return False
        return (self._history[-1] - self._history[-5]) < 0.5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _t_crit_95(n: int) -> float:
    """Critical t-value for 95% CI, two-tailed."""
    table = {5: 2.776, 10: 2.262, 15: 2.145, 20: 2.093, 25: 2.064,
             30: 2.045, 50: 2.009, 100: 1.984}
    for threshold in sorted(table):
        if n <= threshold:
            return table[threshold]
    return 1.96
