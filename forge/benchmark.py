"""
The Forge — Statistical Benchmarking

Implements Welford's online algorithm for computing running mean/variance,
confidence intervals, outlier detection, and statistical comparison between
two candidates (Welch's t-test).

No numpy dependency — pure Python for zero-dep deployment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import BenchmarkResult


@dataclass
class WelfordAccumulator:
    """Welford's online algorithm for numerically stable mean/variance."""
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def update(self, value: float) -> None:
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        delta2 = value - self.mean
        self.m2 += delta * delta2

    @property
    def variance(self) -> float:
        return self.m2 / self.n if self.n > 1 else 0.0

    @property
    def sample_variance(self) -> float:
        return self.m2 / (self.n - 1) if self.n > 1 else 0.0

    @property
    def stddev(self) -> float:
        return math.sqrt(self.variance)

    @property
    def sample_stddev(self) -> float:
        return math.sqrt(self.sample_variance)


def compute_benchmark(timings: list[float]) -> BenchmarkResult:
    """
    Compute full benchmark statistics from a list of timings (in ms).
    Filters out infinite values (failed runs).
    """
    # Filter out failed runs
    valid = [t for t in timings if math.isfinite(t)]
    if not valid:
        return BenchmarkResult(runs=0)

    # Welford accumulator
    acc = WelfordAccumulator()
    for t in valid:
        acc.update(t)

    # Percentiles (sorted)
    sorted_t = sorted(valid)
    n = len(sorted_t)

    def percentile(p: float) -> float:
        idx = p * (n - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return sorted_t[lo]
        frac = idx - lo
        return sorted_t[lo] * (1 - frac) + sorted_t[hi] * frac

    # 95% confidence interval (Student's t approximation)
    # For n >= 30, t ≈ 1.96; for smaller n, use lookup
    t_values = {
        5: 2.776, 10: 2.262, 15: 2.145, 20: 2.093,
        25: 2.064, 30: 2.045, 50: 2.009, 100: 1.984,
    }
    t_crit = 1.96  # default for large n
    for threshold in sorted(t_values.keys()):
        if n <= threshold:
            t_crit = t_values[threshold]
            break

    margin = t_crit * (acc.sample_stddev / math.sqrt(n)) if n > 1 else 0.0
    ci_lower = acc.mean - margin
    ci_upper = acc.mean + margin

    return BenchmarkResult(
        runs=n,
        mean_ms=round(acc.mean, 3),
        stddev_ms=round(acc.sample_stddev, 3),
        p50_ms=round(percentile(0.50), 3),
        p95_ms=round(percentile(0.95), 3),
        p99_ms=round(percentile(0.99), 3),
        min_ms=round(min(valid), 3),
        max_ms=round(max(valid), 3),
        peak_memory_kb=0.0,  # TODO: measure via /proc or perf counters
        confidence_interval_95=(round(ci_lower, 3), round(ci_upper, 3)),
    )


def welch_t_test(a: BenchmarkResult, b: BenchmarkResult) -> dict:
    """
    Welch's t-test: are two benchmark results statistically different?
    
    Returns:
        t_statistic: the t value
        degrees_of_freedom: Welch-Satterthwaite approximation
        significant: bool (is p < 0.05?)
        cohens_d: effect size
        winner: "a", "b", or "tie"
    """
    if a.runs < 2 or b.runs < 2:
        return {"t_statistic": 0, "degrees_of_freedom": 0,
                "significant": False, "cohens_d": 0, "winner": "tie"}

    var_a = a.stddev_ms ** 2
    var_b = b.stddev_ms ** 2
    n_a, n_b = a.runs, b.runs

    # Standard error
    se = math.sqrt(var_a / n_a + var_b / n_b) if (var_a + var_b) > 0 else 1e-10

    # t statistic
    t_stat = (a.mean_ms - b.mean_ms) / se

    # Welch-Satterthwaite degrees of freedom
    num = (var_a / n_a + var_b / n_b) ** 2
    denom = ((var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1))
    df = num / denom if denom > 0 else 1

    # Critical t for p < 0.05 (two-tailed, approximation)
    t_crit = 1.96 if df > 120 else 2.0

    # Cohen's d effect size
    pooled_sd = math.sqrt((var_a + var_b) / 2) if (var_a + var_b) > 0 else 1e-10
    cohens_d = abs(a.mean_ms - b.mean_ms) / pooled_sd

    significant = abs(t_stat) > t_crit

    if not significant:
        winner = "tie"
    elif a.mean_ms < b.mean_ms:
        winner = "a"  # lower time = faster = winner
    else:
        winner = "b"

    return {
        "t_statistic": round(t_stat, 4),
        "degrees_of_freedom": round(df, 1),
        "significant": significant,
        "cohens_d": round(cohens_d, 4),
        "winner": winner,
    }


def detect_outliers(timings: list[float], threshold: float = 3.5) -> list[int]:
    """
    Detect outliers using Modified Z-Score (MAD-based).
    Returns indices of outlier values.
    """
    valid = [t for t in timings if math.isfinite(t)]
    if len(valid) < 3:
        return []

    median = sorted(valid)[len(valid) // 2]
    deviations = [abs(x - median) for x in valid]
    mad = sorted(deviations)[len(deviations) // 2]

    if mad == 0:
        return []

    outliers = []
    for i, t in enumerate(timings):
        if math.isfinite(t):
            modified_z = 0.6745 * (t - median) / mad
            if abs(modified_z) > threshold:
                outliers.append(i)

    return outliers


def compute_fitness_performance(candidate_ms: float, all_times_ms: list[float]) -> float:
    """
    Score performance relative to the cohort (0.0 = worst, 1.0 = best).
    Uses percentile rank.
    """
    if not all_times_ms or not math.isfinite(candidate_ms):
        return 0.0

    valid = sorted([t for t in all_times_ms if math.isfinite(t)])
    if not valid:
        return 0.0

    # Lower time = better, so count how many are slower
    slower_count = sum(1 for t in valid if t >= candidate_ms)
    return slower_count / len(valid)
