"""
The Forge — Statistical Benchmarking (Vitalis Hotpath Edition)

Primary: delegates heavy math to Vitalis native Rust hotpaths (<1µs per op).
Fallback: pure Python Welford & order statistics if DLL unavailable.

All statistical operations (p95, stddev, confidence intervals, Welch's t-test,
outlier detection, Pareto dominance) run with numerical stability.
"""

from __future__ import annotations

import math

from .models import BenchmarkResult
from . import compiler as _c     # hotpath functions live here


# ── Core Benchmark Computation ────────────────────────────────────────────────

def welford_stats(timings: list[float]) -> tuple[float, float]:
    """Numerically stable Welford's online algorithm for mean and sample stddev."""
    valid = [t for t in timings if math.isfinite(t)]
    n = len(valid)
    if n == 0:
        return 0.0, 0.0
    mean = 0.0
    m2 = 0.0
    for i, x in enumerate(valid, 1):
        delta = x - mean
        mean += delta / i
        delta2 = x - mean
        m2 += delta * delta2
    stddev = math.sqrt(m2 / (n - 1)) if n > 1 else 0.0
    return mean, stddev


def compute_benchmark(timings: list[float]) -> BenchmarkResult:
    """
    Compute full statistical profile from a list of run timings (ms).
    Uses Vitalis native Rust hotpaths when available, with Welford fallback.
    """
    valid = [t for t in timings if math.isfinite(t)]
    if not valid:
        return BenchmarkResult(runs=0)

    n = len(valid)
    s = sorted(valid)

    # ── Native Rust stats or Welford fallback ─────────────────────────
    if _c._ensure_ffi():
        mean = _c.hotpath_mean(valid)
        stddev = _c.hotpath_stddev(valid) if n > 1 else 0.0
    else:
        mean, stddev = welford_stats(valid)

    p50 = _c.hotpath_percentile(valid, 0.50)
    p95 = _c.hotpath_p95(valid)
    p99 = _c.hotpath_percentile(valid, 0.99)

    mn, mx = s[0], s[-1]

    # 95% confidence interval via Student's t distribution
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


def compute_ci_95(timings: list[float]) -> tuple[float, float]:
    """Compute the 95% Student's t confidence interval (low, high) for a list of timings."""
    res = compute_benchmark(timings)
    return res.confidence_interval_95


def welch_t_test(a: BenchmarkResult, b: BenchmarkResult, min_runs: int = 2) -> dict:
    """
    Welch's t-test between two benchmark results.
    Returns significance, effect size (Cohen's d), and winner.
    """
    if a.runs < max(2, min_runs) or b.runs < max(2, min_runs):
        return {
            "t_statistic": 0.0,
            "degrees_of_freedom": 0.0,
            "significant": False,
            "cohens_d": 0.0,
            "winner": "tie",
        }

    var_a, var_b = a.stddev_ms ** 2, b.stddev_ms ** 2
    na, nb = a.runs, b.runs

    if var_a == 0.0 and var_b == 0.0:
        if abs(a.mean_ms - b.mean_ms) < 1e-9:
            return {
                "t_statistic": 0.0,
                "degrees_of_freedom": float(na + nb - 2),
                "significant": False,
                "cohens_d": 0.0,
                "winner": "tie",
            }
        winner = "a" if a.mean_ms < b.mean_ms else "b"
        return {
            "t_statistic": float("inf") if a.mean_ms > b.mean_ms else float("-inf"),
            "degrees_of_freedom": float(na + nb - 2),
            "significant": True,
            "cohens_d": float("inf"),
            "winner": winner,
        }

    se = math.sqrt(var_a / na + var_b / nb) or 1e-10
    t_stat = (a.mean_ms - b.mean_ms) / se

    num = (var_a / na + var_b / nb) ** 2
    denom = ((var_a / na) ** 2 / (na - 1) + (var_b / nb) ** 2 / (nb - 1)) or 1e-10
    df = num / denom
    t_crit = _t_crit_95(max(2, int(round(df)) + 1))

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
    """MAD-based outlier detection (modified Z-score) with mean-deviation fallback when MAD == 0."""
    valid = [t for t in timings if math.isfinite(t)]
    if len(valid) < 3:
        return []
    median = _c.hotpath_median(valid)
    deviations = [abs(x - median) for x in valid]
    mad = _c.hotpath_median(deviations)
    if mad == 0:
        mean_ad = sum(deviations) / len(deviations)
        if mean_ad == 0:
            return []
        scale = 1.253314 * mean_ad
        return [
            i for i, t in enumerate(timings)
            if math.isfinite(t) and abs(t - median) / scale > threshold
        ]
    return [
        i for i, t in enumerate(timings)
        if math.isfinite(t) and abs(0.6745 * (t - median) / mad) > threshold
    ]


def compute_fitness_performance(candidate_ms: float, all_times_ms: list[float], jitter_tolerance_ms: float = 0.5) -> float:
    """
    Percentile rank of candidate (0=worst, 1=best). Lower time = better.
    Includes a sub-millisecond OS timer jitter tolerance band so identical-complexity
    candidates are not penalized by microsecond clock noise.
    """
    if not all_times_ms or not math.isfinite(candidate_ms):
        return 0.0
    valid = [t for t in all_times_ms if math.isfinite(t)]
    return sum(1 for t in valid if (t + jitter_tolerance_ms) >= candidate_ms) / len(valid) if valid else 0.0


# ── Multi-Objective Fitness ───────────────────────────────────────────────────

def compute_pareto_front(submission_objective_vectors: list[tuple[str, list[float]]]) -> list[str]:
    """
    Compute the Pareto front (non-dominated submissions) across multiple objectives.
    """
    if not submission_objective_vectors:
        return []

    ids = [sid for sid, _ in submission_objective_vectors]
    vectors = [v for _, v in submission_objective_vectors]

    front_indices = _c.hotpath_pareto_front(vectors)
    return [ids[i] for i in front_indices if 0 <= i < len(ids)]


def adaptive_fitness_score(
    speed: float,
    correctness: float,
    complexity: float,
    security: float,
    generation: int,
) -> float:
    """
    Multi-objective adaptive fitness with generation-shifting weights.
    Early gens: correctness + simplicity first.
    Late gens: speed + security first.
    """
    return _c.hotpath_adaptive_fitness(speed, correctness, complexity, security, generation)


def weighted_composite(metrics: list[float], weights: list[float]) -> float:
    """Compute weighted composite score via native Rust or Python fallback (clamped to [0,1])."""
    return _c.hotpath_weighted_score(metrics, weights)


def measure_diversity(fitness_scores: list[float]) -> float:
    """
    Shannon diversity of the population fitness distribution.
    1.0 = maximum diversity. 0.0 = everyone converged to same score.
    """
    if not fitness_scores:
        return 0.0
    probs = _c.hotpath_softmax([f / 100.0 for f in fitness_scores])
    return _c.hotpath_shannon_diversity(probs)


def native_code_quality(source_code: str) -> float:
    """
    Score code quality using Vitalis's native hotpath scorer.
    Measures: cyclomatic complexity, cognitive complexity, LOC, security patterns.
    """
    lines = source_code.strip().split("\n")
    body = [l for l in lines if l.strip() and not l.strip().startswith("//")]

    cyclomatic = 1 + sum(
        1 for l in body
        if any(kw in l for kw in ("if ", "else ", "while ", "for ", "match ", "loop "))
    )
    cognitive = sum(
        len(l) - len(l.lstrip())
        for l in body
    ) // max(len(body), 1)
    loc = len(body)
    num_fns = source_code.count("fn ")
    sec_issues = sum(
        1 for p in ("unsafe", "raw_pointer", "transmute", "forget")
        if p in source_code
    )
    has_tests = "#[test]" in source_code or "fn test_" in source_code

    raw = _c.hotpath_code_quality_score(
        float(cyclomatic), float(cognitive), float(loc),
        float(num_fns), float(sec_issues), has_tests
    )
    # Handle both 0-100 scale and 0-1 scale defensively
    return raw / 100.0 if raw > 1.0 else max(0.0, min(1.0, raw))


# ── EMA Fitness Trend Tracking ────────────────────────────────────────────────

class FitnessTrend:
    """Exponential moving average tracker for champion fitness per generation."""

    def __init__(self, alpha: float = 0.3):
        self._ema: float = 0.0
        self._alpha = alpha
        self._history: list[float] = []

    def update(self, new_fitness: float) -> float:
        if not self._history:
            self._ema = float(new_fitness)
        else:
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


# ── KLS Isoperimetric & Poincaré Spectral Diagnostics (Song & Zhang 2026) ────

def kls_spectral_diagnostics(
    embeddings: list[list[float]],
    fitness_scores: list[float],
) -> dict:
    """
    Compute empirical KLS (Kannan-Lovász-Simonovits) and Poincaré spectral diagnostics
    over a population's embedding manifold (Song & Zhang 2026, arXiv:2610.01447; Lean 4 KLS500).

    For an isotropic log-concave distribution mu in R^d:
        Var_mu(f) <= C_Poincare * E_mu[||grad f||^2]
    where C_Poincare = O(1) <= 500 independent of dimension d, and the KLS constant
    psi_n = 1 / (2 * sqrt(C_Poincare)) bounds the minimum hyperplane cut bottleneck.

    Returns:
        {
            "dimension": int,
            "sample_size": int,
            "fitness_variance": float,
            "dirichlet_gradient_energy": float,
            "poincare_ratio": float,
            "kls_hyperplane_conductance": float,
            "isotropic_condition_number": float,
            "kls500_bound_satisfied": bool,
        }
    """
    n = min(len(embeddings), len(fitness_scores))
    if n < 2 or not embeddings or not embeddings[0]:
        return {
            "dimension": len(embeddings[0]) if embeddings and embeddings[0] else 0,
            "sample_size": n,
            "fitness_variance": 0.0,
            "dirichlet_gradient_energy": 0.0,
            "poincare_ratio": 0.0,
            "kls_hyperplane_conductance": 1.0,
            "isotropic_condition_number": 1.0,
            "kls500_bound_satisfied": True,
        }

    dim = len(embeddings[0])
    vecs = embeddings[:n]
    fits = [float(f) for f in fitness_scores[:n]]

    # 1. Center and isotropize coordinates (isotropic preconditioning)
    coord_means = [sum(vecs[i][j] for i in range(n)) / n for j in range(dim)]
    coord_vars = [
        sum((vecs[i][j] - coord_means[j]) ** 2 for i in range(n)) / max(n - 1, 1)
        for j in range(dim)
    ]
    active_vars = [v for v in coord_vars if v > 1e-12]
    cond_num = (max(active_vars) / min(active_vars)) if active_vars else 1.0

    iso_vecs: list[list[float]] = []
    for i in range(n):
        iso_vecs.append([
            (vecs[i][j] - coord_means[j]) / math.sqrt(coord_vars[j])
            if coord_vars[j] > 1e-12 else 0.0
            for j in range(dim)
        ])

    # 2. Empirical fitness variance Var_mu(f) (normalized to [0, 1] scale)
    norm_fits = [f / 100.0 if f > 1.0 else f for f in fits]
    f_mean = sum(norm_fits) / n
    f_var = sum((f - f_mean) ** 2 for f in norm_fits) / max(n - 1, 1)

    # 3. Discrete Dirichlet gradient energy E_mu[||grad_G f||^2] over isotropic manifold
    grad_sq_terms: list[float] = []
    for i in range(n):
        best_dist_sq = float("inf")
        best_df_sq = 0.0
        for k in range(n):
            if i == k:
                continue
            dist_sq = sum((iso_vecs[i][j] - iso_vecs[k][j]) ** 2 for j in range(dim)) / max(dim, 1)
            df_sq = (norm_fits[i] - norm_fits[k]) ** 2
            if 1e-9 < dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_df_sq = df_sq
        if math.isfinite(best_dist_sq) and best_dist_sq > 1e-9:
            grad_sq_terms.append(best_df_sq / best_dist_sq)

    dirichlet_energy = (sum(grad_sq_terms) / len(grad_sq_terms)) if grad_sq_terms else 0.0
    if dirichlet_energy > 1e-9:
        poincare_ratio = f_var / dirichlet_energy
    else:
        poincare_ratio = 0.0 if f_var < 1e-9 else min(500.0, f_var * 100.0)

    # 4. Empirical KLS hyperplane cut conductance via Silverman KDE at coordinate medians
    min_conductance = float("inf")
    h_bw = max(0.25, 1.06 * (n ** -0.2))
    inv_sqrt_2pi = 0.3989422804014327
    for j in range(dim):
        if coord_vars[j] <= 1e-12:
            continue
        col = sorted(iso_vecs[i][j] for i in range(n))
        med_j = col[n // 2]
        # Kernel density estimate of marginal boundary measure at hyperplane x_j = med_j
        kde_boundary = sum(
            math.exp(-0.5 * ((iso_vecs[i][j] - med_j) / h_bw) ** 2)
            for i in range(n)
        ) * inv_sqrt_2pi / (n * h_bw)
        # Half-space volume is 1/2 at the median cut, so Cheeger conductance = boundary / 0.5
        conductance = 2.0 * kde_boundary
        if conductance < min_conductance:
            min_conductance = conductance

    if not math.isfinite(min_conductance):
        min_conductance = 1.0

    return {
        "dimension": dim,
        "sample_size": n,
        "fitness_variance": round(f_var, 6),
        "dirichlet_gradient_energy": round(dirichlet_energy, 6),
        "poincare_ratio": round(poincare_ratio, 4),
        "kls_hyperplane_conductance": round(min_conductance, 4),
        "isotropic_condition_number": round(cond_num, 4),
        "kls500_bound_satisfied": poincare_ratio <= 500.0,
    }



# ── Helpers ───────────────────────────────────────────────────────────────────

def _t_crit_95(n: int) -> float:
    """Critical t-value for 95% CI, two-tailed (df = n - 1)."""
    if n <= 1:
        return 0.0
    table = {
        2: 12.706,
        3: 4.303,
        4: 3.182,
        5: 2.776,
        6: 2.571,
        8: 2.365,
        10: 2.262,
        15: 2.145,
        20: 2.093,
        25: 2.064,
        30: 2.045,
        50: 2.009,
        100: 1.984,
    }
    for threshold in sorted(table):
        if n <= threshold:
            return table[threshold]
    return 1.96
