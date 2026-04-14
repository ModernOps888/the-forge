"""
Vitalis — Python wrapper for the native Vitalis compiler + JIT runtime.

Usage:
    import vitalis

    # Compile and JIT-execute .sl source code (returns i64 result)
    result = vitalis.compile_and_run("fn main() -> i64 { 42 }")
    assert result == 42

    # Run a .sl file from disk
    result = vitalis.run_file("examples/hello.sl")

    # Type-check source (returns list of error strings, [] = valid)
    errors = vitalis.check("fn main() -> i64 { 42 }")

    # Parse source → AST debug string
    ast = vitalis.parse_ast("fn main() -> i64 { 1 + 2 }")

    # Lex source → list of (token_kind, text) tuples
    tokens = vitalis.lex("fn main() -> i64 { 42 }")

    # Lower source → IR dump string
    ir = vitalis.dump_ir("fn main() -> i64 { 1 + 2 }")

    # Compiler version
    print(vitalis.version())

    # Hot-path native operations
    p95 = vitalis.hotpath_p95([1.0, 2.0, ..., 100.0])
"""

import ctypes
import json
import os
import sys

# ─── Locate the DLL ─────────────────────────────────────────────────────

def _find_dll():
    """Find vitalis.dll — checks VITALIS_ROOT env var first, then known build paths."""
    vitalis_root = os.environ.get("VITALIS_ROOT", r"C:\Vitalis-V60")
    candidates = [
        # Primary: known Vitalis build location
        os.path.join(vitalis_root, "target", "debug", "vitalis.dll"),
        os.path.join(vitalis_root, "target", "release", "vitalis.dll"),
        # Legacy paths from original vitalis.py
        os.path.join(os.path.dirname(__file__), "vitalis.dll"),
        "vitalis.dll",
    ]
    # Also check via env var
    env_path = os.environ.get("VITALIS_LIB_PATH") or os.environ.get("SLANG_LIB_PATH")
    if env_path:
        candidates.insert(0, env_path)

    for path in candidates:
        if os.path.isfile(path):
            return path

    raise FileNotFoundError(
        f"vitalis.dll not found. Searched:\n" +
        "\n".join(f"  - {p}" for p in candidates) +
        "\n\nBuild with: cd slang && cargo build"
    )


_dll_path = _find_dll()
_lib = ctypes.CDLL(_dll_path)

# ─── Define C function signatures ───────────────────────────────────────

# i64 slang_compile_and_run(const char* source, char** error_out)
_lib.slang_compile_and_run.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
_lib.slang_compile_and_run.restype = ctypes.c_int64

# char* slang_check(const char* source)  — returns allocated string, must free
_lib.slang_check.argtypes = [ctypes.c_char_p]
_lib.slang_check.restype = ctypes.c_void_p

# char* slang_parse_ast(const char* source)
_lib.slang_parse_ast.argtypes = [ctypes.c_char_p]
_lib.slang_parse_ast.restype = ctypes.c_void_p

# char* slang_lex(const char* source)
_lib.slang_lex.argtypes = [ctypes.c_char_p]
_lib.slang_lex.restype = ctypes.c_void_p

# char* slang_dump_ir(const char* source)
_lib.slang_dump_ir.argtypes = [ctypes.c_char_p]
_lib.slang_dump_ir.restype = ctypes.c_void_p

# char* slang_version()
_lib.slang_version.argtypes = []
_lib.slang_version.restype = ctypes.c_void_p

# void slang_free_string(char* ptr)
_lib.slang_free_string.argtypes = [ctypes.c_void_p]
_lib.slang_free_string.restype = None

# void slang_free_error(char* ptr)
_lib.slang_free_error.argtypes = [ctypes.c_void_p]
_lib.slang_free_error.restype = None


# ─── Helpers ─────────────────────────────────────────────────────────────

def _read_and_free(ptr):
    """Read a C string from a void pointer and free it."""
    if not ptr:
        return ""
    result = ctypes.string_at(ptr).decode("utf-8")
    _lib.slang_free_string(ptr)
    return result


# ─── Public API ──────────────────────────────────────────────────────────

def compile_and_run(source: str) -> int:
    """Compile Vitalis source and JIT-execute main(). Returns i64 result."""
    error_ptr = ctypes.c_void_p(None)
    result = _lib.slang_compile_and_run(
        source.encode("utf-8"),
        ctypes.byref(error_ptr),
    )
    if error_ptr.value is not None:
        error_msg = ctypes.string_at(error_ptr.value).decode("utf-8")
        _lib.slang_free_error(error_ptr)
        raise RuntimeError(f"Vitalis compilation error: {error_msg}")
    return result


def run_file(path: str) -> int:
    """Read a .sl file from disk, compile and JIT-execute it."""
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    return compile_and_run(source)


def check(source: str) -> list[str]:
    """Type-check Vitalis source. Returns list of error strings ([] = valid)."""
    ptr = _lib.slang_check(source.encode("utf-8"))
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else []
    except json.JSONDecodeError:
        return [result] if result else []


def parse_ast(source: str) -> str:
    """Parse Vitalis source and return AST as debug string."""
    ptr = _lib.slang_parse_ast(source.encode("utf-8"))
    return _read_and_free(ptr)


def lex(source: str) -> list[tuple[str, str]]:
    """Lex Vitalis source. Returns list of (token_kind, text) tuples."""
    ptr = _lib.slang_lex(source.encode("utf-8"))
    result = _read_and_free(ptr)
    try:
        pairs = json.loads(result) if result else []
        return [(kind, text) for kind, text in pairs]
    except json.JSONDecodeError:
        return []


def dump_ir(source: str) -> str:
    """Lower Vitalis source to IR and return dump string."""
    ptr = _lib.slang_dump_ir(source.encode("utf-8"))
    return _read_and_free(ptr)


def version() -> str:
    """Return the Vitalis compiler version."""
    ptr = _lib.slang_version()
    return _read_and_free(ptr) or "unknown"


# ─── Evolution API ──────────────────────────────────────────────────────

# void slang_evo_load(const char* source)
_lib.slang_evo_load.argtypes = [ctypes.c_char_p]
_lib.slang_evo_load.restype = None

# void slang_evo_register(const char* name, const char* source)
_lib.slang_evo_register.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_lib.slang_evo_register.restype = None

# i64 slang_evo_evolve(const char* name, const char* new_source)
_lib.slang_evo_evolve.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_lib.slang_evo_evolve.restype = ctypes.c_int64

# void slang_evo_set_fitness(const char* name, f64 score)
_lib.slang_evo_set_fitness.argtypes = [ctypes.c_char_p, ctypes.c_double]
_lib.slang_evo_set_fitness.restype = None

# f64 slang_evo_get_fitness(const char* name)
_lib.slang_evo_get_fitness.argtypes = [ctypes.c_char_p]
_lib.slang_evo_get_fitness.restype = ctypes.c_double

# u64 slang_evo_get_generation(const char* name)
_lib.slang_evo_get_generation.argtypes = [ctypes.c_char_p]
_lib.slang_evo_get_generation.restype = ctypes.c_uint64

# char* slang_evo_list()
_lib.slang_evo_list.argtypes = []
_lib.slang_evo_list.restype = ctypes.c_void_p

# i64 slang_evo_run()
_lib.slang_evo_run.argtypes = []
_lib.slang_evo_run.restype = ctypes.c_int64

# char* slang_evo_get_source(const char* name)
_lib.slang_evo_get_source.argtypes = [ctypes.c_char_p]
_lib.slang_evo_get_source.restype = ctypes.c_void_p

# i64 slang_evo_rollback(const char* name, u64 generation)
_lib.slang_evo_rollback.argtypes = [ctypes.c_char_p, ctypes.c_uint64]
_lib.slang_evo_rollback.restype = ctypes.c_int64


def evo_load(source: str) -> None:
    """Load a program into the evolution registry, extracting @evolvable functions."""
    _lib.slang_evo_load(source.encode("utf-8"))


def evo_register(name: str, source: str) -> None:
    """Register a function as evolvable."""
    _lib.slang_evo_register(name.encode("utf-8"), source.encode("utf-8"))


def evo_evolve(name: str, new_source: str) -> int:
    """Submit a new variant. Returns new generation number (-1 on error)."""
    return _lib.slang_evo_evolve(name.encode("utf-8"), new_source.encode("utf-8"))


def evo_set_fitness(name: str, score: float) -> None:
    """Set fitness score for the current variant."""
    _lib.slang_evo_set_fitness(name.encode("utf-8"), score)


def evo_get_fitness(name: str) -> float | None:
    """Get fitness score. Returns None if not set."""
    result = _lib.slang_evo_get_fitness(name.encode("utf-8"))
    import math
    return None if math.isnan(result) else result


def evo_get_generation(name: str) -> int:
    """Get the current generation number for a function."""
    return _lib.slang_evo_get_generation(name.encode("utf-8"))


def evo_list() -> list[str]:
    """List all evolvable function names."""
    ptr = _lib.slang_evo_list()
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else []
    except json.JSONDecodeError:
        return []


def evo_run() -> int:
    """Compile and execute the evolved program. Returns main() result."""
    return _lib.slang_evo_run()


def evo_get_source(name: str) -> str:
    """Get the current source code of an evolvable function."""
    ptr = _lib.slang_evo_get_source(name.encode("utf-8"))
    return _read_and_free(ptr)


def evo_rollback(name: str, generation: int) -> bool:
    """Rollback a function to a previous generation. Returns True on success."""
    return _lib.slang_evo_rollback(name.encode("utf-8"), generation) == 0


# ─── Hot-path API ───────────────────────────────────────────────────────
# Native Rust implementations of performance-critical operations.
# These bypass Vitalis compilation and call Rust code directly.

# usize hotpath_sliding_window_count(const f64*, usize, f64, f64)
_lib.hotpath_sliding_window_count.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.c_double
]
_lib.hotpath_sliding_window_count.restype = ctypes.c_size_t

# f64 hotpath_token_bucket(f64, f64, f64, f64, f64)
_lib.hotpath_token_bucket.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double
]
_lib.hotpath_token_bucket.restype = ctypes.c_double

# f64 hotpath_p95(const f64*, usize)
_lib.hotpath_p95.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_p95.restype = ctypes.c_double

# f64 hotpath_percentile(const f64*, usize, f64)
_lib.hotpath_percentile.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double
]
_lib.hotpath_percentile.restype = ctypes.c_double

# f64 hotpath_mean(const f64*, usize)
_lib.hotpath_mean.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_mean.restype = ctypes.c_double

# f64 hotpath_weighted_score(const f64*, const f64*, usize)
_lib.hotpath_weighted_score.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t
]
_lib.hotpath_weighted_score.restype = ctypes.c_double

# f64 hotpath_code_quality_score(f64, f64, f64, f64, f64, i32)
_lib.hotpath_code_quality_score.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.c_double, ctypes.c_double, ctypes.c_int32
]
_lib.hotpath_code_quality_score.restype = ctypes.c_double

# i32 hotpath_tally_votes(const i32*, usize, f64*)
_lib.hotpath_tally_votes.argtypes = [
    ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)
]
_lib.hotpath_tally_votes.restype = ctypes.c_int32

# char* hotpath_tally_string_votes(const char*)
_lib.hotpath_tally_string_votes.argtypes = [ctypes.c_char_p]
_lib.hotpath_tally_string_votes.restype = ctypes.c_void_p

# u64 hotpath_cognitive_complexity(const u32*, usize)
_lib.hotpath_cognitive_complexity.argtypes = [
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t
]
_lib.hotpath_cognitive_complexity.restype = ctypes.c_uint64

# ─── Quantum-Inspired & Mathematical Optimization Bindings ──────────────

# i32 hotpath_quantum_anneal_accept(f64, f64, f64, f64, f64, f64)
_lib.hotpath_quantum_anneal_accept.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.c_double, ctypes.c_double, ctypes.c_double
]
_lib.hotpath_quantum_anneal_accept.restype = ctypes.c_int32

# f64 hotpath_bayesian_ucb(f64, u64, u64, f64)
_lib.hotpath_bayesian_ucb.argtypes = [
    ctypes.c_double, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_double
]
_lib.hotpath_bayesian_ucb.restype = ctypes.c_double

# void hotpath_boltzmann_select(const f64*, usize, f64, f64*)
_lib.hotpath_boltzmann_select.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.c_double, ctypes.POINTER(ctypes.c_double)
]
_lib.hotpath_boltzmann_select.restype = None

# f64 hotpath_shannon_diversity(const f64*, usize)
_lib.hotpath_shannon_diversity.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t
]
_lib.hotpath_shannon_diversity.restype = ctypes.c_double

# i32 hotpath_pareto_dominates(const f64*, const f64*, usize)
_lib.hotpath_pareto_dominates.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t
]
_lib.hotpath_pareto_dominates.restype = ctypes.c_int32

# usize hotpath_pareto_front(const f64*, usize, usize, u32*)
_lib.hotpath_pareto_front.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_uint32)
]
_lib.hotpath_pareto_front.restype = ctypes.c_size_t

# void hotpath_cma_es_mean_update(const f64*, const f64*, usize, usize, usize, f64*)
_lib.hotpath_cma_es_mean_update.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
    ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double)
]
_lib.hotpath_cma_es_mean_update.restype = None

# f64 hotpath_ema_update(f64, f64, f64)
_lib.hotpath_ema_update.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.hotpath_ema_update.restype = ctypes.c_double

# f64 hotpath_levy_step(f64, f64, f64, f64)
_lib.hotpath_levy_step.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double
]
_lib.hotpath_levy_step.restype = ctypes.c_double

# f64 hotpath_adaptive_fitness(f64, f64, f64, f64, u64)
_lib.hotpath_adaptive_fitness.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_uint64
]
_lib.hotpath_adaptive_fitness.restype = ctypes.c_double

# Phase 21: Vector & statistical ops
_lib.hotpath_cosine_similarity.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_cosine_similarity.restype = ctypes.c_double

_lib.hotpath_l2_normalize.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_l2_normalize.restype = ctypes.c_double

_lib.hotpath_stddev.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_stddev.restype = ctypes.c_double

_lib.hotpath_median.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_median.restype = ctypes.c_double

# Phase 22: Advanced analytics
_lib.hotpath_exponential_moving_average.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.hotpath_exponential_moving_average.restype = ctypes.c_double

_lib.hotpath_entropy.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_entropy.restype = ctypes.c_double

_lib.hotpath_min_max_normalize.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_min_max_normalize.restype = ctypes.c_double

_lib.hotpath_hamming_distance.argtypes = [ctypes.c_int64, ctypes.c_int64]
_lib.hotpath_hamming_distance.restype = ctypes.c_int64

# Phase 23: ML operations
_lib.hotpath_softmax.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_softmax.restype = None

_lib.hotpath_cross_entropy.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_cross_entropy.restype = ctypes.c_double

_lib.hotpath_batch_sigmoid.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_batch_sigmoid.restype = None

_lib.hotpath_argmax.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_argmax.restype = ctypes.c_size_t

_lib.hotpath_batch_relu.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_batch_relu.restype = None

# Phase 24: Advanced ML hotpath ops
_lib.hotpath_batch_leaky_relu.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.hotpath_batch_leaky_relu.restype = None

_lib.hotpath_batch_norm.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.hotpath_batch_norm.restype = ctypes.c_double

_lib.hotpath_kl_divergence.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_kl_divergence.restype = ctypes.c_double

_lib.hotpath_gelu_batch.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_gelu_batch.restype = None

_lib.hotpath_clip.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.c_double]
_lib.hotpath_clip.restype = None


def _to_double_array(values):
    """Convert a list of numbers to a ctypes double array."""
    arr = (ctypes.c_double * len(values))(*values)
    return arr, len(values)


def hotpath_sliding_window_count(timestamps: list[float], now: float, window_seconds: float) -> int:
    """Count timestamps within a sliding window. Native Rust implementation."""
    arr, n = _to_double_array(timestamps)
    return _lib.hotpath_sliding_window_count(arr, n, now, window_seconds)


def hotpath_token_bucket(tokens: float, max_tokens: float, refill_rate: float,
                         elapsed_seconds: float, cost: float) -> float:
    """Token bucket rate limiter. Returns remaining tokens (< 0 = denied). Native Rust."""
    return _lib.hotpath_token_bucket(tokens, max_tokens, refill_rate, elapsed_seconds, cost)


def hotpath_p95(values: list[float]) -> float:
    """Compute 95th percentile of a list. Native Rust implementation."""
    if not values:
        return -1.0
    arr, n = _to_double_array(values)
    return _lib.hotpath_p95(arr, n)


def hotpath_percentile(values: list[float], pct: float) -> float:
    """Compute arbitrary percentile (0.0-1.0) of a list. Native Rust."""
    if not values:
        return -1.0
    arr, n = _to_double_array(values)
    return _lib.hotpath_percentile(arr, n, pct)


def hotpath_mean(values: list[float]) -> float:
    """Compute mean of a list. Native Rust implementation."""
    if not values:
        return 0.0
    arr, n = _to_double_array(values)
    return _lib.hotpath_mean(arr, n)


def hotpath_weighted_score(metrics: list[float], weights: list[float]) -> float:
    """Compute weighted score, clamped to [0, 1]. Native Rust implementation."""
    assert len(metrics) == len(weights), "metrics and weights must have same length"
    m_arr, n = _to_double_array(metrics)
    w_arr, _ = _to_double_array(weights)
    return _lib.hotpath_weighted_score(m_arr, w_arr, n)


def hotpath_code_quality_score(cyclomatic: float, cognitive: float, loc: float,
                                num_functions: float, security_issues: float,
                                has_tests: bool) -> float:
    """Compute code quality score (0-100). Native Rust implementation."""
    return _lib.hotpath_code_quality_score(
        cyclomatic, cognitive, loc, num_functions, security_issues, int(has_tests)
    )


def hotpath_tally_votes(votes: list[int]) -> tuple[int, float]:
    """Tally integer votes. Returns (winner, agreement_pct). Native Rust."""
    if not votes:
        return (-1, 0.0)
    arr = (ctypes.c_int32 * len(votes))(*votes)
    agreement = ctypes.c_double(0.0)
    winner = _lib.hotpath_tally_votes(arr, len(votes), ctypes.byref(agreement))
    return (winner, agreement.value)


def hotpath_tally_string_votes(votes: list[str]) -> dict:
    """Tally string votes. Returns {"winner": str, "agreement": float, "counts": dict}."""
    import json as j
    json_input = j.dumps(votes)
    ptr = _lib.hotpath_tally_string_votes(json_input.encode("utf-8"))
    result = _read_and_free(ptr)
    try:
        return j.loads(result)
    except j.JSONDecodeError:
        return {"winner": "", "agreement": 0.0, "counts": {}}


def hotpath_cognitive_complexity(depths: list[int]) -> int:
    """Compute cognitive complexity from nesting depths. Native Rust."""
    if not depths:
        return 0
    arr = (ctypes.c_uint32 * len(depths))(*depths)
    return _lib.hotpath_cognitive_complexity(arr, len(depths))


# ─── Quantum-Inspired & Mathematical Optimization ───────────────────────

def hotpath_quantum_anneal_accept(old_fitness: float, new_fitness: float,
                                   temperature: float, tunnel_strength: float = 0.1,
                                   barrier_width: float = 1.0,
                                   rand_uniform: float | None = None) -> bool:
    """Quantum-inspired annealing acceptance criterion.

    Combines Metropolis criterion with quantum tunneling to escape local optima.
    Always accepts improvements; probabilistically accepts worse solutions based
    on temperature and tunneling parameters. Native Rust.
    """
    import random
    if rand_uniform is None:
        rand_uniform = random.random()
    result = _lib.hotpath_quantum_anneal_accept(
        old_fitness, new_fitness, temperature,
        tunnel_strength, barrier_width, rand_uniform
    )
    return bool(result)


def hotpath_bayesian_ucb(mean_fitness: float, num_trials: int,
                          total_trials: int, kappa: float = 1.414) -> float:
    """Bayesian UCB1 acquisition score for explore/exploit balance.

    Higher score = should evolve this function next. Functions tried fewer
    times get an exploration bonus. kappa=√2 is theoretically optimal. Native Rust.
    """
    return _lib.hotpath_bayesian_ucb(mean_fitness, num_trials, total_trials, kappa)


def hotpath_boltzmann_select(fitnesses: list[float], temperature: float = 1.0) -> list[float]:
    """Boltzmann (softmax) selection probabilities.

    Temperature controls selection pressure:
    - T→0: greedy (always pick best)
    - T→∞: uniform random
    - T≈1: moderate pressure
    Returns list of selection probabilities that sum to 1.0. Native Rust.
    """
    if not fitnesses:
        return []
    arr, n = _to_double_array(fitnesses)
    probs = (ctypes.c_double * n)()
    _lib.hotpath_boltzmann_select(arr, n, temperature, probs)
    return list(probs)


def hotpath_shannon_diversity(probs: list[float]) -> float:
    """Shannon entropy diversity metric, normalized to [0, 1].

    1.0 = maximum diversity (uniform distribution).
    0.0 = minimum diversity (all weight on one option). Native Rust.
    """
    if len(probs) <= 1:
        return 0.0
    arr, n = _to_double_array(probs)
    return _lib.hotpath_shannon_diversity(arr, n)


def hotpath_pareto_dominates(a: list[float], b: list[float]) -> bool:
    """Check if solution A Pareto-dominates solution B.

    A dominates B iff all objectives of A ≥ B and at least one is strictly >. Native Rust.
    """
    assert len(a) == len(b), "objective vectors must have same length"
    arr_a, n = _to_double_array(a)
    arr_b, _ = _to_double_array(b)
    return bool(_lib.hotpath_pareto_dominates(arr_a, arr_b, n))


def hotpath_pareto_front(solutions: list[list[float]]) -> list[int]:
    """Compute the Pareto front (non-dominated solution indices).

    Input: list of solutions, each a list of objective values (higher = better).
    Returns: indices of non-dominated solutions. Native Rust.
    """
    if not solutions:
        return []
    n_obj = len(solutions[0])
    pop_size = len(solutions)
    flat = []
    for sol in solutions:
        assert len(sol) == n_obj, "all solutions must have same number of objectives"
        flat.extend(sol)
    arr = (ctypes.c_double * len(flat))(*flat)
    out = (ctypes.c_uint32 * pop_size)()
    count = _lib.hotpath_pareto_front(arr, pop_size, n_obj, out)
    return [int(out[i]) for i in range(count)]


def hotpath_cma_es_mean_update(solutions: list[list[float]], fitnesses: list[float],
                                mu: int | None = None) -> list[float]:
    """CMA-ES weighted mean update from best µ solutions.

    Computes the updated distribution mean using log-linear CMA-ES weights
    on the µ best solutions (ranked by fitness). Native Rust.
    """
    if not solutions or not fitnesses:
        return []
    lam = len(solutions)
    dim = len(solutions[0])
    if mu is None:
        mu = max(1, lam // 2)
    flat = []
    for sol in solutions:
        flat.extend(sol)
    sol_arr = (ctypes.c_double * len(flat))(*flat)
    fit_arr, _ = _to_double_array(fitnesses)
    mean_out = (ctypes.c_double * dim)()
    _lib.hotpath_cma_es_mean_update(sol_arr, fit_arr, lam, mu, dim, mean_out)
    return list(mean_out)


def hotpath_ema_update(ema_old: float, new_value: float, alpha: float = 0.3) -> float:
    """Exponential Moving Average update for fitness trend tracking. Native Rust."""
    return _lib.hotpath_ema_update(ema_old, new_value, alpha)


def hotpath_levy_step(u_normal: float, v_normal: float,
                       beta: float = 1.5, scale: float = 1.0) -> float:
    """Lévy flight step magnitude for mutation distance.

    Generates heavy-tailed step sizes: frequent small mutations with
    occasional large jumps. Caller provides pre-generated standard normal
    samples u and v. Beta=1.5 is typical. Native Rust.
    """
    return _lib.hotpath_levy_step(u_normal, v_normal, beta, scale)


def hotpath_adaptive_fitness(speed: float, correctness: float,
                              complexity: float, security: float,
                              generation: int = 0) -> float:
    """Multi-objective adaptive fitness score.

    Combines speed, correctness, complexity, and security scores (each 0-1)
    with generation-adaptive weights:
    - Early generations: prioritize correctness + simplicity
    - Late generations: prioritize speed + security
    Native Rust.
    """
    return _lib.hotpath_adaptive_fitness(speed, correctness, complexity, security, generation)


# ─── Phase 21: Vector & Statistical Operations ─────────────────────────

def hotpath_cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Returns [-1, 1]. Native Rust."""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    arr_a = (ctypes.c_double * n)(*a[:n])
    arr_b = (ctypes.c_double * n)(*b[:n])
    return _lib.hotpath_cosine_similarity(arr_a, arr_b, n)


def hotpath_l2_normalize(values: list[float]) -> tuple[list[float], float]:
    """L2-normalize a vector. Returns (normalized_vector, original_magnitude). Native Rust."""
    if not values:
        return [], 0.0
    arr = (ctypes.c_double * len(values))(*values)
    magnitude = _lib.hotpath_l2_normalize(arr, len(values))
    return list(arr), magnitude


def hotpath_stddev(values: list[float]) -> float:
    """Standard deviation (sample) of values. Native Rust."""
    if len(values) < 2:
        return 0.0
    arr = (ctypes.c_double * len(values))(*values)
    return _lib.hotpath_stddev(arr, len(values))


def hotpath_median(values: list[float]) -> float:
    """Median of values. Native Rust."""
    if not values:
        return 0.0
    arr = (ctypes.c_double * len(values))(*values)
    return _lib.hotpath_median(arr, len(values))


def hotpath_exponential_moving_average(values: list[float], alpha: float = 0.3) -> float:
    """Exponential moving average over a series. Native Rust."""
    if not values:
        return 0.0
    arr = (ctypes.c_double * len(values))(*values)
    return _lib.hotpath_exponential_moving_average(arr, len(values), alpha)


def hotpath_entropy(probs: list[float]) -> float:
    """Shannon entropy of a probability distribution (bits). Native Rust."""
    if not probs:
        return 0.0
    arr = (ctypes.c_double * len(probs))(*probs)
    return _lib.hotpath_entropy(arr, len(probs))


def hotpath_min_max_normalize(values: list[float]) -> tuple[list[float], float]:
    """Min-max normalize a vector. Returns (normalized_values, range). Native Rust."""
    if not values:
        return [], 0.0
    arr = (ctypes.c_double * len(values))(*values)
    data_range = _lib.hotpath_min_max_normalize(arr, len(values))
    return list(arr), data_range


def hotpath_hamming_distance(a: int, b: int) -> int:
    """Hamming distance (differing bits) between two integers. Native Rust."""
    return _lib.hotpath_hamming_distance(a, b)


def hotpath_softmax(values: list[float]) -> list[float]:
    """Softmax over a vector (numerically stable). Native Rust."""
    if not values:
        return []
    arr = (ctypes.c_double * len(values))(*values)
    _lib.hotpath_softmax(arr, len(values))
    return list(arr)


def hotpath_cross_entropy(target: list[float], predicted: list[float]) -> float:
    """Cross-entropy loss. Native Rust."""
    if not target or not predicted:
        return 0.0
    n = min(len(target), len(predicted))
    t_arr = (ctypes.c_double * n)(*target[:n])
    p_arr = (ctypes.c_double * n)(*predicted[:n])
    return _lib.hotpath_cross_entropy(t_arr, p_arr, n)


def hotpath_batch_sigmoid(values: list[float]) -> list[float]:
    """Apply sigmoid to each element. Native Rust."""
    if not values:
        return []
    arr = (ctypes.c_double * len(values))(*values)
    _lib.hotpath_batch_sigmoid(arr, len(values))
    return list(arr)


def hotpath_argmax(values: list[float]) -> int:
    """Index of the maximum value. Native Rust."""
    if not values:
        return 0
    arr = (ctypes.c_double * len(values))(*values)
    return _lib.hotpath_argmax(arr, len(values))


def hotpath_batch_relu(values: list[float]) -> list[float]:
    """Apply ReLU to each element. Native Rust."""
    if not values:
        return []
    arr = (ctypes.c_double * len(values))(*values)
    _lib.hotpath_batch_relu(arr, len(values))
    return list(arr)


# ── Phase 24: Advanced ML hotpath wrappers ─────────────────────────────

def hotpath_batch_leaky_relu(values: list[float], alpha: float = 0.01) -> list[float]:
    """Apply Leaky ReLU (alpha * x for x < 0) to each element. Native Rust."""
    if not values:
        return []
    arr = (ctypes.c_double * len(values))(*values)
    _lib.hotpath_batch_leaky_relu(arr, len(values), alpha)
    return list(arr)


def hotpath_batch_norm(values: list[float], gamma: float = 1.0, beta: float = 0.0, epsilon: float = 1e-5) -> tuple[float, list[float]]:
    """Batch normalize values: (x - mean) / std * gamma + beta. Returns (mean, normalized). Native Rust."""
    if not values:
        return 0.0, []
    arr = (ctypes.c_double * len(values))(*values)
    mean = _lib.hotpath_batch_norm(arr, len(values), gamma, beta, epsilon)
    return mean, list(arr)


def hotpath_kl_divergence(p: list[float], q: list[float]) -> float:
    """KL divergence D_KL(P || Q). Native Rust."""
    n = min(len(p), len(q))
    if n == 0:
        return 0.0
    p_arr = (ctypes.c_double * n)(*p[:n])
    q_arr = (ctypes.c_double * n)(*q[:n])
    return _lib.hotpath_kl_divergence(p_arr, q_arr, n)


def hotpath_gelu_batch(values: list[float]) -> list[float]:
    """Apply GELU activation to each element. Native Rust."""
    if not values:
        return []
    arr = (ctypes.c_double * len(values))(*values)
    _lib.hotpath_gelu_batch(arr, len(values))
    return list(arr)


def hotpath_clip(values: list[float], min_val: float, max_val: float) -> list[float]:
    """Clip/clamp all values to [min_val, max_val]. Native Rust."""
    if not values:
        return []
    arr = (ctypes.c_double * len(values))(*values)
    _lib.hotpath_clip(arr, len(values), min_val, max_val)
    return list(arr)


# ── Phase 25: Numerical Linear Algebra & Loss Operations ────────────────

# Set argtypes/restype at module level for Phase 25 hotpath functions
_lib.hotpath_layer_norm.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.hotpath_layer_norm.restype = ctypes.c_double

_lib.hotpath_dropout_mask.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.c_uint64]
_lib.hotpath_dropout_mask.restype = None

_lib.hotpath_cosine_distance.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_cosine_distance.restype = ctypes.c_double

_lib.hotpath_huber_loss.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.hotpath_huber_loss.restype = ctypes.c_double

_lib.hotpath_mse_loss.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.hotpath_mse_loss.restype = ctypes.c_double

def hotpath_layer_norm(values: list[float], gamma: float = 1.0, beta: float = 0.0, epsilon: float = 1e-5) -> tuple[list[float], float]:
    """Layer normalization: (x - mean) / sqrt(var + eps) * gamma + beta. Returns (normalized, mean). Native Rust."""
    if not values:
        return [], 0.0
    arr = (ctypes.c_double * len(values))(*values)
    mean = _lib.hotpath_layer_norm(arr, len(values), ctypes.c_double(gamma), ctypes.c_double(beta), ctypes.c_double(epsilon))
    return list(arr), mean


def hotpath_dropout_mask(values: list[float], keep_prob: float = 0.8, seed: int = 42) -> list[float]:
    """Apply deterministic dropout mask. Elements are either zeroed or scaled up. Native Rust."""
    if not values:
        return []
    arr = (ctypes.c_double * len(values))(*values)
    _lib.hotpath_dropout_mask(arr, len(values), ctypes.c_double(keep_prob), ctypes.c_uint64(seed))
    return list(arr)


def hotpath_cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine distance: 1 - cosine_similarity. Native Rust."""
    n = min(len(a), len(b))
    if n == 0:
        return 1.0
    a_arr = (ctypes.c_double * n)(*a[:n])
    b_arr = (ctypes.c_double * n)(*b[:n])
    return _lib.hotpath_cosine_distance(a_arr, b_arr, n)


def hotpath_huber_loss(targets: list[float], predicted: list[float], delta: float = 1.0) -> float:
    """Huber loss (smooth L1): robust to outliers. Native Rust."""
    n = min(len(targets), len(predicted))
    if n == 0:
        return 0.0
    t_arr = (ctypes.c_double * n)(*targets[:n])
    p_arr = (ctypes.c_double * n)(*predicted[:n])
    return _lib.hotpath_huber_loss(t_arr, p_arr, n, ctypes.c_double(delta))


def hotpath_mse_loss(targets: list[float], predicted: list[float]) -> float:
    """Mean squared error loss. Native Rust."""
    n = min(len(targets), len(predicted))
    if n == 0:
        return 0.0
    t_arr = (ctypes.c_double * n)(*targets[:n])
    p_arr = (ctypes.c_double * n)(*predicted[:n])
    return _lib.hotpath_mse_loss(t_arr, p_arr, n)



# ============================================================================
# v7.0 Algorithm Libraries - Python wrappers
# ============================================================================

# --- Signal Processing ---

_lib.vitalis_fft.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_fft.restype = ctypes.c_int32
_lib.vitalis_ifft.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_ifft.restype = ctypes.c_int32
_lib.vitalis_power_spectrum.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_power_spectrum.restype = ctypes.c_int32
_lib.vitalis_convolve.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_convolve.restype = ctypes.c_int32
_lib.vitalis_cross_correlate.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_cross_correlate.restype = ctypes.c_int64
_lib.vitalis_window_hann.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_window_hann.restype = None
_lib.vitalis_window_hamming.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_window_hamming.restype = None
_lib.vitalis_window_blackman.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_window_blackman.restype = None
_lib.vitalis_fir_filter.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_fir_filter.restype = ctypes.c_int32
_lib.vitalis_iir_biquad.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_iir_biquad.restype = ctypes.c_int32
_lib.vitalis_zero_crossing_rate.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_zero_crossing_rate.restype = ctypes.c_double
_lib.vitalis_rms_energy.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_rms_energy.restype = ctypes.c_double
_lib.vitalis_spectral_centroid.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_spectral_centroid.restype = ctypes.c_double
_lib.vitalis_autocorrelation.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_autocorrelation.restype = ctypes.c_int32
_lib.vitalis_resample_linear.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_resample_linear.restype = ctypes.c_int32


def fft(real, imag=None):
    n = len(real)
    r = (ctypes.c_double * n)(*real)
    im = (ctypes.c_double * n)(*(imag or [0.0] * n))
    _lib.vitalis_fft(r, im, n)
    return list(r), list(im)

def ifft(real, imag):
    n = len(real)
    r = (ctypes.c_double * n)(*real)
    im = (ctypes.c_double * n)(*imag)
    _lib.vitalis_ifft(r, im, n)
    return list(r), list(im)

def power_spectrum(real, imag):
    n = len(real)
    r = (ctypes.c_double * n)(*real)
    im = (ctypes.c_double * n)(*imag)
    out = (ctypes.c_double * n)()
    _lib.vitalis_power_spectrum(r, im, out, n)
    return list(out)

def convolve(signal, kernel):
    out_len = len(signal) + len(kernel) - 1
    s = (ctypes.c_double * len(signal))(*signal)
    k = (ctypes.c_double * len(kernel))(*kernel)
    out = (ctypes.c_double * out_len)()
    _lib.vitalis_convolve(s, len(signal), k, len(kernel), out)
    return list(out)

def cross_correlate(a, b):
    out_len = len(a) + len(b) - 1
    aa = (ctypes.c_double * len(a))(*a)
    bb = (ctypes.c_double * len(b))(*b)
    out = (ctypes.c_double * out_len)()
    count = _lib.vitalis_cross_correlate(aa, len(a), bb, len(b), out, out_len)
    return list(out[:count])

def window_hann(data):
    arr = (ctypes.c_double * len(data))(*data)
    _lib.vitalis_window_hann(arr, len(data))
    return list(arr)

def window_hamming(data):
    arr = (ctypes.c_double * len(data))(*data)
    _lib.vitalis_window_hamming(arr, len(data))
    return list(arr)

def window_blackman(data):
    arr = (ctypes.c_double * len(data))(*data)
    _lib.vitalis_window_blackman(arr, len(data))
    return list(arr)

def fir_filter(signal, coeffs):
    s = (ctypes.c_double * len(signal))(*signal)
    c = (ctypes.c_double * len(coeffs))(*coeffs)
    out = (ctypes.c_double * len(signal))()
    _lib.vitalis_fir_filter(s, len(signal), c, len(coeffs), out)
    return list(out)

def iir_biquad(signal, b0, b1, b2, a1, a2):
    s = (ctypes.c_double * len(signal))(*signal)
    out = (ctypes.c_double * len(signal))()
    _lib.vitalis_iir_biquad(s, out, len(signal), b0, b1, b2, a1, a2)
    return list(out)

def zero_crossing_rate(data):
    arr, n = _to_double_array(data)
    return _lib.vitalis_zero_crossing_rate(arr, n)

def rms_energy(data):
    arr, n = _to_double_array(data)
    return _lib.vitalis_rms_energy(arr, n)

def spectral_centroid(freq_bins, magnitudes):
    n = min(len(freq_bins), len(magnitudes))
    f = (ctypes.c_double * n)(*freq_bins[:n])
    m = (ctypes.c_double * n)(*magnitudes[:n])
    return _lib.vitalis_spectral_centroid(f, m, n)

def autocorrelation(data, max_lag=None):
    n = len(data)
    lag = max_lag or n
    arr = (ctypes.c_double * n)(*data)
    out = (ctypes.c_double * lag)()
    _lib.vitalis_autocorrelation(arr, n, out, lag)
    return list(out)

def resample_linear(data, out_len):
    arr = (ctypes.c_double * len(data))(*data)
    out = (ctypes.c_double * out_len)()
    _lib.vitalis_resample_linear(arr, len(data), out, out_len)
    return list(out)

# ============================================================================
# v7.0 + v9.0 Algorithm Libraries - Python wrappers
# ============================================================================

# --- Signal Processing ---

_lib.vitalis_fft.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_fft.restype = ctypes.c_int32
_lib.vitalis_ifft.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_ifft.restype = ctypes.c_int32
_lib.vitalis_power_spectrum.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_power_spectrum.restype = ctypes.c_int32
_lib.vitalis_convolve.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_convolve.restype = ctypes.c_int32
_lib.vitalis_cross_correlate.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_cross_correlate.restype = ctypes.c_int64
_lib.vitalis_window_hann.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_window_hann.restype = None
_lib.vitalis_window_hamming.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_window_hamming.restype = None
_lib.vitalis_window_blackman.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_window_blackman.restype = None
_lib.vitalis_fir_filter.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_fir_filter.restype = ctypes.c_int32
_lib.vitalis_iir_biquad.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_iir_biquad.restype = ctypes.c_int32
_lib.vitalis_zero_crossing_rate.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_zero_crossing_rate.restype = ctypes.c_double
_lib.vitalis_rms_energy.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_rms_energy.restype = ctypes.c_double
_lib.vitalis_spectral_centroid.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_spectral_centroid.restype = ctypes.c_double
_lib.vitalis_autocorrelation.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_autocorrelation.restype = ctypes.c_int32
_lib.vitalis_resample_linear.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_resample_linear.restype = ctypes.c_int32

def fft(real, imag=None):
    """In-place FFT. Returns (real, imag)."""
    n = len(real)
    r = (ctypes.c_double * n)(*real)
    im = (ctypes.c_double * n)(*(imag or [0.0] * n))
    _lib.vitalis_fft(r, im, n)
    return list(r), list(im)

def ifft(real, imag):
    """In-place inverse FFT."""
    n = len(real)
    r = (ctypes.c_double * n)(*real)
    im = (ctypes.c_double * n)(*imag)
    _lib.vitalis_ifft(r, im, n)
    return list(r), list(im)

def power_spectrum(real, imag):
    """Power spectrum |X[k]|^2."""
    n = len(real)
    out = (ctypes.c_double * n)()
    _lib.vitalis_power_spectrum((ctypes.c_double * n)(*real), (ctypes.c_double * n)(*imag), out, n)
    return list(out)

def convolve(signal, kernel):
    """Convolve signal with kernel."""
    out_len = len(signal) + len(kernel) - 1
    out = (ctypes.c_double * out_len)()
    _lib.vitalis_convolve((ctypes.c_double * len(signal))(*signal), len(signal), (ctypes.c_double * len(kernel))(*kernel), len(kernel), out)
    return list(out)

def cross_correlate(a, b):
    """Cross-correlate two signals."""
    out_len = len(a) + len(b) - 1
    out = (ctypes.c_double * out_len)()
    count = _lib.vitalis_cross_correlate((ctypes.c_double * len(a))(*a), len(a), (ctypes.c_double * len(b))(*b), len(b), out, out_len)
    return list(out[:count])

def window_hann(data):
    """Apply window hann in-place."""
    arr = (ctypes.c_double * len(data))(*data)
    _lib.vitalis_window_hann(arr, len(data))
    return list(arr)

def window_hamming(data):
    """Apply window hamming in-place."""
    arr = (ctypes.c_double * len(data))(*data)
    _lib.vitalis_window_hamming(arr, len(data))
    return list(arr)

def window_blackman(data):
    """Apply window blackman in-place."""
    arr = (ctypes.c_double * len(data))(*data)
    _lib.vitalis_window_blackman(arr, len(data))
    return list(arr)

def fir_filter(signal, coeffs):
    """FIR filter."""
    out = (ctypes.c_double * len(signal))()
    _lib.vitalis_fir_filter((ctypes.c_double * len(signal))(*signal), len(signal), (ctypes.c_double * len(coeffs))(*coeffs), len(coeffs), out)
    return list(out)

def iir_biquad(signal, b0, b1, b2, a1, a2):
    """IIR biquad filter."""
    out = (ctypes.c_double * len(signal))()
    _lib.vitalis_iir_biquad((ctypes.c_double * len(signal))(*signal), out, len(signal), b0, b1, b2, a1, a2)
    return list(out)

def zero_crossing_rate(data):
    """Zero Crossing Rate."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_zero_crossing_rate(arr, n)

def rms_energy(data):
    """Rms Energy."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_rms_energy(arr, n)

def spectral_centroid(freq_bins, magnitudes):
    """Spectral centroid."""
    n = min(len(freq_bins), len(magnitudes))
    return _lib.vitalis_spectral_centroid((ctypes.c_double * n)(*freq_bins[:n]), (ctypes.c_double * n)(*magnitudes[:n]), n)

def autocorrelation(data, max_lag=None):
    """Autocorrelation up to max_lag."""
    n = len(data)
    lag = max_lag or n
    out = (ctypes.c_double * lag)()
    _lib.vitalis_autocorrelation((ctypes.c_double * n)(*data), n, out, lag)
    return list(out)

def resample_linear(data, out_len):
    """Resample via linear interpolation."""
    out = (ctypes.c_double * out_len)()
    _lib.vitalis_resample_linear((ctypes.c_double * len(data))(*data), len(data), out, out_len)
    return list(out)

# --- Crypto ---

_lib.vitalis_sha256.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_sha256.restype = ctypes.c_void_p
_lib.vitalis_sha256_str.argtypes = [ctypes.c_char_p]
_lib.vitalis_sha256_str.restype = ctypes.c_void_p
_lib.vitalis_hmac_sha256.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_hmac_sha256.restype = ctypes.c_void_p
_lib.vitalis_base64_encode.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_base64_encode.restype = ctypes.c_void_p
_lib.vitalis_base64_decode.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_base64_decode.restype = ctypes.c_int64
_lib.vitalis_crc32.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_crc32.restype = ctypes.c_uint32
_lib.vitalis_fnv1a_64.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_fnv1a_64.restype = ctypes.c_uint64
_lib.vitalis_constant_time_eq.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_constant_time_eq.restype = ctypes.c_int32
_lib.vitalis_xorshift128plus.argtypes = [ctypes.POINTER(ctypes.c_uint64)]
_lib.vitalis_xorshift128plus.restype = ctypes.c_uint64

def sha256(data):
    """SHA-256 hash. Returns hex string."""
    if isinstance(data, str):
        ptr = _lib.vitalis_sha256_str(data.encode("utf-8"))
    else:
        buf = (ctypes.c_uint8 * len(data))(*data)
        ptr = _lib.vitalis_sha256(buf, len(data))
    return _read_and_free(ptr)

def hmac_sha256(key, msg):
    """HMAC-SHA256. Returns hex string."""
    k = (ctypes.c_uint8 * len(key))(*key)
    m = (ctypes.c_uint8 * len(msg))(*msg)
    ptr = _lib.vitalis_hmac_sha256(k, len(key), m, len(msg))
    return _read_and_free(ptr)

def base64_encode(data):
    """Base64 encode bytes."""
    buf = (ctypes.c_uint8 * len(data))(*data)
    ptr = _lib.vitalis_base64_encode(buf, len(data))
    return _read_and_free(ptr)

def base64_decode(encoded):
    """Base64 decode string."""
    max_out = len(encoded)
    out = (ctypes.c_uint8 * max_out)()
    n = _lib.vitalis_base64_decode(encoded.encode("utf-8"), out, max_out)
    return bytes(out[:n])

def crc32(data):
    """CRC-32 checksum."""
    buf = (ctypes.c_uint8 * len(data))(*data)
    return _lib.vitalis_crc32(buf, len(data))

def fnv1a_64(data):
    """FNV-1a 64-bit hash."""
    buf = (ctypes.c_uint8 * len(data))(*data)
    return _lib.vitalis_fnv1a_64(buf, len(data))

def constant_time_eq(a, b):
    """Constant-time equality."""
    n = min(len(a), len(b))
    return _lib.vitalis_constant_time_eq((ctypes.c_uint8 * n)(*a[:n]), (ctypes.c_uint8 * n)(*b[:n]), n) == 1

def xorshift128plus(state):
    """XorShift128+ PRNG."""
    s = (ctypes.c_uint64 * 2)(*state[:2])
    val = _lib.vitalis_xorshift128plus(s)
    return val, list(s)

# --- Graph Algorithms ---

_lib.vitalis_bfs.argtypes = [ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int64)]
_lib.vitalis_bfs.restype = None
_lib.vitalis_dfs.argtypes = [ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_lib.vitalis_dfs.restype = ctypes.c_size_t
_lib.vitalis_dijkstra.argtypes = [ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_dijkstra.restype = None
_lib.vitalis_has_cycle.argtypes = [ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t]
_lib.vitalis_has_cycle.restype = ctypes.c_int32
_lib.vitalis_is_bipartite.argtypes = [ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t]
_lib.vitalis_is_bipartite.restype = ctypes.c_int32
_lib.vitalis_connected_components.argtypes = [ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_lib.vitalis_connected_components.restype = ctypes.c_size_t
_lib.vitalis_toposort.argtypes = [ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_lib.vitalis_toposort.restype = ctypes.c_size_t
_lib.vitalis_pagerank.argtypes = [ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t, ctypes.c_double, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_pagerank.restype = None
_lib.vitalis_tarjan_scc.argtypes = [ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_lib.vitalis_tarjan_scc.restype = ctypes.c_size_t

def _edges_flat_sz(edges):
    flat = []
    for u, v in edges:
        flat.extend([u, v])
    return (ctypes.c_size_t * len(flat))(*flat), len(edges)

def bfs(n_vertices, edges, start):
    """BFS shortest distances from start."""
    e, ne = _edges_flat_sz(edges)
    out = (ctypes.c_int64 * n_vertices)()
    _lib.vitalis_bfs(n_vertices, e, ne, start, out)
    return list(out)

def dfs(n_vertices, edges, start):
    """DFS traversal order."""
    e, ne = _edges_flat_sz(edges)
    out = (ctypes.c_size_t * n_vertices)()
    count = _lib.vitalis_dfs(n_vertices, e, ne, start, out)
    return list(out[:count])

def dijkstra(n_vertices, edges_weighted, start):
    """Dijkstra shortest paths. edges_weighted = [(u,v,w),...]"""
    flat = []
    for u, v, wt in edges_weighted:
        flat.extend([float(u), float(v), wt])
    e = (ctypes.c_double * len(flat))(*flat)
    out = (ctypes.c_double * n_vertices)()
    _lib.vitalis_dijkstra(n_vertices, e, len(edges_weighted), start, out)
    return list(out)

def has_cycle(n_vertices, edges):
    """Check if directed graph has cycle."""
    e, ne = _edges_flat_sz(edges)
    return _lib.vitalis_has_cycle(n_vertices, e, ne) == 1

def is_bipartite(n_vertices, edges):
    """Check if graph is bipartite."""
    e, ne = _edges_flat_sz(edges)
    return _lib.vitalis_is_bipartite(n_vertices, e, ne) == 1

def connected_components(n_vertices, edges):
    """Returns (count, component_ids)."""
    e, ne = _edges_flat_sz(edges)
    out = (ctypes.c_size_t * n_vertices)()
    count = _lib.vitalis_connected_components(n_vertices, e, ne, out)
    return count, list(out)

def toposort(n_vertices, edges):
    """Topological sort."""
    e, ne = _edges_flat_sz(edges)
    out = (ctypes.c_size_t * n_vertices)()
    count = _lib.vitalis_toposort(n_vertices, e, ne, out)
    return list(out[:count])

def pagerank(n_vertices, edges, damping=0.85, iterations=100):
    """PageRank algorithm."""
    e, ne = _edges_flat_sz(edges)
    out = (ctypes.c_double * n_vertices)()
    _lib.vitalis_pagerank(n_vertices, e, ne, damping, iterations, out)
    return list(out)

def tarjan_scc(n_vertices, edges):
    """Tarjan SCC. Returns (count, component_ids)."""
    e, ne = _edges_flat_sz(edges)
    out = (ctypes.c_size_t * n_vertices)()
    count = _lib.vitalis_tarjan_scc(n_vertices, e, ne, out)
    return count, list(out)

# --- String Algorithms ---

_lib.vitalis_levenshtein.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_lib.vitalis_levenshtein.restype = ctypes.c_size_t
_lib.vitalis_lcs_length.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_lib.vitalis_lcs_length.restype = ctypes.c_size_t
_lib.vitalis_lcs_string.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_lib.vitalis_lcs_string.restype = ctypes.c_void_p
_lib.vitalis_longest_common_substring.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_lib.vitalis_longest_common_substring.restype = ctypes.c_size_t
_lib.vitalis_hamming_distance.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_lib.vitalis_hamming_distance.restype = ctypes.c_int64
_lib.vitalis_jaro_winkler.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_double]
_lib.vitalis_jaro_winkler.restype = ctypes.c_double
_lib.vitalis_soundex.argtypes = [ctypes.c_char_p]
_lib.vitalis_soundex.restype = ctypes.c_void_p
_lib.vitalis_is_rotation.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_lib.vitalis_is_rotation.restype = ctypes.c_int32
_lib.vitalis_ngram_count.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
_lib.vitalis_ngram_count.restype = ctypes.c_size_t
_lib.vitalis_kmp_search.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t]
_lib.vitalis_kmp_search.restype = ctypes.c_size_t
_lib.vitalis_rabin_karp.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t]
_lib.vitalis_rabin_karp.restype = ctypes.c_size_t
_lib.vitalis_bmh_search.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t]
_lib.vitalis_bmh_search.restype = ctypes.c_size_t

def levenshtein(a, b):
    """Levenshtein edit distance."""
    return _lib.vitalis_levenshtein(a.encode('utf-8'), b.encode('utf-8'))

def lcs_length(a, b):
    """Longest common subsequence length."""
    return _lib.vitalis_lcs_length(a.encode('utf-8'), b.encode('utf-8'))

def longest_common_substring(a, b):
    """Longest common substring length."""
    return _lib.vitalis_longest_common_substring(a.encode('utf-8'), b.encode('utf-8'))

def lcs_string(a, b):
    """Longest common subsequence string."""
    ptr = _lib.vitalis_lcs_string(a.encode('utf-8'), b.encode('utf-8'))
    return _read_and_free(ptr)

def str_hamming_distance(a, b):
    """Hamming distance between strings."""
    return _lib.vitalis_hamming_distance(a.encode('utf-8'), b.encode('utf-8'))

def jaro_winkler(a, b, prefix_weight=0.1):
    """Jaro-Winkler similarity (0-1)."""
    return _lib.vitalis_jaro_winkler(a.encode('utf-8'), b.encode('utf-8'), prefix_weight)

def soundex(word):
    """Soundex phonetic code."""
    ptr = _lib.vitalis_soundex(word.encode('utf-8'))
    return _read_and_free(ptr)

def is_rotation(a, b):
    """Check if b is rotation of a."""
    return _lib.vitalis_is_rotation(a.encode('utf-8'), b.encode('utf-8')) == 1

def ngram_count(text, n):
    """Count distinct n-grams."""
    return _lib.vitalis_ngram_count(text.encode('utf-8'), n)

def kmp_search(text, pattern, max_results=1000):
    """Kmp Search - returns match positions."""
    out = (ctypes.c_size_t * max_results)()
    count = _lib.vitalis_kmp_search(text.encode('utf-8'), pattern.encode('utf-8'), out, max_results)
    return list(out[:count])

def rabin_karp(text, pattern, max_results=1000):
    """Rabin Karp - returns match positions."""
    out = (ctypes.c_size_t * max_results)()
    count = _lib.vitalis_rabin_karp(text.encode('utf-8'), pattern.encode('utf-8'), out, max_results)
    return list(out[:count])

def bmh_search(text, pattern, max_results=1000):
    """Bmh Search - returns match positions."""
    out = (ctypes.c_size_t * max_results)()
    count = _lib.vitalis_bmh_search(text.encode('utf-8'), pattern.encode('utf-8'), out, max_results)
    return list(out[:count])

# --- Numerical / Linear Algebra ---

_lib.vitalis_mat_mul.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_mat_mul.restype = None
_lib.vitalis_mat_det.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_mat_det.restype = ctypes.c_double
_lib.vitalis_mat_inverse.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_mat_inverse.restype = ctypes.c_int32
_lib.vitalis_solve_linear.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_solve_linear.restype = ctypes.c_int32
_lib.vitalis_simpson.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_simpson.restype = ctypes.c_double
_lib.vitalis_trapezoid.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_trapezoid.restype = ctypes.c_double
_lib.vitalis_horner.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_horner.restype = ctypes.c_double
_lib.vitalis_lagrange_interp.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_lagrange_interp.restype = ctypes.c_double
_lib.vitalis_power_iteration.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_power_iteration.restype = ctypes.c_double
_lib.vitalis_mat_trace.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_mat_trace.restype = ctypes.c_double
_lib.vitalis_mat_frobenius.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_mat_frobenius.restype = ctypes.c_double
_lib.vitalis_dot_product.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_dot_product.restype = ctypes.c_double
_lib.vitalis_vec_norm.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_vec_norm.restype = ctypes.c_double
_lib.vitalis_cross_product.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_cross_product.restype = None
_lib.vitalis_newton_quadratic.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_newton_quadratic.restype = ctypes.c_double
_lib.vitalis_bisection_quadratic.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_bisection_quadratic.restype = ctypes.c_double

def mat_mul(a, b):
    """Matrix multiplication A @ B."""
    m, k, n = len(a), len(a[0]), len(b[0])
    fa = (ctypes.c_double * (m*k))(*[x for row in a for x in row])
    fb = (ctypes.c_double * (k*n))(*[x for row in b for x in row])
    fc = (ctypes.c_double * (m*n))()
    _lib.vitalis_mat_mul(fa, m, k, fb, n, fc)
    return [[fc[i*n+j] for j in range(n)] for i in range(m)]

def mat_det(a):
    """Matrix determinant."""
    n = len(a)
    return _lib.vitalis_mat_det((ctypes.c_double * (n*n))(*[x for row in a for x in row]), n)

def mat_inverse(a):
    """Matrix inverse. Returns None if singular."""
    n = len(a)
    out = (ctypes.c_double * (n*n))()
    ok = _lib.vitalis_mat_inverse((ctypes.c_double * (n*n))(*[x for row in a for x in row]), n, out)
    return [[out[i*n+j] for j in range(n)] for i in range(n)] if ok == 0 else None

def solve_linear(a, b):
    """Solve Ax=b. Returns None if singular."""
    n = len(b)
    fx = (ctypes.c_double * n)()
    ok = _lib.vitalis_solve_linear((ctypes.c_double * (n*n))(*[x for row in a for x in row]), (ctypes.c_double * n)(*b), n, fx)
    return list(fx) if ok == 0 else None

def simpson(values, h):
    """Simpson's rule integration."""
    arr, n = _to_double_array(values)
    return _lib.vitalis_simpson(arr, n, h)

def trapezoid(values, h):
    """Trapezoidal rule integration."""
    arr, n = _to_double_array(values)
    return _lib.vitalis_trapezoid(arr, n, h)

def horner(coeffs, x):
    """Horner polynomial evaluation."""
    arr, n = _to_double_array(coeffs)
    return _lib.vitalis_horner(arr, n, x)

def lagrange_interp(xs, ys, x):
    """Lagrange interpolation."""
    n = min(len(xs), len(ys))
    return _lib.vitalis_lagrange_interp((ctypes.c_double * n)(*xs[:n]), (ctypes.c_double * n)(*ys[:n]), n, x)

def power_iteration(a, max_iter=1000, tol=1e-10):
    """Dominant eigenvalue."""
    n = len(a)
    return _lib.vitalis_power_iteration((ctypes.c_double * (n*n))(*[x for row in a for x in row]), n, max_iter, tol)

def mat_trace(a):
    """Matrix trace."""
    n = len(a)
    return _lib.vitalis_mat_trace((ctypes.c_double * (n*n))(*[x for row in a for x in row]), n)

def dot_product(a, b):
    """Dot product."""
    n = min(len(a), len(b))
    return _lib.vitalis_dot_product((ctypes.c_double * n)(*a[:n]), (ctypes.c_double * n)(*b[:n]), n)

def vec_norm(a):
    """Euclidean norm."""
    arr, n = _to_double_array(a)
    return _lib.vitalis_vec_norm(arr, n)

def cross_product(a, b):
    """Cross product of 3D vectors."""
    out = (ctypes.c_double * 3)()
    _lib.vitalis_cross_product((ctypes.c_double * 3)(*a[:3]), (ctypes.c_double * 3)(*b[:3]), out)
    return list(out)

def newton_root(a, b, c, x0=0.0, max_iter=100, tol=1e-12):
    """Newton root finding for ax^2+bx+c."""
    return _lib.vitalis_newton_quadratic(a, b, c, x0, max_iter, tol)

def bisection_root(a, b, c, lo, hi, max_iter=100, tol=1e-12):
    """Bisection root finding for ax^2+bx+c."""
    return _lib.vitalis_bisection_quadratic(a, b, c, lo, hi, max_iter, tol)

# --- Compression ---

_lib.vitalis_rle_encode.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_rle_encode.restype = ctypes.c_size_t
_lib.vitalis_rle_decode.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_rle_decode.restype = ctypes.c_size_t
_lib.vitalis_huffman_encode.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_huffman_encode.restype = ctypes.c_size_t
_lib.vitalis_delta_encode.argtypes = [ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t, ctypes.POINTER(ctypes.c_int64)]
_lib.vitalis_delta_encode.restype = None
_lib.vitalis_delta_decode.argtypes = [ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t, ctypes.POINTER(ctypes.c_int64)]
_lib.vitalis_delta_decode.restype = None
_lib.vitalis_lz77_compress.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_lz77_compress.restype = ctypes.c_size_t

def rle_encode(data):
    """RLE encode."""
    buf = (ctypes.c_uint8 * len(data))(*data)
    out = (ctypes.c_uint8 * (len(data) * 2 + 1024))()
    n = _lib.vitalis_rle_encode(buf, len(data), out, len(data) * 2 + 1024)
    return bytes(out[:n])

def huffman_encode(data):
    """Huffman encode."""
    buf = (ctypes.c_uint8 * len(data))(*data)
    out = (ctypes.c_uint8 * (len(data) * 2 + 1024))()
    n = _lib.vitalis_huffman_encode(buf, len(data), out, len(data) * 2 + 1024)
    return bytes(out[:n])

def rle_decode(data):
    """RLE decode."""
    buf = (ctypes.c_uint8 * len(data))(*data)
    out = (ctypes.c_uint8 * (len(data) * 256))()
    n = _lib.vitalis_rle_decode(buf, len(data), out, len(data) * 256)
    return bytes(out[:n])

def delta_encode(data):
    """Delta encode integers."""
    n = len(data)
    out = (ctypes.c_int64 * n)()
    _lib.vitalis_delta_encode((ctypes.c_int64 * n)(*data), n, out)
    return list(out)

def delta_decode(data):
    """Delta decode integers."""
    n = len(data)
    out = (ctypes.c_int64 * n)()
    _lib.vitalis_delta_decode((ctypes.c_int64 * n)(*data), n, out)
    return list(out)

def lz77_compress(data, window_size=4096):
    """LZ77 compress."""
    buf = (ctypes.c_uint8 * len(data))(*data)
    out = (ctypes.c_uint8 * (len(data) * 2 + 1024))()
    n = _lib.vitalis_lz77_compress(buf, len(data), out, len(data) * 2 + 1024, window_size)
    return bytes(out[:n])

# --- Probability and Statistics ---

_lib.vitalis_stats_mean.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_stats_mean.restype = ctypes.c_double
_lib.vitalis_stats_median.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_stats_median.restype = ctypes.c_double
_lib.vitalis_stats_variance.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_stats_variance.restype = ctypes.c_double
_lib.vitalis_stats_stddev.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_stats_stddev.restype = ctypes.c_double
_lib.vitalis_stats_skewness.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_stats_skewness.restype = ctypes.c_double
_lib.vitalis_stats_kurtosis.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_stats_kurtosis.restype = ctypes.c_double
_lib.vitalis_stats_mode.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_stats_mode.restype = ctypes.c_double
_lib.vitalis_normal_pdf.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_normal_pdf.restype = ctypes.c_double
_lib.vitalis_normal_cdf.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_normal_cdf.restype = ctypes.c_double
_lib.vitalis_normal_inv_cdf.argtypes = [ctypes.c_double]
_lib.vitalis_normal_inv_cdf.restype = ctypes.c_double
_lib.vitalis_exponential_pdf.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_exponential_pdf.restype = ctypes.c_double
_lib.vitalis_exponential_cdf.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_exponential_cdf.restype = ctypes.c_double
_lib.vitalis_poisson_pmf.argtypes = [ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_poisson_pmf.restype = ctypes.c_double
_lib.vitalis_binomial_pmf.argtypes = [ctypes.c_size_t, ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_binomial_pmf.restype = ctypes.c_double
_lib.vitalis_pearson_correlation.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_pearson_correlation.restype = ctypes.c_double
_lib.vitalis_spearman_correlation.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_spearman_correlation.restype = ctypes.c_double
_lib.vitalis_linear_regression.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_linear_regression.restype = None
_lib.vitalis_entropy.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_entropy.restype = ctypes.c_double
_lib.vitalis_chi_squared.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_chi_squared.restype = ctypes.c_double
_lib.vitalis_ks_statistic.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_ks_statistic.restype = ctypes.c_double
_lib.vitalis_covariance_matrix.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_covariance_matrix.restype = None

def stats_mean(data):
    """Mean."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_stats_mean(arr, n)

def stats_median(data):
    """Median."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_stats_median(arr, n)

def stats_variance(data):
    """Variance."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_stats_variance(arr, n)

def stats_stddev(data):
    """Stddev."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_stats_stddev(arr, n)

def stats_skewness(data):
    """Skewness."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_stats_skewness(arr, n)

def stats_kurtosis(data):
    """Kurtosis."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_stats_kurtosis(arr, n)

def stats_mode(data):
    """Mode."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_stats_mode(arr, n)

def normal_pdf(x, mu=0.0, sigma=1.0):
    """Normal PDF."""
    return _lib.vitalis_normal_pdf(x, mu, sigma)

def normal_cdf(x, mu=0.0, sigma=1.0):
    """Normal CDF."""
    return _lib.vitalis_normal_cdf(x, mu, sigma)

def normal_inv_cdf(p):
    """Inverse normal CDF."""
    return _lib.vitalis_normal_inv_cdf(p)

def exponential_pdf(x, lam):
    """Exponential PDF."""
    return _lib.vitalis_exponential_pdf(x, lam)

def exponential_cdf(x, lam):
    """Exponential CDF."""
    return _lib.vitalis_exponential_cdf(x, lam)

def poisson_pmf(k, lam):
    """Poisson PMF."""
    return _lib.vitalis_poisson_pmf(k, lam)

def binomial_pmf(k, n, p):
    """Binomial PMF."""
    return _lib.vitalis_binomial_pmf(k, n, p)

def pearson_correlation(x, y):
    """Pearson Correlation."""
    n = min(len(x), len(y))
    return _lib.vitalis_pearson_correlation((ctypes.c_double * n)(*x[:n]), (ctypes.c_double * n)(*y[:n]), n)

def spearman_correlation(x, y):
    """Spearman Correlation."""
    n = min(len(x), len(y))
    return _lib.vitalis_spearman_correlation((ctypes.c_double * n)(*x[:n]), (ctypes.c_double * n)(*y[:n]), n)

def linear_regression(x, y):
    """Linear regression. Returns (slope, intercept, r_squared)."""
    n = min(len(x), len(y))
    slope, intercept, r_sq = ctypes.c_double(0), ctypes.c_double(0), ctypes.c_double(0)
    _lib.vitalis_linear_regression((ctypes.c_double * n)(*x[:n]), (ctypes.c_double * n)(*y[:n]), n, ctypes.byref(slope), ctypes.byref(intercept), ctypes.byref(r_sq))
    return slope.value, intercept.value, r_sq.value

def data_entropy(data):
    """Shannon entropy of byte data."""
    buf = (ctypes.c_uint8 * len(data))(*data)
    return _lib.vitalis_entropy(buf, len(data))

def chi_squared(observed, expected):
    """Chi-squared statistic."""
    n = min(len(observed), len(expected))
    return _lib.vitalis_chi_squared((ctypes.c_double * n)(*observed[:n]), (ctypes.c_double * n)(*expected[:n]), n)

def ks_statistic(a, b):
    """Kolmogorov-Smirnov statistic."""
    return _lib.vitalis_ks_statistic((ctypes.c_double * len(a))(*a), len(a), (ctypes.c_double * len(b))(*b), len(b))

# ============================================================================
# v9.0 Modules
# ============================================================================

# --- Quantum Simulator ---

_lib.vitalis_quantum_new.argtypes = [ctypes.c_size_t]
_lib.vitalis_quantum_new.restype = ctypes.c_void_p
_lib.vitalis_quantum_free.argtypes = [ctypes.c_void_p]
_lib.vitalis_quantum_free.restype = None
_lib.vitalis_quantum_h.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_lib.vitalis_quantum_h.restype = None
_lib.vitalis_quantum_x.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_lib.vitalis_quantum_x.restype = None
_lib.vitalis_quantum_y.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_lib.vitalis_quantum_y.restype = None
_lib.vitalis_quantum_z.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_lib.vitalis_quantum_z.restype = None
_lib.vitalis_quantum_cnot.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_quantum_cnot.restype = None
_lib.vitalis_quantum_rx.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_quantum_rx.restype = None
_lib.vitalis_quantum_ry.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_quantum_ry.restype = None
_lib.vitalis_quantum_rz.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_quantum_rz.restype = None
_lib.vitalis_quantum_bell.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_quantum_bell.restype = None
_lib.vitalis_quantum_qft.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_lib.vitalis_quantum_qft.restype = None
_lib.vitalis_quantum_prob.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_lib.vitalis_quantum_prob.restype = ctypes.c_double
_lib.vitalis_quantum_measure.argtypes = [ctypes.c_void_p]
_lib.vitalis_quantum_measure.restype = ctypes.c_size_t
_lib.vitalis_quantum_bloch_theta.argtypes = [ctypes.c_void_p]
_lib.vitalis_quantum_bloch_theta.restype = ctypes.c_double
_lib.vitalis_quantum_fidelity.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_lib.vitalis_quantum_fidelity.restype = ctypes.c_double
_lib.vitalis_quantum_purity.argtypes = [ctypes.c_void_p]
_lib.vitalis_quantum_purity.restype = ctypes.c_double
_lib.vitalis_quantum_entropy.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_lib.vitalis_quantum_entropy.restype = ctypes.c_double

class QuantumRegister:
    """Quantum register with gate operations."""

    def __init__(self, n_qubits):
        self._ptr = _lib.vitalis_quantum_new(n_qubits)
        self._n = n_qubits

    def __del__(self):
        if hasattr(self, '_ptr') and self._ptr:
            _lib.vitalis_quantum_free(self._ptr)
            self._ptr = None

    def h(self, target):
        _lib.vitalis_quantum_h(self._ptr, target)
        return self

    def x(self, target):
        _lib.vitalis_quantum_x(self._ptr, target)
        return self

    def y(self, target):
        _lib.vitalis_quantum_y(self._ptr, target)
        return self

    def z(self, target):
        _lib.vitalis_quantum_z(self._ptr, target)
        return self

    def cnot(self, control, target):
        _lib.vitalis_quantum_cnot(self._ptr, control, target)
        return self

    def rx(self, target, theta):
        _lib.vitalis_quantum_rx(self._ptr, target, theta)
        return self

    def ry(self, target, theta):
        _lib.vitalis_quantum_ry(self._ptr, target, theta)
        return self

    def rz(self, target, theta):
        _lib.vitalis_quantum_rz(self._ptr, target, theta)
        return self

    def bell(self, q0=0, q1=1):
        _lib.vitalis_quantum_bell(self._ptr, q0, q1)
        return self

    def qft(self, n=None):
        _lib.vitalis_quantum_qft(self._ptr, n or self._n)
        return self

    def prob(self, state):
        return _lib.vitalis_quantum_prob(self._ptr, state)

    def measure(self):
        return _lib.vitalis_quantum_measure(self._ptr)

    def bloch_theta(self):
        return _lib.vitalis_quantum_bloch_theta(self._ptr)

    def fidelity(self, other):
        return _lib.vitalis_quantum_fidelity(self._ptr, other._ptr)

    def purity(self):
        return _lib.vitalis_quantum_purity(self._ptr)

    def entropy(self, qubit=0):
        return _lib.vitalis_quantum_entropy(self._ptr, qubit)

# --- Quantum Math ---

_lib.vitalis_complex_mul.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_complex_mul.restype = None
_lib.vitalis_complex_abs.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_complex_abs.restype = ctypes.c_double
_lib.vitalis_complex_exp.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_complex_exp.restype = None
_lib.vitalis_gamma.argtypes = [ctypes.c_double]
_lib.vitalis_gamma.restype = ctypes.c_double
_lib.vitalis_lgamma.argtypes = [ctypes.c_double]
_lib.vitalis_lgamma.restype = ctypes.c_double
_lib.vitalis_beta.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_beta.restype = ctypes.c_double
_lib.vitalis_bessel_j0.argtypes = [ctypes.c_double]
_lib.vitalis_bessel_j0.restype = ctypes.c_double
_lib.vitalis_bessel_j1.argtypes = [ctypes.c_double]
_lib.vitalis_bessel_j1.restype = ctypes.c_double
_lib.vitalis_zeta.argtypes = [ctypes.c_double]
_lib.vitalis_zeta.restype = ctypes.c_double
_lib.vitalis_monte_carlo_pi.argtypes = [ctypes.c_size_t]
_lib.vitalis_monte_carlo_pi.restype = ctypes.c_double
_lib.vitalis_monte_carlo_integrate.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.c_double]
_lib.vitalis_monte_carlo_integrate.restype = ctypes.c_double
_lib.vitalis_rk4_step.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_rk4_step.restype = ctypes.c_double
_lib.vitalis_rk4_solve.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_size_t, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_rk4_solve.restype = ctypes.c_double
_lib.vitalis_mod_pow.argtypes = [ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64]
_lib.vitalis_mod_pow.restype = ctypes.c_uint64
_lib.vitalis_is_prime.argtypes = [ctypes.c_uint64]
_lib.vitalis_is_prime.restype = ctypes.c_int32
_lib.vitalis_gcd.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
_lib.vitalis_gcd.restype = ctypes.c_uint64
_lib.vitalis_lcm.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
_lib.vitalis_lcm.restype = ctypes.c_uint64
_lib.vitalis_haar_forward.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_haar_forward.restype = None
_lib.vitalis_haar_inverse.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_haar_inverse.restype = None
_lib.vitalis_legendre.argtypes = [ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_legendre.restype = ctypes.c_double
_lib.vitalis_assoc_legendre.argtypes = [ctypes.c_size_t, ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_assoc_legendre.restype = ctypes.c_double
_lib.vitalis_fibonacci.argtypes = [ctypes.c_uint64]
_lib.vitalis_fibonacci.restype = ctypes.c_uint64
_lib.vitalis_golden_ratio.argtypes = []
_lib.vitalis_golden_ratio.restype = ctypes.c_double
_lib.vitalis_euler_totient.argtypes = [ctypes.c_uint64]
_lib.vitalis_euler_totient.restype = ctypes.c_uint64
_lib.vitalis_quat_mul.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_quat_mul.restype = None
_lib.vitalis_quat_rotate.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_quat_rotate.restype = None
_lib.vitalis_quat_slerp.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_quat_slerp.restype = None
_lib.vitalis_quantum_anneal_prob.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_quantum_anneal_prob.restype = ctypes.c_double
_lib.vitalis_outer_product.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_outer_product.restype = None
_lib.vitalis_kronecker_product.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_kronecker_product.restype = None

def complex_mul(a_re, a_im, b_re, b_im):
    """Complex multiply. Returns (re, im)."""
    out_re, out_im = ctypes.c_double(0), ctypes.c_double(0)
    _lib.vitalis_complex_mul(a_re, a_im, b_re, b_im, ctypes.byref(out_re), ctypes.byref(out_im))
    return out_re.value, out_im.value

def complex_abs(re, im):
    """Complex magnitude."""
    return _lib.vitalis_complex_abs(re, im)

def complex_exp(re, im):
    """Complex exponential. Returns (re, im)."""
    out_re, out_im = ctypes.c_double(0), ctypes.c_double(0)
    _lib.vitalis_complex_exp(re, im, ctypes.byref(out_re), ctypes.byref(out_im))
    return out_re.value, out_im.value

def gamma(x):
    """Gamma function."""
    return _lib.vitalis_gamma(x)

def lgamma(x):
    """Log-gamma function."""
    return _lib.vitalis_lgamma(x)

def bessel_j0(x):
    """Bessel J0."""
    return _lib.vitalis_bessel_j0(x)

def bessel_j1(x):
    """Bessel J1."""
    return _lib.vitalis_bessel_j1(x)

def beta_func(a, b):
    """Beta function B(a,b)."""
    return _lib.vitalis_beta(a, b)

def riemann_zeta(s):
    """Riemann zeta function."""
    return _lib.vitalis_zeta(s)

def monte_carlo_pi(n_samples=1000000):
    """Estimate pi via Monte Carlo."""
    return _lib.vitalis_monte_carlo_pi(n_samples)

def monte_carlo_integrate(values, a, b):
    """Monte Carlo integration."""
    arr, n = _to_double_array(values)
    return _lib.vitalis_monte_carlo_integrate(arr, n, a, b)

def rk4_step(y, t, h, a, b, c):
    """RK4 single step for y'=ay^2+by+c."""
    return _lib.vitalis_rk4_step(y, t, h, a, b, c)

def rk4_solve(y0, t0, t_end, n_steps, a, b, c):
    """RK4 solve ODE y'=ay^2+by+c."""
    return _lib.vitalis_rk4_solve(y0, t0, t_end, n_steps, a, b, c)

def mod_pow(base, exp, modulus):
    """Modular exponentiation."""
    return _lib.vitalis_mod_pow(base, exp, modulus)

def is_prime(n):
    """Primality test."""
    return _lib.vitalis_is_prime(n) == 1

def gcd(a, b):
    """Greatest common divisor."""
    return _lib.vitalis_gcd(a, b)

def lcm(a, b):
    """Least common multiple."""
    return _lib.vitalis_lcm(a, b)

def haar_forward(data):
    """Haar Forward transform."""
    arr = (ctypes.c_double * len(data))(*data)
    _lib.vitalis_haar_forward(arr, len(data))
    return list(arr)

def haar_inverse(data):
    """Haar Inverse transform."""
    arr = (ctypes.c_double * len(data))(*data)
    _lib.vitalis_haar_inverse(arr, len(data))
    return list(arr)

def legendre_poly(n, x):
    """Legendre polynomial P_n(x)."""
    return _lib.vitalis_legendre(n, x)

def assoc_legendre(l, m, x):
    """Associated Legendre P_l^m(x)."""
    return _lib.vitalis_assoc_legendre(l, m, x)

def fibonacci(n):
    """Fibonacci F(n)."""
    return _lib.vitalis_fibonacci(n)

def golden_ratio():
    """Golden ratio phi."""
    return _lib.vitalis_golden_ratio()

def euler_totient(n):
    """Euler totient phi(n)."""
    return _lib.vitalis_euler_totient(n)

def quat_mul(aw, ax, ay, az, bw, bx, by_, bz):
    """Quaternion multiply. Returns [w,x,y,z]."""
    out = (ctypes.c_double * 4)()
    _lib.vitalis_quat_mul(aw, ax, ay, az, bw, bx, by_, bz, out)
    return list(out)

def quat_rotate(qw, qx, qy, qz, vx, vy, vz):
    """Rotate vector by quaternion. Returns [x,y,z]."""
    out = (ctypes.c_double * 3)()
    _lib.vitalis_quat_rotate(qw, qx, qy, qz, vx, vy, vz, out)
    return list(out)

def quat_slerp(aw, ax, ay, az, bw, bx, by_, bz, t):
    """Quaternion SLERP. Returns [w,x,y,z]."""
    out = (ctypes.c_double * 4)()
    _lib.vitalis_quat_slerp(aw, ax, ay, az, bw, bx, by_, bz, t, out)
    return list(out)

def quantum_anneal_prob(energy_delta, temperature, transverse_field, time_step):
    """Quantum annealing acceptance probability."""
    return _lib.vitalis_quantum_anneal_prob(energy_delta, temperature, transverse_field, time_step)

def outer_product(a, b):
    """Outer product."""
    m, n = len(a), len(b)
    out = (ctypes.c_double * (m*n))()
    _lib.vitalis_outer_product((ctypes.c_double * m)(*a), m, (ctypes.c_double * n)(*b), n, out)
    return [[out[i*n+j] for j in range(n)] for i in range(m)]

def kronecker_product(a, b):
    """Kronecker product."""
    m, na = len(a), len(a[0])
    p, q = len(b), len(b[0])
    out_r, out_c = m*p, na*q
    out = (ctypes.c_double * (out_r*out_c))()
    _lib.vitalis_kronecker_product((ctypes.c_double * (m*na))(*[x for r in a for x in r]), m, na, (ctypes.c_double * (p*q))(*[x for r in b for x in r]), p, q, out)
    return [[out[i*out_c+j] for j in range(out_c)] for i in range(out_r)]

# --- Advanced Math ---

_lib.vitalis_math_factorial.argtypes = [ctypes.c_uint64]
_lib.vitalis_math_factorial.restype = ctypes.c_uint64
_lib.vitalis_math_binomial.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
_lib.vitalis_math_binomial.restype = ctypes.c_uint64
_lib.vitalis_math_catalan.argtypes = [ctypes.c_uint64]
_lib.vitalis_math_catalan.restype = ctypes.c_uint64
_lib.vitalis_math_erf.argtypes = [ctypes.c_double]
_lib.vitalis_math_erf.restype = ctypes.c_double
_lib.vitalis_math_mandelbrot.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_uint32]
_lib.vitalis_math_mandelbrot.restype = ctypes.c_uint32
_lib.vitalis_math_partition_count.argtypes = [ctypes.c_uint64]
_lib.vitalis_math_partition_count.restype = ctypes.c_uint64
_lib.vitalis_math_bell.argtypes = [ctypes.c_uint64]
_lib.vitalis_math_bell.restype = ctypes.c_uint64

def math_factorial(n):
    """Factorial n!."""
    return _lib.vitalis_math_factorial(n)

def math_catalan(n):
    """Catalan number C_n."""
    return _lib.vitalis_math_catalan(n)

def math_partition_count(n):
    """Integer partitions of n."""
    return _lib.vitalis_math_partition_count(n)

def math_bell(n):
    """Bell number B_n."""
    return _lib.vitalis_math_bell(n)

def math_binomial(n, k):
    """Binomial coefficient C(n,k)."""
    return _lib.vitalis_math_binomial(n, k)

def math_erf(x):
    """Error function erf(x)."""
    return _lib.vitalis_math_erf(x)

def mandelbrot(cx, cy, max_iter=1000):
    """Mandelbrot iteration count."""
    return _lib.vitalis_math_mandelbrot(cx, cy, max_iter)

# --- Science ---

_lib.vitalis_constant.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_constant.restype = ctypes.c_double
_lib.vitalis_kinematic_v.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_kinematic_v.restype = ctypes.c_double
_lib.vitalis_kinematic_s.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_kinematic_s.restype = ctypes.c_double
_lib.vitalis_kinematic_v_from_s.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_kinematic_v_from_s.restype = ctypes.c_double
_lib.vitalis_kinetic_energy.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_kinetic_energy.restype = ctypes.c_double
_lib.vitalis_potential_energy.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_potential_energy.restype = ctypes.c_double
_lib.vitalis_pendulum_period.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_pendulum_period.restype = ctypes.c_double
_lib.vitalis_orbital_velocity.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_orbital_velocity.restype = ctypes.c_double
_lib.vitalis_escape_velocity.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_escape_velocity.restype = ctypes.c_double
_lib.vitalis_projectile_range.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_projectile_range.restype = ctypes.c_double
_lib.vitalis_projectile_max_height.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_projectile_max_height.restype = ctypes.c_double
_lib.vitalis_ideal_gas_pressure.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_ideal_gas_pressure.restype = ctypes.c_double
_lib.vitalis_ideal_gas_temperature.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_ideal_gas_temperature.restype = ctypes.c_double
_lib.vitalis_carnot_efficiency.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_carnot_efficiency.restype = ctypes.c_double
_lib.vitalis_radiation_power.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_radiation_power.restype = ctypes.c_double
_lib.vitalis_heat_transfer.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_heat_transfer.restype = ctypes.c_double
_lib.vitalis_entropy_change.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_entropy_change.restype = ctypes.c_double
_lib.vitalis_coulomb_force.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_coulomb_force.restype = ctypes.c_double
_lib.vitalis_electric_field.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_electric_field.restype = ctypes.c_double
_lib.vitalis_ohms_law_v.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_ohms_law_v.restype = ctypes.c_double
_lib.vitalis_electrical_power.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_electrical_power.restype = ctypes.c_double
_lib.vitalis_capacitor_energy.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_capacitor_energy.restype = ctypes.c_double
_lib.vitalis_magnetic_force.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_magnetic_force.restype = ctypes.c_double
_lib.vitalis_wavelength.argtypes = [ctypes.c_double]
_lib.vitalis_wavelength.restype = ctypes.c_double
_lib.vitalis_photon_energy.argtypes = [ctypes.c_double]
_lib.vitalis_photon_energy.restype = ctypes.c_double
_lib.vitalis_doppler.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_doppler.restype = ctypes.c_double
_lib.vitalis_snell.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_snell.restype = ctypes.c_double
_lib.vitalis_de_broglie.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_de_broglie.restype = ctypes.c_double
_lib.vitalis_decay_constant.argtypes = [ctypes.c_double]
_lib.vitalis_decay_constant.restype = ctypes.c_double
_lib.vitalis_radioactive_decay.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_radioactive_decay.restype = ctypes.c_double
_lib.vitalis_activity.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_activity.restype = ctypes.c_double
_lib.vitalis_mass_energy.argtypes = [ctypes.c_double]
_lib.vitalis_mass_energy.restype = ctypes.c_double
_lib.vitalis_ph.argtypes = [ctypes.c_double]
_lib.vitalis_ph.restype = ctypes.c_double
_lib.vitalis_poh.argtypes = [ctypes.c_double]
_lib.vitalis_poh.restype = ctypes.c_double
_lib.vitalis_arrhenius.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_arrhenius.restype = ctypes.c_double
_lib.vitalis_nernst.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_nernst.restype = ctypes.c_double
_lib.vitalis_dilution.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_dilution.restype = ctypes.c_double
_lib.vitalis_schwarzschild_radius.argtypes = [ctypes.c_double]
_lib.vitalis_schwarzschild_radius.restype = ctypes.c_double
_lib.vitalis_luminosity.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_luminosity.restype = ctypes.c_double
_lib.vitalis_hubble_velocity.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_hubble_velocity.restype = ctypes.c_double
_lib.vitalis_redshift.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_redshift.restype = ctypes.c_double
_lib.vitalis_reynolds_number.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_reynolds_number.restype = ctypes.c_double
_lib.vitalis_drag_force.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_drag_force.restype = ctypes.c_double
_lib.vitalis_bernoulli_pressure.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_bernoulli_pressure.restype = ctypes.c_double
_lib.vitalis_celsius_to_kelvin.argtypes = [ctypes.c_double]
_lib.vitalis_celsius_to_kelvin.restype = ctypes.c_double
_lib.vitalis_kelvin_to_celsius.argtypes = [ctypes.c_double]
_lib.vitalis_kelvin_to_celsius.restype = ctypes.c_double
_lib.vitalis_ev_to_joules.argtypes = [ctypes.c_double]
_lib.vitalis_ev_to_joules.restype = ctypes.c_double
_lib.vitalis_joules_to_ev.argtypes = [ctypes.c_double]
_lib.vitalis_joules_to_ev.restype = ctypes.c_double
_lib.vitalis_deg_to_rad.argtypes = [ctypes.c_double]
_lib.vitalis_deg_to_rad.restype = ctypes.c_double
_lib.vitalis_rad_to_deg.argtypes = [ctypes.c_double]
_lib.vitalis_rad_to_deg.restype = ctypes.c_double

def physical_constant(name):
    """Get physical constant by name (c, h, G, k_B, e, N_A, sigma, etc)."""
    b = name.encode("utf-8")
    return _lib.vitalis_constant((ctypes.c_uint8 * len(b))(*b), len(b))

def kinematic_v(v0, a, t):
    """v = v0 + a*t."""
    return _lib.vitalis_kinematic_v(v0, a, t)

def kinematic_s(v0, a, t):
    """s = v0*t + 0.5*a*t^2."""
    return _lib.vitalis_kinematic_s(v0, a, t)

def kinematic_v_from_s(v0, a, s):
    """v = sqrt(v0^2 + 2as)."""
    return _lib.vitalis_kinematic_v_from_s(v0, a, s)

def kinetic_energy(mass, velocity):
    """KE = 0.5*m*v^2."""
    return _lib.vitalis_kinetic_energy(mass, velocity)

def potential_energy(mass, g, height):
    """PE = m*g*h."""
    return _lib.vitalis_potential_energy(mass, g, height)

def pendulum_period(length, g=9.81):
    """T = 2*pi*sqrt(L/g)."""
    return _lib.vitalis_pendulum_period(length, g)

def orbital_velocity(mass, radius):
    """v = sqrt(GM/r)."""
    return _lib.vitalis_orbital_velocity(mass, radius)

def escape_velocity(mass, radius):
    """v = sqrt(2GM/r)."""
    return _lib.vitalis_escape_velocity(mass, radius)

def projectile_range(v, theta, g=9.81):
    """R = v^2*sin(2t)/g."""
    return _lib.vitalis_projectile_range(v, theta, g)

def projectile_max_height(v, theta, g=9.81):
    """H = v^2*sin^2(t)/(2g)."""
    return _lib.vitalis_projectile_max_height(v, theta, g)

def ideal_gas_pressure(n, t, v):
    """P = nRT/V."""
    return _lib.vitalis_ideal_gas_pressure(n, t, v)

def ideal_gas_temperature(p, v, n):
    """T = PV/(nR)."""
    return _lib.vitalis_ideal_gas_temperature(p, v, n)

def carnot_efficiency(t_cold, t_hot):
    """eta = 1-Tc/Th."""
    return _lib.vitalis_carnot_efficiency(t_cold, t_hot)

def radiation_power(area, temp):
    """P = sigma*A*T^4."""
    return _lib.vitalis_radiation_power(area, temp)

def heat_transfer(mass, specific_heat, delta_t):
    """Q = m*c*dT."""
    return _lib.vitalis_heat_transfer(mass, specific_heat, delta_t)

def entropy_change(heat, temperature):
    """dS = Q/T."""
    return _lib.vitalis_entropy_change(heat, temperature)

def coulomb_force(q1, q2, r):
    """F = k*q1*q2/r^2."""
    return _lib.vitalis_coulomb_force(q1, q2, r)

def electric_field(q, r):
    """E = k*q/r^2."""
    return _lib.vitalis_electric_field(q, r)

def ohms_law_v(current, resistance):
    """V = IR."""
    return _lib.vitalis_ohms_law_v(current, resistance)

def electrical_power(voltage, current):
    """P = VI."""
    return _lib.vitalis_electrical_power(voltage, current)

def capacitor_energy(capacitance, voltage):
    """E = 0.5*C*V^2."""
    return _lib.vitalis_capacitor_energy(capacitance, voltage)

def magnetic_force(q, v, b, theta):
    """F = qvBsin(t)."""
    return _lib.vitalis_magnetic_force(q, v, b, theta)

def wavelength(frequency):
    """lambda = c/f."""
    return _lib.vitalis_wavelength(frequency)

def photon_energy(frequency):
    """E = hf."""
    return _lib.vitalis_photon_energy(frequency)

def doppler(f_source, v_sound, v_observer, v_source):
    """Doppler shift."""
    return _lib.vitalis_doppler(f_source, v_sound, v_observer, v_source)

def snell(n1, theta1, n2):
    """Snell's law refraction."""
    return _lib.vitalis_snell(n1, theta1, n2)

def de_broglie(mass, velocity):
    """lambda = h/(mv)."""
    return _lib.vitalis_de_broglie(mass, velocity)

def decay_constant(half_life):
    """lambda = ln2/t_half."""
    return _lib.vitalis_decay_constant(half_life)

def radioactive_decay(n0, decay_const, t):
    """N(t) = N0*exp(-lt)."""
    return _lib.vitalis_radioactive_decay(n0, decay_const, t)

def activity(decay_const, n):
    """A = lambda*N."""
    return _lib.vitalis_activity(decay_const, n)

def mass_energy(mass):
    """E = mc^2."""
    return _lib.vitalis_mass_energy(mass)

def ph(h_concentration):
    """pH = -log10([H+])."""
    return _lib.vitalis_ph(h_concentration)

def poh(ph_val):
    """pOH = 14 - pH."""
    return _lib.vitalis_poh(ph_val)

def arrhenius(a_factor, ea, t):
    """k = A*exp(-Ea/RT)."""
    return _lib.vitalis_arrhenius(a_factor, ea, t)

def nernst(e0, n_electrons, temperature, q):
    """Nernst equation."""
    return _lib.vitalis_nernst(e0, n_electrons, temperature, q)

def dilution(m1, v1, m2):
    """V2 = M1*V1/M2."""
    return _lib.vitalis_dilution(m1, v1, m2)

def schwarzschild_radius(mass):
    """r_s = 2GM/c^2."""
    return _lib.vitalis_schwarzschild_radius(mass)

def luminosity(radius, temperature):
    """L = 4pi*r^2*sigma*T^4."""
    return _lib.vitalis_luminosity(radius, temperature)

def hubble_velocity(h0, distance):
    """v = H0*d."""
    return _lib.vitalis_hubble_velocity(h0, distance)

def redshift(lambda_obs, lambda_emit):
    """z = (obs-emit)/emit."""
    return _lib.vitalis_redshift(lambda_obs, lambda_emit)

def reynolds_number(density, velocity, length, viscosity):
    """Re = rho*v*L/mu."""
    return _lib.vitalis_reynolds_number(density, velocity, length, viscosity)

def drag_force(cd, density, area, velocity):
    """F = 0.5*Cd*rho*A*v^2."""
    return _lib.vitalis_drag_force(cd, density, area, velocity)

def bernoulli_pressure(density, velocity, g, height, p_total):
    """Bernoulli eq.."""
    return _lib.vitalis_bernoulli_pressure(density, velocity, g, height, p_total)

def celsius_to_kelvin(c):
    """C to K."""
    return _lib.vitalis_celsius_to_kelvin(c)

def kelvin_to_celsius(k):
    """K to C."""
    return _lib.vitalis_kelvin_to_celsius(k)

def ev_to_joules(ev):
    """eV to J."""
    return _lib.vitalis_ev_to_joules(ev)

def joules_to_ev(j):
    """J to eV."""
    return _lib.vitalis_joules_to_ev(j)

def deg_to_rad(deg):
    """Degrees to radians."""
    return _lib.vitalis_deg_to_rad(deg)

def rad_to_deg(rad):
    """Radians to degrees."""
    return _lib.vitalis_rad_to_deg(rad)

# --- Analytics ---

_lib.vitalis_sma.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_sma.restype = ctypes.c_size_t
_lib.vitalis_ema.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_ema.restype = ctypes.c_size_t
_lib.vitalis_wma.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_wma.restype = ctypes.c_size_t
_lib.vitalis_dema.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_dema.restype = ctypes.c_size_t
_lib.vitalis_anomaly_zscore.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.POINTER(ctypes.c_int32)]
_lib.vitalis_anomaly_zscore.restype = ctypes.c_size_t
_lib.vitalis_anomaly_iqr.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.POINTER(ctypes.c_int32)]
_lib.vitalis_anomaly_iqr.restype = ctypes.c_size_t
_lib.vitalis_anomaly_mad.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.POINTER(ctypes.c_int32)]
_lib.vitalis_anomaly_mad.restype = ctypes.c_size_t
_lib.vitalis_linear_trend.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_linear_trend.restype = None
_lib.vitalis_turning_points.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_turning_points.restype = ctypes.c_size_t
_lib.vitalis_ses_forecast.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_ses_forecast.restype = ctypes.c_double
_lib.vitalis_holt_forecast.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.c_double, ctypes.c_size_t]
_lib.vitalis_holt_forecast.restype = ctypes.c_double
_lib.vitalis_minmax_scale.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_minmax_scale.restype = None
_lib.vitalis_zscore_normalize.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_zscore_normalize.restype = None
_lib.vitalis_sla_uptime.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_sla_uptime.restype = ctypes.c_double
_lib.vitalis_error_rate.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_error_rate.restype = ctypes.c_double
_lib.vitalis_throughput.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_throughput.restype = ctypes.c_double
_lib.vitalis_apdex.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_apdex.restype = ctypes.c_double
_lib.vitalis_mtbf.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_mtbf.restype = ctypes.c_double
_lib.vitalis_mttr.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_mttr.restype = ctypes.c_double
_lib.vitalis_cardinality.argtypes = [ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t]
_lib.vitalis_cardinality.restype = ctypes.c_size_t

def sma(data, window):
    """Simple moving average."""
    arr, n = _to_double_array(data)
    out = (ctypes.c_double * n)()
    count = _lib.vitalis_sma(arr, n, window, out)
    return list(out[:count])

def ema(data, alpha):
    """Exponential moving average."""
    arr, n = _to_double_array(data)
    out = (ctypes.c_double * n)()
    count = _lib.vitalis_ema(arr, n, alpha, out)
    return list(out[:count])

def wma(data, window):
    """Weighted moving average."""
    arr, n = _to_double_array(data)
    out = (ctypes.c_double * n)()
    count = _lib.vitalis_wma(arr, n, window, out)
    return list(out[:count])

def dema(data, alpha):
    """Double exponential moving average."""
    arr, n = _to_double_array(data)
    out = (ctypes.c_double * n)()
    count = _lib.vitalis_dema(arr, n, alpha, out)
    return list(out[:count])

def anomaly_zscore(data, threshold=2.0):
    """Anomaly detection (ZSCORE)."""
    arr, n = _to_double_array(data)
    flags = (ctypes.c_int32 * n)()
    count = _lib.vitalis_anomaly_zscore(arr, n, threshold, flags)
    return count, [bool(f) for f in flags]

def anomaly_iqr(data, threshold=1.5):
    """Anomaly detection (IQR)."""
    arr, n = _to_double_array(data)
    flags = (ctypes.c_int32 * n)()
    count = _lib.vitalis_anomaly_iqr(arr, n, threshold, flags)
    return count, [bool(f) for f in flags]

def anomaly_mad(data, threshold=3.0):
    """Anomaly detection (MAD)."""
    arr, n = _to_double_array(data)
    flags = (ctypes.c_int32 * n)()
    count = _lib.vitalis_anomaly_mad(arr, n, threshold, flags)
    return count, [bool(f) for f in flags]

def linear_trend(data):
    """Returns (slope, intercept)."""
    arr, n = _to_double_array(data)
    slope, intercept = ctypes.c_double(0), ctypes.c_double(0)
    _lib.vitalis_linear_trend(arr, n, ctypes.byref(slope), ctypes.byref(intercept))
    return slope.value, intercept.value

def turning_points(data):
    """Count turning points."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_turning_points(arr, n)

def ses_forecast(data, alpha=0.3):
    """Simple exponential smoothing forecast."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_ses_forecast(arr, n, alpha)

def holt_forecast(data, alpha=0.3, beta_val=0.1, h=1):
    """Holt linear trend forecast h steps ahead."""
    arr, n = _to_double_array(data)
    return _lib.vitalis_holt_forecast(arr, n, alpha, beta_val, h)

def minmax_scale(data):
    """Minmax Scale."""
    arr, n = _to_double_array(data)
    out = (ctypes.c_double * n)()
    _lib.vitalis_minmax_scale(arr, n, out)
    return list(out)

def zscore_normalize(data):
    """Zscore Normalize."""
    arr, n = _to_double_array(data)
    out = (ctypes.c_double * n)()
    _lib.vitalis_zscore_normalize(arr, n, out)
    return list(out)

def sla_uptime(samples):
    """SLA uptime percentage."""
    arr, n = _to_double_array(samples)
    return _lib.vitalis_sla_uptime(arr, n)

def error_rate(errors, total):
    """Error rate."""
    return _lib.vitalis_error_rate(errors, total)

def throughput(count, duration_seconds):
    """Throughput."""
    return _lib.vitalis_throughput(count, duration_seconds)

def apdex(satisfied, tolerating, total):
    """Apdex score."""
    return _lib.vitalis_apdex(satisfied, tolerating, total)

def mtbf(total_uptime, num_failures):
    """Mean time between failures."""
    return _lib.vitalis_mtbf(total_uptime, num_failures)

def mttr(total_downtime, num_failures):
    """Mean time to recovery."""
    return _lib.vitalis_mttr(total_downtime, num_failures)

def cardinality(values):
    """Count distinct integers."""
    arr = (ctypes.c_int64 * len(values))(*values)
    return _lib.vitalis_cardinality(arr, len(values))

# --- Security ---

_lib.vitalis_validate_email.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_validate_email.restype = ctypes.c_int32
_lib.vitalis_validate_ipv4.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_validate_ipv4.restype = ctypes.c_int32
_lib.vitalis_validate_range.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_validate_range.restype = ctypes.c_int32
_lib.vitalis_validate_length.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_validate_length.restype = ctypes.c_int32
_lib.vitalis_validate_url.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_validate_url.restype = ctypes.c_int32
_lib.vitalis_detect_sqli.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_detect_sqli.restype = ctypes.c_double
_lib.vitalis_detect_xss.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_detect_xss.restype = ctypes.c_double
_lib.vitalis_detect_path_traversal.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_detect_path_traversal.restype = ctypes.c_int32
_lib.vitalis_detect_command_injection.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_detect_command_injection.restype = ctypes.c_double
_lib.vitalis_password_strength.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_password_strength.restype = ctypes.c_double
_lib.vitalis_password_entropy.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_password_entropy.restype = ctypes.c_double
_lib.vitalis_check_memory_quota.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
_lib.vitalis_check_memory_quota.restype = ctypes.c_int32
_lib.vitalis_check_time_budget.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_check_time_budget.restype = ctypes.c_int32
_lib.vitalis_check_recursion_depth.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
_lib.vitalis_check_recursion_depth.restype = ctypes.c_int32
_lib.vitalis_resource_utilization.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_resource_utilization.restype = ctypes.c_double
_lib.vitalis_code_safety_score.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_code_safety_score.restype = ctypes.c_double
_lib.vitalis_audit_hash.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_audit_hash.restype = ctypes.c_uint64
_lib.vitalis_hash_chain.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_uint64]
_lib.vitalis_hash_chain.restype = ctypes.c_uint64
_lib.vitalis_token_bucket_check.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_token_bucket_check.restype = ctypes.c_double
_lib.vitalis_sliding_window_check.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.c_double, ctypes.c_size_t]
_lib.vitalis_sliding_window_check.restype = ctypes.c_int32
_lib.vitalis_sandbox_grant.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
_lib.vitalis_sandbox_grant.restype = ctypes.c_uint64
_lib.vitalis_sandbox_revoke.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
_lib.vitalis_sandbox_revoke.restype = ctypes.c_uint64
_lib.vitalis_sandbox_check.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
_lib.vitalis_sandbox_check.restype = ctypes.c_int32
_lib.vitalis_sandbox_count.argtypes = [ctypes.c_uint64]
_lib.vitalis_sandbox_count.restype = ctypes.c_uint32
_lib.vitalis_html_escape.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_html_escape.restype = ctypes.c_size_t

def _str_buf(s):
    b = s.encode("utf-8")
    return (ctypes.c_uint8 * len(b))(*b), len(b)

def validate_email(s):
    """Validate email."""
    buf, n = _str_buf(s)
    return _lib.vitalis_validate_email(buf, n) == 1

def validate_ipv4(s):
    """Validate ipv4."""
    buf, n = _str_buf(s)
    return _lib.vitalis_validate_ipv4(buf, n) == 1

def validate_url(s):
    """Validate url."""
    buf, n = _str_buf(s)
    return _lib.vitalis_validate_url(buf, n) == 1

def validate_range(value, min_val, max_val):
    """Validate value in range."""
    return _lib.vitalis_validate_range(value, min_val, max_val) == 1

def validate_length(s, min_len, max_len):
    """Validate string length."""
    buf, n = _str_buf(s)
    return _lib.vitalis_validate_length(buf, n, min_len, max_len) == 1

def detect_sqli(input_str):
    """Detect sqli risk (0-1)."""
    buf, n = _str_buf(input_str)
    return _lib.vitalis_detect_sqli(buf, n)

def detect_xss(input_str):
    """Detect xss risk (0-1)."""
    buf, n = _str_buf(input_str)
    return _lib.vitalis_detect_xss(buf, n)

def detect_command_injection(input_str):
    """Detect command risk (0-1)."""
    buf, n = _str_buf(input_str)
    return _lib.vitalis_detect_command_injection(buf, n)

def detect_path_traversal(path):
    """Detect path traversal."""
    buf, n = _str_buf(path)
    return _lib.vitalis_detect_path_traversal(buf, n) == 1

def password_strength(password):
    """Password Strength."""
    buf, n = _str_buf(password)
    return _lib.vitalis_password_strength(buf, n)

def password_entropy(password):
    """Password Entropy."""
    buf, n = _str_buf(password)
    return _lib.vitalis_password_entropy(buf, n)

def check_memory_quota(used_bytes, max_bytes):
    """Check memory quota."""
    return _lib.vitalis_check_memory_quota(used_bytes, max_bytes) == 1

def check_time_budget(elapsed_ms, budget_ms):
    """Check time budget."""
    return _lib.vitalis_check_time_budget(elapsed_ms, budget_ms) == 1

def check_recursion_depth(depth, max_depth):
    """Check recursion depth."""
    return _lib.vitalis_check_recursion_depth(depth, max_depth) == 1

def resource_utilization(used, total):
    """Resource utilization %."""
    return _lib.vitalis_resource_utilization(used, total)

def code_safety_score(code):
    """Code safety score (0-1)."""
    buf, n = _str_buf(code)
    return _lib.vitalis_code_safety_score(buf, n)

def audit_hash(data):
    """FNV-1a audit hash."""
    buf, n = _str_buf(data)
    return _lib.vitalis_audit_hash(buf, n)

def hash_chain(data, prev_hash):
    """Chain hash."""
    buf, n = _str_buf(data)
    return _lib.vitalis_hash_chain(buf, n, prev_hash)

def sec_token_bucket_check(tokens, max_tokens, refill_rate, elapsed_secs, cost):
    """Token bucket rate limit."""
    return _lib.vitalis_token_bucket_check(tokens, max_tokens, refill_rate, elapsed_secs, cost)

def sec_sliding_window_check(timestamps, now, window_secs, max_requests):
    """Sliding window rate limit."""
    arr, n = _to_double_array(timestamps)
    return _lib.vitalis_sliding_window_check(arr, n, now, window_secs, max_requests) == 1

def sandbox_grant(current, capability):
    """Sandbox Grant."""
    return _lib.vitalis_sandbox_grant(current, capability)

def sandbox_revoke(current, capability):
    """Sandbox Revoke."""
    return _lib.vitalis_sandbox_revoke(current, capability)

def sandbox_check(current, required):
    """Check capability."""
    return _lib.vitalis_sandbox_check(current, required) == 1

def sandbox_count(caps):
    """Count capabilities."""
    return _lib.vitalis_sandbox_count(caps)

def html_escape(text):
    """HTML-escape string."""
    buf, n = _str_buf(text)
    out = (ctypes.c_uint8 * (n * 6 + 1))()
    out_len = _lib.vitalis_html_escape(buf, n, out, n * 6 + 1)
    return bytes(out[:out_len]).decode("utf-8")

# --- Scoring ---

_lib.vitalis_maintainability_index.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_maintainability_index.restype = ctypes.c_double
_lib.vitalis_tech_debt_ratio.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_tech_debt_ratio.restype = ctypes.c_double
_lib.vitalis_code_quality_composite.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_code_quality_composite.restype = ctypes.c_double
_lib.vitalis_halstead_metrics.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_halstead_metrics.restype = None
_lib.vitalis_weighted_fitness.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_weighted_fitness.restype = ctypes.c_double
_lib.vitalis_pareto_dominates.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_pareto_dominates.restype = ctypes.c_int32
_lib.vitalis_pareto_rank.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32)]
_lib.vitalis_pareto_rank.restype = None
_lib.vitalis_elo_update.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_elo_update.restype = None
_lib.vitalis_elo_expected.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_elo_expected.restype = ctypes.c_double
_lib.vitalis_welch_t.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_welch_t.restype = ctypes.c_double
_lib.vitalis_cohens_d.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_cohens_d.restype = ctypes.c_double
_lib.vitalis_mann_whitney_u.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_mann_whitney_u.restype = ctypes.c_double
_lib.vitalis_conversion_rate.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_conversion_rate.restype = None
_lib.vitalis_bayesian_ab.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_bayesian_ab.restype = ctypes.c_double
_lib.vitalis_regression_score.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_regression_score.restype = ctypes.c_double
_lib.vitalis_regression_count.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_regression_count.restype = ctypes.c_size_t
_lib.vitalis_geometric_mean.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_geometric_mean.restype = ctypes.c_double
_lib.vitalis_harmonic_mean.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_harmonic_mean.restype = ctypes.c_double
_lib.vitalis_power_mean.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_power_mean.restype = ctypes.c_double
_lib.vitalis_latency_score.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_latency_score.restype = ctypes.c_double
_lib.vitalis_efficiency_ratio.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_efficiency_ratio.restype = ctypes.c_double
_lib.vitalis_throughput_efficiency.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_throughput_efficiency.restype = ctypes.c_double
_lib.vitalis_system_health.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_system_health.restype = ctypes.c_double
_lib.vitalis_decay_fitness.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_decay_fitness.restype = ctypes.c_double
_lib.vitalis_sigmoid_fitness.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_sigmoid_fitness.restype = ctypes.c_double
_lib.vitalis_tournament_fitness.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_tournament_fitness.restype = ctypes.c_double

def maintainability_index(halstead_volume, cyclomatic_complexity, loc):
    """Maintainability Index."""
    return _lib.vitalis_maintainability_index(halstead_volume, cyclomatic_complexity, loc)

def tech_debt_ratio(issues, avg_fix_time_hours, total_dev_time_hours):
    """Technical debt ratio."""
    return _lib.vitalis_tech_debt_ratio(issues, avg_fix_time_hours, total_dev_time_hours)

def code_quality_composite(cyclomatic, cognitive, loc, num_functions, issues, test_coverage, duplication):
    """Composite code quality (0-1)."""
    return _lib.vitalis_code_quality_composite(cyclomatic, cognitive, loc, num_functions, issues, test_coverage, duplication)

def halstead_metrics(n1, n2, eta1, eta2):
    """Halstead metrics dict."""
    out = (ctypes.c_double * 5)()
    _lib.vitalis_halstead_metrics(n1, n2, eta1, eta2, out)
    return {"vocabulary": out[0], "length": out[1], "volume": out[2], "difficulty": out[3], "effort": out[4]}

def weighted_fitness(objectives, weights):
    """Weighted fitness."""
    n = min(len(objectives), len(weights))
    return _lib.vitalis_weighted_fitness((ctypes.c_double * n)(*objectives[:n]), (ctypes.c_double * n)(*weights[:n]), n)

def score_pareto_dominates(a, b):
    """Pareto dominance check."""
    n = min(len(a), len(b))
    return _lib.vitalis_pareto_dominates((ctypes.c_double * n)(*a[:n]), (ctypes.c_double * n)(*b[:n]), n) == 1

def pareto_rank(solutions):
    """Pareto rank for population."""
    n_pop, n_obj = len(solutions), len(solutions[0])
    flat = (ctypes.c_double * (n_pop*n_obj))(*[x for s in solutions for x in s])
    ranks = (ctypes.c_uint32 * n_pop)()
    _lib.vitalis_pareto_rank(flat, n_pop, n_obj, ranks)
    return list(ranks)

def elo_update(rating_a, rating_b, result, k_factor=32.0):
    """ELO update. Returns (new_a, new_b)."""
    out = (ctypes.c_double * 2)()
    _lib.vitalis_elo_update(rating_a, rating_b, result, k_factor, out)
    return out[0], out[1]

def elo_expected(rating_a, rating_b):
    """Expected ELO score for A."""
    return _lib.vitalis_elo_expected(rating_a, rating_b)

def welch_t(a, b):
    """Welch T."""
    return _lib.vitalis_welch_t((ctypes.c_double * len(a))(*a), len(a), (ctypes.c_double * len(b))(*b), len(b))

def cohens_d(a, b):
    """Cohens D."""
    return _lib.vitalis_cohens_d((ctypes.c_double * len(a))(*a), len(a), (ctypes.c_double * len(b))(*b), len(b))

def mann_whitney_u(a, b):
    """Mann Whitney U."""
    return _lib.vitalis_mann_whitney_u((ctypes.c_double * len(a))(*a), len(a), (ctypes.c_double * len(b))(*b), len(b))

def conversion_rate(successes, trials, z=1.96):
    """Wilson score interval. Returns (rate, lower, upper)."""
    out = (ctypes.c_double * 3)()
    _lib.vitalis_conversion_rate(successes, trials, z, out)
    return out[0], out[1], out[2]

def bayesian_ab(a_succ, a_fail, b_succ, b_fail):
    """Bayesian A/B: P(B>A)."""
    return _lib.vitalis_bayesian_ab(a_succ, a_fail, b_succ, b_fail)

def regression_score(current, baseline):
    """Regression ratio."""
    return _lib.vitalis_regression_score(current, baseline)

def regression_count(current, baseline, threshold_pct=5.0):
    """Count regressions."""
    n = min(len(current), len(baseline))
    return _lib.vitalis_regression_count((ctypes.c_double * n)(*current[:n]), (ctypes.c_double * n)(*baseline[:n]), n, threshold_pct)

def geometric_mean(values):
    """Geometric Mean."""
    arr, n = _to_double_array(values)
    return _lib.vitalis_geometric_mean(arr, n)

def harmonic_mean(values):
    """Harmonic Mean."""
    arr, n = _to_double_array(values)
    return _lib.vitalis_harmonic_mean(arr, n)

def power_mean_val(values, weights, p):
    """Generalized power mean."""
    n = min(len(values), len(weights))
    return _lib.vitalis_power_mean((ctypes.c_double * n)(*values[:n]), (ctypes.c_double * n)(*weights[:n]), n, p)

def latency_score(p50, p95, p99, target_p50, target_p95, target_p99):
    """Latency score (0-1)."""
    return _lib.vitalis_latency_score(p50, p95, p99, target_p50, target_p95, target_p99)

def efficiency_ratio(useful_work, total_resources):
    """Efficiency ratio."""
    return _lib.vitalis_efficiency_ratio(useful_work, total_resources)

def throughput_efficiency(actual, theoretical_max):
    """Throughput efficiency."""
    return _lib.vitalis_throughput_efficiency(actual, theoretical_max)

def system_health(dimensions, weights):
    """System health (0-1)."""
    n = min(len(dimensions), len(weights))
    return _lib.vitalis_system_health((ctypes.c_double * n)(*dimensions[:n]), (ctypes.c_double * n)(*weights[:n]), n)

def score_decay_fitness(distance, k=1.0):
    """Exponential decay fitness."""
    return _lib.vitalis_decay_fitness(distance, k)

def sigmoid_fitness(x, k=1.0, midpoint=0.0):
    """Sigmoid fitness."""
    return _lib.vitalis_sigmoid_fitness(x, k, midpoint)

def tournament_fitness(wins, losses, draws):
    """Tournament fitness."""
    return _lib.vitalis_tournament_fitness(wins, losses, draws)


# ============================================================================
# v10.0: Machine Learning
# ============================================================================

_lib.vitalis_kmeans.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int32)]
_lib.vitalis_kmeans.restype = None

_lib.vitalis_knn_classify.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_knn_classify.restype = ctypes.c_int32

_lib.vitalis_naive_bayes.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_naive_bayes.restype = ctypes.c_int32

_lib.vitalis_logistic_regression.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t, ctypes.c_size_t, ctypes.c_double, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_logistic_regression.restype = None

_lib.vitalis_logistic_predict.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_logistic_predict.restype = ctypes.c_double

_lib.vitalis_pca.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_pca.restype = None

_lib.vitalis_decision_stump.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_decision_stump.restype = ctypes.c_double

_lib.vitalis_dbscan.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.c_double, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int32)]
_lib.vitalis_dbscan.restype = ctypes.c_int32

_lib.vitalis_accuracy.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t]
_lib.vitalis_accuracy.restype = ctypes.c_double

_lib.vitalis_precision.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t, ctypes.c_int32]
_lib.vitalis_precision.restype = ctypes.c_double

_lib.vitalis_recall.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t, ctypes.c_int32]
_lib.vitalis_recall.restype = ctypes.c_double

_lib.vitalis_f1_score.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t, ctypes.c_int32]
_lib.vitalis_f1_score.restype = ctypes.c_double

_lib.vitalis_mse.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_mse.restype = ctypes.c_double

_lib.vitalis_mae.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_mae.restype = ctypes.c_double

_lib.vitalis_r2_score.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_r2_score.restype = ctypes.c_double

_lib.vitalis_ml_cosine_similarity.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_ml_cosine_similarity.restype = ctypes.c_double

_lib.vitalis_silhouette_score.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t, ctypes.c_size_t, ctypes.c_int32]
_lib.vitalis_silhouette_score.restype = ctypes.c_double

def ml_kmeans(data, n_samples, n_features, k, max_iter=100):
    """K-means clustering. data is flattened [n_samples * n_features]. Returns cluster labels."""
    flat = (ctypes.c_double * len(data))(*data)
    labels = (ctypes.c_int32 * n_samples)()
    _lib.vitalis_kmeans(flat, n_samples, n_features, k, max_iter, labels)
    return list(labels)

def ml_knn(train_data, train_labels, n_train, n_features, query, k):
    """K-nearest neighbors classification. Returns predicted label."""
    flat = (ctypes.c_double * len(train_data))(*train_data)
    labs = (ctypes.c_int32 * n_train)(*train_labels)
    q = (ctypes.c_double * n_features)(*query)
    return _lib.vitalis_knn_classify(flat, labs, n_train, n_features, q, k)

def ml_naive_bayes(train_data, train_labels, n_train, n_features, query):
    """Gaussian Naive Bayes classification. Returns predicted label."""
    flat = (ctypes.c_double * len(train_data))(*train_data)
    labs = (ctypes.c_int32 * n_train)(*train_labels)
    q = (ctypes.c_double * n_features)(*query)
    return _lib.vitalis_naive_bayes(flat, labs, n_train, n_features, q)

def ml_logistic_regression(data, labels, n_samples, n_features, lr=0.01, epochs=100):
    """Train logistic regression. Returns weight vector [n_features + 1] (bias last)."""
    flat = (ctypes.c_double * len(data))(*data)
    labs = (ctypes.c_int32 * n_samples)(*labels)
    weights = (ctypes.c_double * (n_features + 1))()
    _lib.vitalis_logistic_regression(flat, labs, n_samples, n_features, lr, epochs, weights)
    return list(weights)

def ml_logistic_predict(features, weights):
    """Predict probability using trained logistic regression weights."""
    f = (ctypes.c_double * len(features))(*features)
    w = (ctypes.c_double * len(weights))(*weights)
    return _lib.vitalis_logistic_predict(f, w, len(features))

def ml_pca(data, n_samples, n_features, n_components):
    """PCA dimensionality reduction. Returns projected data [n_samples * n_components]."""
    flat = (ctypes.c_double * len(data))(*data)
    out = (ctypes.c_double * (n_samples * n_components))()
    _lib.vitalis_pca(flat, n_samples, n_features, n_components, out)
    return list(out)

def ml_decision_stump(data, labels, n_samples, n_features):
    """Train decision stump. Returns (gini, best_feature, best_threshold)."""
    flat = (ctypes.c_double * len(data))(*data)
    labs = (ctypes.c_int32 * n_samples)(*labels)
    feat = ctypes.c_size_t()
    thresh = ctypes.c_double()
    gini = _lib.vitalis_decision_stump(flat, labs, n_samples, n_features, ctypes.byref(feat), ctypes.byref(thresh))
    return gini, feat.value, thresh.value

def ml_dbscan(data, n_samples, n_features, eps, min_pts):
    """DBSCAN density clustering. Returns (n_clusters, labels)."""
    flat = (ctypes.c_double * len(data))(*data)
    labels = (ctypes.c_int32 * n_samples)()
    nc = _lib.vitalis_dbscan(flat, n_samples, n_features, eps, min_pts, labels)
    return nc, list(labels)

def ml_accuracy(predicted, actual):
    """Classification accuracy."""
    n = len(predicted)
    p = (ctypes.c_int32 * n)(*predicted)
    a = (ctypes.c_int32 * n)(*actual)
    return _lib.vitalis_accuracy(p, a, n)

def ml_precision(predicted, actual, positive_label=1):
    """Precision for binary classification."""
    n = len(predicted)
    p = (ctypes.c_int32 * n)(*predicted)
    a = (ctypes.c_int32 * n)(*actual)
    return _lib.vitalis_precision(p, a, n, positive_label)

def ml_recall(predicted, actual, positive_label=1):
    """Recall for binary classification."""
    n = len(predicted)
    p = (ctypes.c_int32 * n)(*predicted)
    a = (ctypes.c_int32 * n)(*actual)
    return _lib.vitalis_recall(p, a, n, positive_label)

def ml_f1(predicted, actual, positive_label=1):
    """F1 score for binary classification."""
    n = len(predicted)
    p = (ctypes.c_int32 * n)(*predicted)
    a = (ctypes.c_int32 * n)(*actual)
    return _lib.vitalis_f1_score(p, a, n, positive_label)

def ml_mse(predicted, actual):
    """Mean Squared Error."""
    n = len(predicted)
    p = (ctypes.c_double * n)(*predicted)
    a = (ctypes.c_double * n)(*actual)
    return _lib.vitalis_mse(p, a, n)

def ml_mae(predicted, actual):
    """Mean Absolute Error."""
    n = len(predicted)
    p = (ctypes.c_double * n)(*predicted)
    a = (ctypes.c_double * n)(*actual)
    return _lib.vitalis_mae(p, a, n)

def ml_r2(predicted, actual):
    """R² score."""
    n = len(predicted)
    p = (ctypes.c_double * n)(*predicted)
    a = (ctypes.c_double * n)(*actual)
    return _lib.vitalis_r2_score(p, a, n)

def ml_cosine_similarity(a_vec, b_vec):
    """Cosine similarity between two vectors."""
    n = len(a_vec)
    a = (ctypes.c_double * n)(*a_vec)
    b = (ctypes.c_double * n)(*b_vec)
    return _lib.vitalis_ml_cosine_similarity(a, b, n)

def ml_silhouette(data, labels, n_samples, n_features, n_clusters):
    """Silhouette score for clustering quality."""
    flat = (ctypes.c_double * len(data))(*data)
    labs = (ctypes.c_int32 * n_samples)(*labels)
    return _lib.vitalis_silhouette_score(flat, labs, n_samples, n_features, n_clusters)


# ============================================================================
# v10.0: Computational Geometry
# ============================================================================

_lib.vitalis_convex_hull.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_convex_hull.restype = ctypes.c_int32

_lib.vitalis_point_in_polygon.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_point_in_polygon.restype = ctypes.c_int32

_lib.vitalis_line_intersection.argtypes = [ctypes.c_double]*8 + [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_line_intersection.restype = ctypes.c_int32

_lib.vitalis_closest_pair.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_closest_pair.restype = ctypes.c_double

_lib.vitalis_polygon_area.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_polygon_area.restype = ctypes.c_double

_lib.vitalis_polygon_perimeter.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_polygon_perimeter.restype = ctypes.c_double

_lib.vitalis_triangle_area.argtypes = [ctypes.c_double]*6
_lib.vitalis_triangle_area.restype = ctypes.c_double

_lib.vitalis_is_convex.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_is_convex.restype = ctypes.c_int32

_lib.vitalis_distance_3d.argtypes = [ctypes.c_double]*6
_lib.vitalis_distance_3d.restype = ctypes.c_double

def geo_convex_hull(xs, ys):
    """Convex hull of 2D points. Returns (hull_xs, hull_ys)."""
    n = len(xs)
    x = (ctypes.c_double * n)(*xs)
    y = (ctypes.c_double * n)(*ys)
    ox = (ctypes.c_double * n)()
    oy = (ctypes.c_double * n)()
    hn = _lib.vitalis_convex_hull(x, y, n, ox, oy)
    return list(ox[:hn]), list(oy[:hn])

def geo_point_in_polygon(px, py, xs, ys):
    """Test if point (px,py) is inside polygon. Returns bool."""
    n = len(xs)
    x = (ctypes.c_double * n)(*xs)
    y = (ctypes.c_double * n)(*ys)
    return bool(_lib.vitalis_point_in_polygon(px, py, x, y, n))

def geo_line_intersection(x1, y1, x2, y2, x3, y3, x4, y4):
    """Line segment intersection. Returns (True, ix, iy) or (False, 0, 0)."""
    ix = ctypes.c_double()
    iy = ctypes.c_double()
    r = _lib.vitalis_line_intersection(x1, y1, x2, y2, x3, y3, x4, y4, ctypes.byref(ix), ctypes.byref(iy))
    return bool(r), ix.value, iy.value

def geo_closest_pair(xs, ys):
    """Closest pair of points. Returns min distance."""
    n = len(xs)
    x = (ctypes.c_double * n)(*xs)
    y = (ctypes.c_double * n)(*ys)
    return _lib.vitalis_closest_pair(x, y, n)

def geo_polygon_area(xs, ys):
    """Polygon area via Shoelace formula."""
    n = len(xs)
    x = (ctypes.c_double * n)(*xs)
    y = (ctypes.c_double * n)(*ys)
    return _lib.vitalis_polygon_area(x, y, n)

def geo_polygon_perimeter(xs, ys):
    """Polygon perimeter."""
    n = len(xs)
    x = (ctypes.c_double * n)(*xs)
    y = (ctypes.c_double * n)(*ys)
    return _lib.vitalis_polygon_perimeter(x, y, n)

def geo_triangle_area(x1, y1, x2, y2, x3, y3):
    """Triangle area from vertices."""
    return _lib.vitalis_triangle_area(x1, y1, x2, y2, x3, y3)

def geo_is_convex(xs, ys):
    """Check if polygon is convex."""
    n = len(xs)
    x = (ctypes.c_double * n)(*xs)
    y = (ctypes.c_double * n)(*ys)
    return bool(_lib.vitalis_is_convex(x, y, n))

def geo_distance_3d(x1, y1, z1, x2, y2, z2):
    """Euclidean distance in 3D."""
    return _lib.vitalis_distance_3d(x1, y1, z1, x2, y2, z2)


# ============================================================================
# v10.0: Sorting & Searching
# ============================================================================

_lib.vitalis_quicksort.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_quicksort.restype = None

_lib.vitalis_mergesort.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_mergesort.restype = None

_lib.vitalis_heapsort.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_heapsort.restype = None

_lib.vitalis_radixsort.argtypes = [ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t]
_lib.vitalis_radixsort.restype = None

_lib.vitalis_binary_search.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_binary_search.restype = ctypes.c_int64

_lib.vitalis_quickselect.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_quickselect.restype = ctypes.c_double

_lib.vitalis_is_sorted.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_is_sorted.restype = ctypes.c_int32

_lib.vitalis_inversion_count.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_inversion_count.restype = ctypes.c_int64

def sort_quicksort(data):
    """QuickSort (in-place, median-of-three). Returns sorted list."""
    n = len(data)
    arr = (ctypes.c_double * n)(*data)
    _lib.vitalis_quicksort(arr, n)
    return list(arr)

def sort_mergesort(data):
    """MergeSort. Returns sorted list."""
    n = len(data)
    arr = (ctypes.c_double * n)(*data)
    _lib.vitalis_mergesort(arr, n)
    return list(arr)

def sort_heapsort(data):
    """HeapSort. Returns sorted list."""
    n = len(data)
    arr = (ctypes.c_double * n)(*data)
    _lib.vitalis_heapsort(arr, n)
    return list(arr)

def sort_radixsort(data):
    """RadixSort for integers. Returns sorted list."""
    n = len(data)
    arr = (ctypes.c_int64 * n)(*data)
    _lib.vitalis_radixsort(arr, n)
    return list(arr)

def sort_binary_search(sorted_data, target):
    """Binary search in sorted array. Returns index or -1."""
    n = len(sorted_data)
    arr = (ctypes.c_double * n)(*sorted_data)
    return _lib.vitalis_binary_search(arr, n, target)

def sort_quickselect(data, k):
    """Find k-th smallest element."""
    n = len(data)
    arr = (ctypes.c_double * n)(*data)
    return _lib.vitalis_quickselect(arr, n, k)

def sort_is_sorted(data):
    """Check if array is sorted."""
    n = len(data)
    arr = (ctypes.c_double * n)(*data)
    return bool(_lib.vitalis_is_sorted(arr, n))

def sort_inversion_count(data):
    """Count inversions in array."""
    n = len(data)
    arr = (ctypes.c_double * n)(*data)
    return _lib.vitalis_inversion_count(arr, n)


# ============================================================================
# v10.0: Automata & Pattern Matching
# ============================================================================

_lib.vitalis_aho_corasick.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t]
_lib.vitalis_aho_corasick.restype = ctypes.c_int32

_lib.vitalis_regex_match.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_regex_match.restype = ctypes.c_int32

_lib.vitalis_levenshtein_within.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_levenshtein_within.restype = ctypes.c_int32

_lib.vitalis_trie_new.argtypes = []
_lib.vitalis_trie_new.restype = ctypes.c_void_p

_lib.vitalis_trie_insert.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_trie_insert.restype = None

_lib.vitalis_trie_contains.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_trie_contains.restype = ctypes.c_int32

_lib.vitalis_trie_free.argtypes = [ctypes.c_void_p]
_lib.vitalis_trie_free.restype = None

_lib.vitalis_bloom_new.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_bloom_new.restype = ctypes.c_void_p

_lib.vitalis_bloom_insert.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_lib.vitalis_bloom_insert.restype = None

_lib.vitalis_bloom_contains.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_lib.vitalis_bloom_contains.restype = ctypes.c_int32

_lib.vitalis_bloom_free.argtypes = [ctypes.c_void_p]
_lib.vitalis_bloom_free.restype = None

def auto_aho_corasick(text, patterns):
    """Aho-Corasick multi-pattern search. Returns number of matches."""
    txt = text.encode() if isinstance(text, str) else text
    pat_bytes = b""
    offsets = []
    for p in patterns:
        pb = p.encode() if isinstance(p, str) else p
        offsets.append(len(pat_bytes))
        pat_bytes += pb
    lens = [len(p.encode() if isinstance(p, str) else p) for p in patterns]
    t = (ctypes.c_uint8 * len(txt))(*txt)
    pb = (ctypes.c_uint8 * len(pat_bytes))(*pat_bytes)
    o = (ctypes.c_size_t * len(patterns))(*offsets)
    l = (ctypes.c_size_t * len(patterns))(*lens)
    out = (ctypes.c_size_t * (len(txt) * len(patterns)))()
    return _lib.vitalis_aho_corasick(t, len(txt), pb, o, len(patterns), l, len(patterns))

def auto_regex_match(pattern, text):
    """Simple regex match. Returns bool."""
    pat = pattern.encode() if isinstance(pattern, str) else pattern
    txt = text.encode() if isinstance(text, str) else text
    p = (ctypes.c_uint8 * len(pat))(*pat)
    t = (ctypes.c_uint8 * len(txt))(*txt)
    return bool(_lib.vitalis_regex_match(p, len(pat), t, len(txt)))

def auto_levenshtein_within(a, b, threshold):
    """Check if Levenshtein distance <= threshold. Returns bool."""
    ab = a.encode() if isinstance(a, str) else a
    bb = b.encode() if isinstance(b, str) else b
    pa = (ctypes.c_uint8 * len(ab))(*ab)
    pb = (ctypes.c_uint8 * len(bb))(*bb)
    return bool(_lib.vitalis_levenshtein_within(pa, len(ab), pb, len(bb), threshold))

class Trie:
    """Trie (prefix tree) backed by native Rust implementation."""
    def __init__(self):
        self._ptr = _lib.vitalis_trie_new()
    def insert(self, word):
        w = word.encode() if isinstance(word, str) else word
        _lib.vitalis_trie_insert(self._ptr, (ctypes.c_uint8 * len(w))(*w), len(w))
    def contains(self, word):
        w = word.encode() if isinstance(word, str) else word
        return bool(_lib.vitalis_trie_contains(self._ptr, (ctypes.c_uint8 * len(w))(*w), len(w)))
    def __del__(self):
        if self._ptr:
            _lib.vitalis_trie_free(self._ptr)
            self._ptr = None

class BloomFilter:
    """Bloom filter backed by native Rust implementation."""
    def __init__(self, size=1024, n_hashes=3):
        self._ptr = _lib.vitalis_bloom_new(size, n_hashes)
    def insert(self, item):
        _lib.vitalis_bloom_insert(self._ptr, item)
    def contains(self, item):
        return bool(_lib.vitalis_bloom_contains(self._ptr, item))
    def __del__(self):
        if self._ptr:
            _lib.vitalis_bloom_free(self._ptr)
            self._ptr = None


# ============================================================================
# v10.0: Combinatorial Optimization
# ============================================================================

_lib.vitalis_knapsack_01.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int32)]
_lib.vitalis_knapsack_01.restype = ctypes.c_double

_lib.vitalis_knapsack_fractional.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_knapsack_fractional.restype = ctypes.c_double

_lib.vitalis_simplex.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_simplex.restype = ctypes.c_double

_lib.vitalis_bin_packing_ffd.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.POINTER(ctypes.c_int32)]
_lib.vitalis_bin_packing_ffd.restype = ctypes.c_int32

_lib.vitalis_coin_change.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t, ctypes.c_int32]
_lib.vitalis_coin_change.restype = ctypes.c_int32

_lib.vitalis_lis_length.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_lis_length.restype = ctypes.c_int32

_lib.vitalis_job_scheduling.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_job_scheduling.restype = ctypes.c_double

_lib.vitalis_activity_selection.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_int32)]
_lib.vitalis_activity_selection.restype = ctypes.c_int32

_lib.vitalis_matrix_chain_order.argtypes = [ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t]
_lib.vitalis_matrix_chain_order.restype = ctypes.c_int64

_lib.vitalis_genetic_sphere.argtypes = [ctypes.c_size_t, ctypes.c_double, ctypes.c_double, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_double, ctypes.c_uint64, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_genetic_sphere.restype = ctypes.c_double

_lib.vitalis_tsp_nearest_neighbor.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_lib.vitalis_tsp_nearest_neighbor.restype = ctypes.c_double

def opt_knapsack_01(weights, values, capacity):
    """0/1 Knapsack DP. Returns (max_value, selected_indices)."""
    n = len(weights)
    w = (ctypes.c_double * n)(*weights)
    v = (ctypes.c_double * n)(*values)
    sel = (ctypes.c_int32 * n)()
    val = _lib.vitalis_knapsack_01(w, v, n, capacity, sel)
    return val, [i for i in range(n) if sel[i]]

def opt_knapsack_fractional(weights, values, capacity):
    """Fractional knapsack (greedy). Returns max value."""
    n = len(weights)
    w = (ctypes.c_double * n)(*weights)
    v = (ctypes.c_double * n)(*values)
    return _lib.vitalis_knapsack_fractional(w, v, n, capacity)

def opt_simplex(a_matrix, b_vec, c_vec, m, n):
    """Simplex LP: maximize c^T x s.t. Ax<=b. Returns (opt_value, x)."""
    a = (ctypes.c_double * len(a_matrix))(*a_matrix)
    b = (ctypes.c_double * m)(*b_vec)
    c = (ctypes.c_double * n)(*c_vec)
    x = (ctypes.c_double * n)()
    val = _lib.vitalis_simplex(a, b, c, m, n, x)
    return val, list(x)

def opt_bin_packing(items, bin_capacity):
    """First Fit Decreasing bin packing. Returns (n_bins, assignments)."""
    n = len(items)
    it = (ctypes.c_double * n)(*items)
    assign = (ctypes.c_int32 * n)()
    bins = _lib.vitalis_bin_packing_ffd(it, n, bin_capacity, assign)
    return bins, list(assign)

def opt_coin_change(coins, amount):
    """Minimum coins to make amount. Returns count or -1."""
    n = len(coins)
    c = (ctypes.c_int32 * n)(*coins)
    return _lib.vitalis_coin_change(c, n, amount)

def opt_lis_length(data):
    """Length of longest increasing subsequence."""
    n = len(data)
    arr = (ctypes.c_double * n)(*data)
    return _lib.vitalis_lis_length(arr, n)

def opt_job_scheduling(starts, ends, values):
    """Weighted job scheduling. Returns max value."""
    n = len(starts)
    s = (ctypes.c_double * n)(*starts)
    e = (ctypes.c_double * n)(*ends)
    v = (ctypes.c_double * n)(*values)
    return _lib.vitalis_job_scheduling(s, e, v, n)

def opt_activity_selection(starts, ends):
    """Activity selection (greedy). Returns (count, selected_mask)."""
    n = len(starts)
    s = (ctypes.c_double * n)(*starts)
    e = (ctypes.c_double * n)(*ends)
    sel = (ctypes.c_int32 * n)()
    count = _lib.vitalis_activity_selection(s, e, n, sel)
    return count, list(sel)

def opt_matrix_chain(dims):
    """Matrix chain multiplication order. Returns min scalar multiplications."""
    n = len(dims) - 1
    d = (ctypes.c_int64 * len(dims))(*dims)
    return _lib.vitalis_matrix_chain_order(d, n)

def opt_genetic_sphere(dimensions, lo=-5.0, hi=5.0, pop=50, generations=100, mutation_rate=0.1, seed=42):
    """Genetic algorithm on sphere function. Returns (fitness, best_solution)."""
    best = (ctypes.c_double * dimensions)()
    fit = _lib.vitalis_genetic_sphere(dimensions, lo, hi, pop, generations, mutation_rate, seed, best)
    return fit, list(best)

def opt_tsp_nearest_neighbor(dist_matrix, n, start=0):
    """TSP nearest-neighbor heuristic. Returns (length, tour)."""
    d = (ctypes.c_double * len(dist_matrix))(*dist_matrix)
    tour = (ctypes.c_size_t * n)()
    length = _lib.vitalis_tsp_nearest_neighbor(d, n, start, tour)
    return length, list(tour)


# ============================================================================
# v13.0: Quantum Algorithms
# ============================================================================

_lib.vitalis_deutsch_jozsa.argtypes = [ctypes.c_uint64, ctypes.c_size_t]
_lib.vitalis_deutsch_jozsa.restype = ctypes.c_int32

_lib.vitalis_bernstein_vazirani.argtypes = [ctypes.c_uint64, ctypes.c_size_t]
_lib.vitalis_bernstein_vazirani.restype = ctypes.c_uint64

_lib.vitalis_qpe.argtypes = [ctypes.c_double, ctypes.c_size_t]
_lib.vitalis_qpe.restype = ctypes.c_double

_lib.vitalis_shor_factor.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
_lib.vitalis_shor_factor.restype = ctypes.c_uint64

_lib.vitalis_vqe_2qubit.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.c_double, ctypes.c_size_t,
]
_lib.vitalis_vqe_2qubit.restype = ctypes.c_double

_lib.vitalis_qaoa_maxcut.argtypes = [
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
]
_lib.vitalis_qaoa_maxcut.restype = ctypes.c_double

_lib.vitalis_quantum_walk_line.argtypes = [ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_quantum_walk_line.restype = ctypes.c_int32

_lib.vitalis_quantum_teleport.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_uint64,
]
_lib.vitalis_quantum_teleport.restype = ctypes.c_double

_lib.vitalis_qec_bitflip.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_int32,
]
_lib.vitalis_qec_bitflip.restype = ctypes.c_double

_lib.vitalis_bb84_qber.argtypes = [ctypes.c_size_t, ctypes.c_int32, ctypes.c_uint64]
_lib.vitalis_bb84_qber.restype = ctypes.c_double

_lib.vitalis_simon.argtypes = [ctypes.c_uint64, ctypes.c_size_t]
_lib.vitalis_simon.restype = ctypes.c_uint64

_lib.vitalis_grover_search.argtypes = [ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint64]
_lib.vitalis_grover_search.restype = ctypes.c_int64


def quantum_deutsch_jozsa(oracle, n):
    """Deutsch-Jozsa algorithm. oracle=bitmask of f(x), n=input qubits. Returns 1=constant, 0=balanced."""
    return _lib.vitalis_deutsch_jozsa(oracle, n)

def quantum_bernstein_vazirani(secret, n):
    """Bernstein-Vazirani: recover hidden string s from f(x)=s·x mod 2. Returns recovered secret."""
    return _lib.vitalis_bernstein_vazirani(secret, n)

def quantum_phase_estimation(phase_fraction, precision_bits):
    """Quantum Phase Estimation. Returns estimated phase (0.0 to 1.0)."""
    return _lib.vitalis_qpe(phase_fraction, precision_bits)

def quantum_shor_factor(n_val, seed=42):
    """Shor's algorithm: factor a semiprime. Returns a non-trivial factor or 0."""
    return _lib.vitalis_shor_factor(n_val, seed)

def quantum_vqe(c_zz, c_z0, c_z1, c_x0, c_x1, initial_params, n_layers, lr=0.01, max_iter=100):
    """Variational Quantum Eigensolver for 2-qubit Hamiltonian. Returns ground state energy."""
    np = n_layers * 4
    params = (ctypes.c_double * np)(*initial_params[:np])
    return _lib.vitalis_vqe_2qubit(c_zz, c_z0, c_z1, c_x0, c_x1, params, n_layers, lr, max_iter)

def quantum_qaoa_maxcut(n_vertices, edges, gamma, beta, p=1):
    """QAOA for MaxCut. edges=[(u,v),...], gamma/beta=[p]. Returns expected cut value."""
    n_edges = len(edges)
    flat_edges = (ctypes.c_size_t * (n_edges * 2))(*[v for e in edges for v in e])
    g = (ctypes.c_double * p)(*gamma[:p])
    b = (ctypes.c_double * p)(*beta[:p])
    return _lib.vitalis_qaoa_maxcut(n_vertices, flat_edges, n_edges, g, b, p)

def quantum_walk_line(n_positions, steps):
    """Discrete quantum walk on a line. Returns probability distribution."""
    probs = (ctypes.c_double * n_positions)()
    _lib.vitalis_quantum_walk_line(n_positions, steps, probs)
    return list(probs)

def quantum_teleport(alpha_re, alpha_im, beta_re, beta_im, seed=42):
    """Quantum teleportation. Returns fidelity (ideally 1.0)."""
    return _lib.vitalis_quantum_teleport(alpha_re, alpha_im, beta_re, beta_im, seed)

def quantum_qec_bitflip(alpha_re, alpha_im, beta_re, beta_im, error_qubit=-1):
    """3-qubit bit-flip error correction. error_qubit=-1 for none. Returns fidelity."""
    return _lib.vitalis_qec_bitflip(alpha_re, alpha_im, beta_re, beta_im, error_qubit)

def quantum_bb84(n_bits, eavesdrop=False, seed=42):
    """BB84 QKD simulation. Returns Quantum Bit Error Rate."""
    return _lib.vitalis_bb84_qber(n_bits, 1 if eavesdrop else 0, seed)

def quantum_simon(secret, n):
    """Simon's algorithm: find hidden period s. Returns recovered secret."""
    return _lib.vitalis_simon(secret, n)

def quantum_grover(n, target, seed=42):
    """Grover's search: find marked item in 2^n items. Returns found index or -1."""
    return _lib.vitalis_grover_search(n, target, seed)


# ============================================================================
# v13.0: Bioinformatics
# ============================================================================

_lib.vitalis_bio_gc_content.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
_lib.vitalis_bio_gc_content.restype = ctypes.c_double

_lib.vitalis_bio_dna_complement.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
_lib.vitalis_bio_dna_complement.restype = ctypes.c_int32

_lib.vitalis_bio_reverse_complement.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
_lib.vitalis_bio_reverse_complement.restype = ctypes.c_int32

_lib.vitalis_bio_transcribe.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
_lib.vitalis_bio_transcribe.restype = ctypes.c_int32

_lib.vitalis_bio_nucleotide_freq.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint64)]
_lib.vitalis_bio_nucleotide_freq.restype = ctypes.c_int32

_lib.vitalis_bio_translate.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
_lib.vitalis_bio_translate.restype = ctypes.c_int32

_lib.vitalis_bio_needleman_wunsch.argtypes = [
    ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
    ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
]
_lib.vitalis_bio_needleman_wunsch.restype = ctypes.c_int32

_lib.vitalis_bio_smith_waterman.argtypes = [
    ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
    ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
]
_lib.vitalis_bio_smith_waterman.restype = ctypes.c_int32

_lib.vitalis_bio_hamming_distance.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
_lib.vitalis_bio_hamming_distance.restype = ctypes.c_int32

_lib.vitalis_bio_edit_distance.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
_lib.vitalis_bio_edit_distance.restype = ctypes.c_int32

_lib.vitalis_bio_kmer_count.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_bio_kmer_count.restype = ctypes.c_uint64

_lib.vitalis_bio_linguistic_complexity.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_bio_linguistic_complexity.restype = ctypes.c_double

_lib.vitalis_bio_hardy_weinberg.argtypes = [ctypes.c_double, ctypes.POINTER(ctypes.c_double)]
_lib.vitalis_bio_hardy_weinberg.restype = ctypes.c_int32

_lib.vitalis_bio_lotka_volterra.argtypes = [
    ctypes.c_double, ctypes.c_double,
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.c_double, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
]
_lib.vitalis_bio_lotka_volterra.restype = ctypes.c_int32

_lib.vitalis_bio_sir_model.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.c_double, ctypes.c_double,
    ctypes.c_double, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double),
]
_lib.vitalis_bio_sir_model.restype = ctypes.c_int32

_lib.vitalis_bio_seir_model.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.c_double, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double),
]
_lib.vitalis_bio_seir_model.restype = ctypes.c_int32

_lib.vitalis_bio_r0.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_bio_r0.restype = ctypes.c_double

_lib.vitalis_bio_michaelis_menten.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_bio_michaelis_menten.restype = ctypes.c_double

_lib.vitalis_bio_competitive_inhibition.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
]
_lib.vitalis_bio_competitive_inhibition.restype = ctypes.c_double

_lib.vitalis_bio_hill_equation.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_bio_hill_equation.restype = ctypes.c_double

_lib.vitalis_bio_jukes_cantor.argtypes = [ctypes.c_double]
_lib.vitalis_bio_jukes_cantor.restype = ctypes.c_double

_lib.vitalis_bio_kimura_distance.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_bio_kimura_distance.restype = ctypes.c_double

_lib.vitalis_bio_protein_mw.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
_lib.vitalis_bio_protein_mw.restype = ctypes.c_double

_lib.vitalis_bio_gravy.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
_lib.vitalis_bio_gravy.restype = ctypes.c_double

_lib.vitalis_bio_logistic_growth.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_bio_logistic_growth.restype = ctypes.c_double

_lib.vitalis_bio_wright_fisher.argtypes = [ctypes.c_double, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint64]
_lib.vitalis_bio_wright_fisher.restype = ctypes.c_double

_lib.vitalis_bio_shannon_diversity.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_bio_shannon_diversity.restype = ctypes.c_double

_lib.vitalis_bio_simpson_diversity.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_bio_simpson_diversity.restype = ctypes.c_double


def bio_gc_content(seq):
    """GC content ratio (0.0-1.0) of a DNA sequence."""
    s = seq.encode() if isinstance(seq, str) else seq
    return _lib.vitalis_bio_gc_content(s, len(s))

def bio_dna_complement(seq):
    """DNA complement (A↔T, G↔C)."""
    s = seq.encode() if isinstance(seq, str) else seq
    out = ctypes.create_string_buffer(len(s))
    _lib.vitalis_bio_dna_complement(s, out, len(s))
    return out.value.decode()

def bio_reverse_complement(seq):
    """Reverse complement of a DNA sequence."""
    s = seq.encode() if isinstance(seq, str) else seq
    out = ctypes.create_string_buffer(len(s))
    _lib.vitalis_bio_reverse_complement(s, out, len(s))
    return out.value.decode()

def bio_transcribe(seq):
    """Transcribe DNA to RNA (T→U)."""
    s = seq.encode() if isinstance(seq, str) else seq
    out = ctypes.create_string_buffer(len(s))
    _lib.vitalis_bio_transcribe(s, out, len(s))
    return out.value.decode()

def bio_nucleotide_freq(seq):
    """Nucleotide frequencies. Returns dict {A, C, G, T}."""
    s = seq.encode() if isinstance(seq, str) else seq
    counts = (ctypes.c_uint64 * 4)()
    _lib.vitalis_bio_nucleotide_freq(s, len(s), counts)
    return {"A": counts[0], "C": counts[1], "G": counts[2], "T": counts[3]}

def bio_translate(seq):
    """Translate DNA/RNA to amino acid sequence."""
    s = seq.encode() if isinstance(seq, str) else seq
    out_cap = len(s) // 3 + 1
    out = ctypes.create_string_buffer(out_cap)
    n = _lib.vitalis_bio_translate(s, len(s), out, out_cap)
    return out.value.decode() if n >= 0 else ""

def bio_needleman_wunsch(seq1, seq2, match_score=1, mismatch_penalty=-1, gap_penalty=-2):
    """Needleman-Wunsch global alignment score."""
    s1 = seq1.encode() if isinstance(seq1, str) else seq1
    s2 = seq2.encode() if isinstance(seq2, str) else seq2
    return _lib.vitalis_bio_needleman_wunsch(s1, len(s1), s2, len(s2), match_score, mismatch_penalty, gap_penalty)

def bio_smith_waterman(seq1, seq2, match_score=2, mismatch_penalty=-1, gap_penalty=-1):
    """Smith-Waterman local alignment score."""
    s1 = seq1.encode() if isinstance(seq1, str) else seq1
    s2 = seq2.encode() if isinstance(seq2, str) else seq2
    return _lib.vitalis_bio_smith_waterman(s1, len(s1), s2, len(s2), match_score, mismatch_penalty, gap_penalty)

def bio_hamming_distance(seq1, seq2):
    """Hamming distance between two equal-length sequences."""
    s1 = seq1.encode() if isinstance(seq1, str) else seq1
    s2 = seq2.encode() if isinstance(seq2, str) else seq2
    return _lib.vitalis_bio_hamming_distance(s1, s2, len(s1))

def bio_edit_distance(seq1, seq2):
    """Edit distance (Levenshtein) between sequences."""
    s1 = seq1.encode() if isinstance(seq1, str) else seq1
    s2 = seq2.encode() if isinstance(seq2, str) else seq2
    return _lib.vitalis_bio_edit_distance(s1, len(s1), s2, len(s2))

def bio_kmer_count(seq, k):
    """Count distinct k-mers in a sequence."""
    s = seq.encode() if isinstance(seq, str) else seq
    return _lib.vitalis_bio_kmer_count(s, len(s), k)

def bio_linguistic_complexity(seq, k):
    """Linguistic complexity: ratio of observed k-mers to possible."""
    s = seq.encode() if isinstance(seq, str) else seq
    return _lib.vitalis_bio_linguistic_complexity(s, len(s), k)

def bio_hardy_weinberg(p):
    """Hardy-Weinberg equilibrium genotype frequencies. Returns (p², 2pq, q²)."""
    freqs = (ctypes.c_double * 3)()
    _lib.vitalis_bio_hardy_weinberg(p, freqs)
    return list(freqs)

def bio_lotka_volterra(prey0, pred0, alpha, beta, delta, gamma, dt, steps):
    """Lotka-Volterra predator-prey simulation. Returns (prey_series, predator_series)."""
    prey = (ctypes.c_double * (steps + 1))()
    pred = (ctypes.c_double * (steps + 1))()
    _lib.vitalis_bio_lotka_volterra(prey0, pred0, alpha, beta, delta, gamma, dt, steps, prey, pred)
    return list(prey), list(pred)

def bio_sir_model(s0, i0, r0, beta, gamma_rate, dt, steps):
    """SIR epidemic model. Returns list of (S, I, R) tuples."""
    out = (ctypes.c_double * ((steps + 1) * 3))()
    _lib.vitalis_bio_sir_model(s0, i0, r0, beta, gamma_rate, dt, steps, out)
    return [(out[i*3], out[i*3+1], out[i*3+2]) for i in range(steps + 1)]

def bio_seir_model(s0, e0, i0, r0, beta, sigma, gamma_rate, dt, steps):
    """SEIR epidemic model. Returns list of (S, E, I, R) tuples."""
    out = (ctypes.c_double * ((steps + 1) * 4))()
    _lib.vitalis_bio_seir_model(s0, e0, i0, r0, beta, sigma, gamma_rate, dt, steps, out)
    return [(out[i*4], out[i*4+1], out[i*4+2], out[i*4+3]) for i in range(steps + 1)]

def bio_r0(beta, gamma_rate):
    """Basic reproduction number R₀ = β / γ."""
    return _lib.vitalis_bio_r0(beta, gamma_rate)

def bio_michaelis_menten(substrate, vmax, km):
    """Michaelis-Menten enzyme kinetics: v = Vmax*[S]/(Km+[S])."""
    return _lib.vitalis_bio_michaelis_menten(substrate, vmax, km)

def bio_competitive_inhibition(substrate, inhibitor, vmax, km, ki):
    """Competitive enzyme inhibition."""
    return _lib.vitalis_bio_competitive_inhibition(substrate, inhibitor, vmax, km, ki)

def bio_hill_equation(substrate, vmax, k, n):
    """Hill equation: cooperative binding kinetics."""
    return _lib.vitalis_bio_hill_equation(substrate, vmax, k, n)

def bio_jukes_cantor(p):
    """Jukes-Cantor evolutionary distance from fraction of differing sites."""
    return _lib.vitalis_bio_jukes_cantor(p)

def bio_kimura_distance(p_transitions, q_transversions):
    """Kimura 2-parameter evolutionary distance."""
    return _lib.vitalis_bio_kimura_distance(p_transitions, q_transversions)

def bio_protein_mw(seq):
    """Protein molecular weight from amino acid sequence (Da)."""
    s = seq.encode() if isinstance(seq, str) else seq
    return _lib.vitalis_bio_protein_mw(s, len(s))

def bio_gravy(seq):
    """GRAVY (Grand Average of Hydropathicity) index."""
    s = seq.encode() if isinstance(seq, str) else seq
    return _lib.vitalis_bio_gravy(s, len(s))

def bio_logistic_growth(p0, r, k, t):
    """Logistic growth model: P(t) = K / (1 + ((K-P0)/P0)*exp(-rt))."""
    return _lib.vitalis_bio_logistic_growth(p0, r, k, t)

def bio_wright_fisher(initial_freq, pop_size, generations, seed=42):
    """Wright-Fisher genetic drift simulation. Returns final allele frequency."""
    return _lib.vitalis_bio_wright_fisher(initial_freq, pop_size, generations, seed)

def bio_shannon_diversity(abundances):
    """Shannon diversity index H' = -Σ(pᵢ * ln(pᵢ))."""
    n = len(abundances)
    a = (ctypes.c_double * n)(*abundances)
    return _lib.vitalis_bio_shannon_diversity(a, n)

def bio_simpson_diversity(abundances):
    """Simpson's diversity index D = 1 - Σ(pᵢ²)."""
    n = len(abundances)
    a = (ctypes.c_double * n)(*abundances)
    return _lib.vitalis_bio_simpson_diversity(a, n)


# ============================================================================
# v13.0: Advanced Chemistry & Physics
# ============================================================================

# Acid-base
_lib.vitalis_chem_henderson_hasselbalch.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_henderson_hasselbalch.restype = ctypes.c_double
_lib.vitalis_chem_buffer_capacity.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_buffer_capacity.restype = ctypes.c_double
_lib.vitalis_chem_ionization_fraction.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_ionization_fraction.restype = ctypes.c_double

# Equilibrium
_lib.vitalis_chem_keq_from_gibbs.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_keq_from_gibbs.restype = ctypes.c_double
_lib.vitalis_chem_gibbs_free_energy.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_gibbs_free_energy.restype = ctypes.c_double
_lib.vitalis_chem_vant_hoff.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_vant_hoff.restype = ctypes.c_double
_lib.vitalis_chem_clausius_clapeyron.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_clausius_clapeyron.restype = ctypes.c_double

# Kinetics
_lib.vitalis_chem_first_order.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_first_order.restype = ctypes.c_double
_lib.vitalis_chem_second_order.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_second_order.restype = ctypes.c_double
_lib.vitalis_chem_half_life_first_order.argtypes = [ctypes.c_double]
_lib.vitalis_chem_half_life_first_order.restype = ctypes.c_double
_lib.vitalis_chem_eyring.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_eyring.restype = ctypes.c_double
_lib.vitalis_chem_arrhenius.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_arrhenius.restype = ctypes.c_double

# Electrochemistry
_lib.vitalis_chem_butler_volmer.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_butler_volmer.restype = ctypes.c_double
_lib.vitalis_chem_tafel.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_tafel.restype = ctypes.c_double
_lib.vitalis_chem_faraday_mass.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_faraday_mass.restype = ctypes.c_double

# Statistical Mechanics
_lib.vitalis_chem_boltzmann_prob.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_boltzmann_prob.restype = ctypes.c_double
_lib.vitalis_chem_partition_function.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_chem_partition_function.restype = ctypes.c_double
_lib.vitalis_chem_fermi_dirac.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_fermi_dirac.restype = ctypes.c_double
_lib.vitalis_chem_bose_einstein.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_bose_einstein.restype = ctypes.c_double
_lib.vitalis_chem_maxwell_boltzmann_speed.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_maxwell_boltzmann_speed.restype = ctypes.c_double
_lib.vitalis_chem_mean_thermal_energy.argtypes = [ctypes.c_double]
_lib.vitalis_chem_mean_thermal_energy.restype = ctypes.c_double
_lib.vitalis_chem_einstein_specific_heat.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_einstein_specific_heat.restype = ctypes.c_double
_lib.vitalis_chem_debye_specific_heat.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_size_t]
_lib.vitalis_chem_debye_specific_heat.restype = ctypes.c_double

# Special Relativity
_lib.vitalis_phys_lorentz_factor.argtypes = [ctypes.c_double]
_lib.vitalis_phys_lorentz_factor.restype = ctypes.c_double
_lib.vitalis_phys_time_dilation.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_phys_time_dilation.restype = ctypes.c_double
_lib.vitalis_phys_length_contraction.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_phys_length_contraction.restype = ctypes.c_double
_lib.vitalis_phys_relativistic_momentum.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_phys_relativistic_momentum.restype = ctypes.c_double
_lib.vitalis_phys_relativistic_energy.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_phys_relativistic_energy.restype = ctypes.c_double
_lib.vitalis_phys_relativistic_kinetic_energy.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_phys_relativistic_kinetic_energy.restype = ctypes.c_double
_lib.vitalis_phys_velocity_addition.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_phys_velocity_addition.restype = ctypes.c_double
_lib.vitalis_phys_mass_energy.argtypes = [ctypes.c_double]
_lib.vitalis_phys_mass_energy.restype = ctypes.c_double
_lib.vitalis_phys_relativistic_doppler.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_phys_relativistic_doppler.restype = ctypes.c_double

# General Relativity
_lib.vitalis_phys_schwarzschild_radius.argtypes = [ctypes.c_double]
_lib.vitalis_phys_schwarzschild_radius.restype = ctypes.c_double
_lib.vitalis_phys_gravitational_time_dilation.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_phys_gravitational_time_dilation.restype = ctypes.c_double
_lib.vitalis_phys_gravitational_redshift.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_phys_gravitational_redshift.restype = ctypes.c_double
_lib.vitalis_phys_isco_radius.argtypes = [ctypes.c_double]
_lib.vitalis_phys_isco_radius.restype = ctypes.c_double

# Material Science
_lib.vitalis_mat_hooke_stress.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_mat_hooke_stress.restype = ctypes.c_double
_lib.vitalis_mat_thermal_expansion.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_mat_thermal_expansion.restype = ctypes.c_double
_lib.vitalis_mat_poisson_transverse_strain.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_mat_poisson_transverse_strain.restype = ctypes.c_double
_lib.vitalis_mat_bulk_modulus.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_mat_bulk_modulus.restype = ctypes.c_double
_lib.vitalis_mat_shear_modulus.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_mat_shear_modulus.restype = ctypes.c_double
_lib.vitalis_mat_fourier_heat_flux.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_mat_fourier_heat_flux.restype = ctypes.c_double

# Quantum Chemistry
_lib.vitalis_chem_hydrogen_energy.argtypes = [ctypes.c_int32]
_lib.vitalis_chem_hydrogen_energy.restype = ctypes.c_double
_lib.vitalis_chem_rydberg_wavelength.argtypes = [ctypes.c_int32, ctypes.c_int32]
_lib.vitalis_chem_rydberg_wavelength.restype = ctypes.c_double
_lib.vitalis_chem_de_broglie.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_de_broglie.restype = ctypes.c_double
_lib.vitalis_chem_heisenberg_min_dp.argtypes = [ctypes.c_double]
_lib.vitalis_chem_heisenberg_min_dp.restype = ctypes.c_double
_lib.vitalis_chem_particle_in_box.argtypes = [ctypes.c_int32, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_particle_in_box.restype = ctypes.c_double
_lib.vitalis_chem_harmonic_oscillator_energy.argtypes = [ctypes.c_int32, ctypes.c_double]
_lib.vitalis_chem_harmonic_oscillator_energy.restype = ctypes.c_double
_lib.vitalis_chem_morse_potential.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_morse_potential.restype = ctypes.c_double

# Gas Laws
_lib.vitalis_chem_ideal_gas_pressure.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_ideal_gas_pressure.restype = ctypes.c_double
_lib.vitalis_chem_van_der_waals_pressure.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_van_der_waals_pressure.restype = ctypes.c_double
_lib.vitalis_chem_compressibility.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_chem_compressibility.restype = ctypes.c_double


def chem_henderson_hasselbalch(pka, conj_base, acid):
    """Henderson-Hasselbalch equation: pH = pKa + log10([A⁻]/[HA])."""
    return _lib.vitalis_chem_henderson_hasselbalch(pka, conj_base, acid)

def chem_buffer_capacity(c_total, ka, h_conc):
    """Buffer capacity β."""
    return _lib.vitalis_chem_buffer_capacity(c_total, ka, h_conc)

def chem_ionization_fraction(ka, h_conc):
    """Fraction of acid ionized: α = Ka / (Ka + [H⁺])."""
    return _lib.vitalis_chem_ionization_fraction(ka, h_conc)

def chem_keq_from_gibbs(delta_g, temperature):
    """Equilibrium constant from Gibbs free energy: K = exp(-ΔG/RT)."""
    return _lib.vitalis_chem_keq_from_gibbs(delta_g, temperature)

def chem_gibbs_free_energy(delta_h, temperature, delta_s):
    """Gibbs free energy: ΔG = ΔH - TΔS."""
    return _lib.vitalis_chem_gibbs_free_energy(delta_h, temperature, delta_s)

def chem_vant_hoff(k1, delta_h, t1, t2):
    """Van't Hoff equation: predict K at new temperature."""
    return _lib.vitalis_chem_vant_hoff(k1, delta_h, t1, t2)

def chem_clausius_clapeyron(p1, delta_h_vap, t1, t2):
    """Clausius-Clapeyron equation: predict vapor pressure at T2."""
    return _lib.vitalis_chem_clausius_clapeyron(p1, delta_h_vap, t1, t2)

def chem_first_order(a0, k, t):
    """First-order reaction: [A](t) = [A]₀ * e^{-kt}."""
    return _lib.vitalis_chem_first_order(a0, k, t)

def chem_second_order(a0, k, t):
    """Second-order reaction: [A](t) = [A]₀ / (1 + [A]₀*k*t)."""
    return _lib.vitalis_chem_second_order(a0, k, t)

def chem_half_life_first_order(k):
    """First-order half-life: t½ = ln(2)/k."""
    return _lib.vitalis_chem_half_life_first_order(k)

def chem_eyring(temperature, delta_g_barrier):
    """Eyring equation: transition state theory rate constant."""
    return _lib.vitalis_chem_eyring(temperature, delta_g_barrier)

def chem_arrhenius_advanced(a_factor, ea, temperature):
    """Arrhenius equation: k = A * exp(-Ea/RT)."""
    return _lib.vitalis_chem_arrhenius(a_factor, ea, temperature)

def chem_butler_volmer(j0, alpha_a, alpha_c, eta, temperature):
    """Butler-Volmer equation for electrode kinetics."""
    return _lib.vitalis_chem_butler_volmer(j0, alpha_a, alpha_c, eta, temperature)

def chem_tafel(a, b, j, j0):
    """Tafel equation: η = a + b*log10(|j/j₀|)."""
    return _lib.vitalis_chem_tafel(a, b, j, j0)

def chem_faraday_mass(current, time, molar_mass, n_electrons):
    """Faradaic mass deposition: m = (I*t*M)/(n*F)."""
    return _lib.vitalis_chem_faraday_mass(current, time, molar_mass, n_electrons)

def chem_boltzmann_prob(energy, temperature, partition_fn):
    """Boltzmann probability: P(E) ∝ exp(-E/kBT)/Z."""
    return _lib.vitalis_chem_boltzmann_prob(energy, temperature, partition_fn)

def chem_partition_function(energies, temperature):
    """Partition function: Z = Σ exp(-Eᵢ/kBT)."""
    n = len(energies)
    e = (ctypes.c_double * n)(*energies)
    return _lib.vitalis_chem_partition_function(e, n, temperature)

def chem_fermi_dirac(energy, mu, temperature):
    """Fermi-Dirac distribution: f(E) = 1/(exp((E-μ)/kBT)+1)."""
    return _lib.vitalis_chem_fermi_dirac(energy, mu, temperature)

def chem_bose_einstein(energy, mu, temperature):
    """Bose-Einstein distribution: n(E) = 1/(exp((E-μ)/kBT)-1)."""
    return _lib.vitalis_chem_bose_einstein(energy, mu, temperature)

def chem_maxwell_boltzmann_speed(velocity, mass, temperature):
    """Maxwell-Boltzmann speed distribution f(v)."""
    return _lib.vitalis_chem_maxwell_boltzmann_speed(velocity, mass, temperature)

def chem_mean_thermal_energy(temperature):
    """Mean thermal energy: ⟨E⟩ = (3/2)kBT."""
    return _lib.vitalis_chem_mean_thermal_energy(temperature)

def chem_einstein_specific_heat(einstein_temp, temperature):
    """Einstein model of specific heat."""
    return _lib.vitalis_chem_einstein_specific_heat(einstein_temp, temperature)

def chem_debye_specific_heat(debye_temp, temperature, n_steps=500):
    """Debye model of specific heat."""
    return _lib.vitalis_chem_debye_specific_heat(debye_temp, temperature, n_steps)

def phys_lorentz_factor(velocity):
    """Lorentz factor: γ = 1/√(1 - v²/c²)."""
    return _lib.vitalis_phys_lorentz_factor(velocity)

def phys_time_dilation(proper_time, velocity):
    """Time dilation: Δt' = γΔt₀."""
    return _lib.vitalis_phys_time_dilation(proper_time, velocity)

def phys_length_contraction(proper_length, velocity):
    """Length contraction: L' = L₀/γ."""
    return _lib.vitalis_phys_length_contraction(proper_length, velocity)

def phys_relativistic_momentum(mass, velocity):
    """Relativistic momentum: p = γmv."""
    return _lib.vitalis_phys_relativistic_momentum(mass, velocity)

def phys_relativistic_energy(mass, velocity):
    """Relativistic energy: E = γmc²."""
    return _lib.vitalis_phys_relativistic_energy(mass, velocity)

def phys_relativistic_kinetic_energy(mass, velocity):
    """Relativistic kinetic energy: K = (γ-1)mc²."""
    return _lib.vitalis_phys_relativistic_kinetic_energy(mass, velocity)

def phys_velocity_addition(u, v):
    """Relativistic velocity addition: u' = (u+v)/(1+uv/c²)."""
    return _lib.vitalis_phys_velocity_addition(u, v)

def phys_mass_energy_equiv(mass):
    """Mass-energy equivalence: E = mc²."""
    return _lib.vitalis_phys_mass_energy(mass)

def phys_relativistic_doppler(f0, velocity):
    """Relativistic Doppler: f' = f₀√((1+β)/(1-β))."""
    return _lib.vitalis_phys_relativistic_doppler(f0, velocity)

def phys_schwarzschild_radius_adv(mass):
    """Schwarzschild radius: rs = 2GM/c²."""
    return _lib.vitalis_phys_schwarzschild_radius(mass)

def phys_gravitational_time_dilation(proper_time, mass, radius):
    """Gravitational time dilation near massive body."""
    return _lib.vitalis_phys_gravitational_time_dilation(proper_time, mass, radius)

def phys_gravitational_redshift(mass, radius):
    """Gravitational redshift: z = 1/√(1-rs/r) - 1."""
    return _lib.vitalis_phys_gravitational_redshift(mass, radius)

def phys_isco_radius(mass):
    """Innermost Stable Circular Orbit: r_isco = 6GM/c²."""
    return _lib.vitalis_phys_isco_radius(mass)

def mat_hooke_stress(youngs_modulus, strain):
    """Hooke's law: σ = E * ε."""
    return _lib.vitalis_mat_hooke_stress(youngs_modulus, strain)

def mat_thermal_expansion(length, alpha, delta_t):
    """Thermal expansion: ΔL = L₀ * α * ΔT."""
    return _lib.vitalis_mat_thermal_expansion(length, alpha, delta_t)

def mat_poisson_transverse_strain(axial_strain, poisson_ratio):
    """Transverse strain from Poisson's ratio."""
    return _lib.vitalis_mat_poisson_transverse_strain(axial_strain, poisson_ratio)

def mat_bulk_modulus(youngs_modulus, poisson_ratio):
    """Bulk modulus: K = E/(3(1-2ν))."""
    return _lib.vitalis_mat_bulk_modulus(youngs_modulus, poisson_ratio)

def mat_shear_modulus(youngs_modulus, poisson_ratio):
    """Shear modulus: G = E/(2(1+ν))."""
    return _lib.vitalis_mat_shear_modulus(youngs_modulus, poisson_ratio)

def mat_fourier_heat_flux(k, dt_dx):
    """Fourier heat flux: q = -k * dT/dx."""
    return _lib.vitalis_mat_fourier_heat_flux(k, dt_dx)

def chem_hydrogen_energy(n):
    """Hydrogen atom energy level: En = -13.6/n² eV."""
    return _lib.vitalis_chem_hydrogen_energy(n)

def chem_rydberg_wavelength(n1, n2):
    """Rydberg formula wavelength for hydrogen transitions."""
    return _lib.vitalis_chem_rydberg_wavelength(n1, n2)

def chem_de_broglie_wavelength(mass, velocity):
    """De Broglie wavelength: λ = h/(mv)."""
    return _lib.vitalis_chem_de_broglie(mass, velocity)

def chem_heisenberg_min_dp(delta_x):
    """Heisenberg minimum Δp given Δx."""
    return _lib.vitalis_chem_heisenberg_min_dp(delta_x)

def chem_particle_in_box(n, mass, length):
    """Particle in a box energy: En = n²h²/(8mL²)."""
    return _lib.vitalis_chem_particle_in_box(n, mass, length)

def chem_harmonic_oscillator_energy(n, omega):
    """Quantum harmonic oscillator: En = ℏω(n + ½)."""
    return _lib.vitalis_chem_harmonic_oscillator_energy(n, omega)

def chem_morse_potential(r, r_eq, de, a):
    """Morse potential: V(r) = De*(1-exp(-a(r-re)))²."""
    return _lib.vitalis_chem_morse_potential(r, r_eq, de, a)

def chem_ideal_gas_pressure_adv(n_moles, volume, temperature):
    """Ideal gas law: PV = nRT. Returns pressure."""
    return _lib.vitalis_chem_ideal_gas_pressure(n_moles, volume, temperature)

def chem_van_der_waals_pressure(n_moles, volume, temperature, a, b):
    """Van der Waals equation of state. Returns pressure."""
    return _lib.vitalis_chem_van_der_waals_pressure(n_moles, volume, temperature, a, b)

def chem_compressibility(pressure, volume, n_moles, temperature):
    """Compressibility factor: Z = PV/(nRT)."""
    return _lib.vitalis_chem_compressibility(pressure, volume, n_moles, temperature)


# ============================================================================
# v13.0: Neuromorphic Computing
# ============================================================================

# Neuron models
_lib.vitalis_neuro_lif.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.c_double, ctypes.c_double,
    ctypes.POINTER(ctypes.c_uint8),
]
_lib.vitalis_neuro_lif.restype = ctypes.c_int32

_lib.vitalis_neuro_izhikevich.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_uint8),
]
_lib.vitalis_neuro_izhikevich.restype = ctypes.c_int32

_lib.vitalis_neuro_adex.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.c_double, ctypes.c_double,
    ctypes.POINTER(ctypes.c_uint8),
]
_lib.vitalis_neuro_adex.restype = ctypes.c_int32

# Plasticity
_lib.vitalis_neuro_hebbian_update.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
    ctypes.c_double, ctypes.c_double,
]
_lib.vitalis_neuro_hebbian_update.restype = ctypes.c_int32

_lib.vitalis_neuro_stdp_delta.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_neuro_stdp_delta.restype = ctypes.c_double

_lib.vitalis_neuro_stdp_update.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
    ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.c_double,
]
_lib.vitalis_neuro_stdp_update.restype = ctypes.c_int32

_lib.vitalis_neuro_bcm_update.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_lib.vitalis_neuro_bcm_update.restype = ctypes.c_double

_lib.vitalis_neuro_homeostatic_scaling.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double, ctypes.c_double,
    ctypes.POINTER(ctypes.c_double),
]
_lib.vitalis_neuro_homeostatic_scaling.restype = ctypes.c_int32

# Neural coding
_lib.vitalis_neuro_firing_rate.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_neuro_firing_rate.restype = ctypes.c_double

_lib.vitalis_neuro_isi_stats.argtypes = [
    ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_double,
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
]
_lib.vitalis_neuro_isi_stats.restype = ctypes.c_int32

_lib.vitalis_neuro_population_decode.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
]
_lib.vitalis_neuro_population_decode.restype = ctypes.c_double

_lib.vitalis_neuro_fano_factor.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_neuro_fano_factor.restype = ctypes.c_double

# Network topology
_lib.vitalis_neuro_small_world_network.argtypes = [
    ctypes.c_size_t, ctypes.c_size_t, ctypes.c_double, ctypes.POINTER(ctypes.c_double), ctypes.c_uint64,
]
_lib.vitalis_neuro_small_world_network.restype = ctypes.c_int32

_lib.vitalis_neuro_scale_free_network.argtypes = [
    ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_uint64,
]
_lib.vitalis_neuro_scale_free_network.restype = ctypes.c_int32

# Reservoir computing
_lib.vitalis_neuro_esn_forward.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double),
    ctypes.c_double,
    ctypes.POINTER(ctypes.c_double),
]
_lib.vitalis_neuro_esn_forward.restype = ctypes.c_int32

# Spike analysis
_lib.vitalis_neuro_spike_correlation.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_neuro_spike_correlation.restype = ctypes.c_double

_lib.vitalis_neuro_spike_entropy.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_neuro_spike_entropy.restype = ctypes.c_double

_lib.vitalis_neuro_burst_detection.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t]
_lib.vitalis_neuro_burst_detection.restype = ctypes.c_int32

# Neuroevolution
_lib.vitalis_neuro_neat_compatibility.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.c_double, ctypes.c_double, ctypes.c_double,
]
_lib.vitalis_neuro_neat_compatibility.restype = ctypes.c_double

_lib.vitalis_neuro_sigmoid.argtypes = [ctypes.c_double, ctypes.c_double]
_lib.vitalis_neuro_sigmoid.restype = ctypes.c_double

_lib.vitalis_neuro_mutual_information.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
_lib.vitalis_neuro_mutual_information.restype = ctypes.c_double

# Oscillations
_lib.vitalis_neuro_kuramoto_step.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.c_double, ctypes.c_double,
]
_lib.vitalis_neuro_kuramoto_step.restype = ctypes.c_double


def neuro_lif(currents, tau_m=20.0, v_rest=-65.0, v_threshold=-55.0, v_reset=-70.0, r_membrane=10.0, dt=0.1):
    """Leaky Integrate-and-Fire neuron. Returns (spike_count, spike_train)."""
    n = len(currents)
    inp = (ctypes.c_double * n)(*currents)
    spikes = (ctypes.c_uint8 * n)()
    count = _lib.vitalis_neuro_lif(inp, n, tau_m, v_rest, v_threshold, v_reset, r_membrane, dt, spikes)
    return count, list(spikes)

def neuro_izhikevich(currents, a=0.02, b=0.2, c=-65.0, d=8.0, dt=0.5):
    """Izhikevich neuron model. Returns (spike_count, v_trace, u_trace, spikes)."""
    n = len(currents)
    inp = (ctypes.c_double * n)(*currents)
    v_out = (ctypes.c_double * n)()
    u_out = (ctypes.c_double * n)()
    spikes = (ctypes.c_uint8 * n)()
    count = _lib.vitalis_neuro_izhikevich(inp, n, a, b, c, d, dt, v_out, u_out, spikes)
    return count, list(v_out), list(u_out), list(spikes)

def neuro_adex(currents, tau_m=20.0, v_rest=-70.0, v_threshold=-50.0, delta_t=2.0,
               a=4.0, b=0.0805, tau_w=150.0, v_reset=-58.0, c_m=281.0, dt=0.1):
    """Adaptive Exponential IF neuron. Returns (spike_count, spikes)."""
    n = len(currents)
    inp = (ctypes.c_double * n)(*currents)
    spikes = (ctypes.c_uint8 * n)()
    count = _lib.vitalis_neuro_adex(inp, n, tau_m, v_rest, v_threshold, delta_t, a, b, tau_w, v_reset, c_m, dt, spikes)
    return count, list(spikes)

def neuro_hebbian_update(weights, n_pre, n_post, pre_activity, post_activity, lr=0.01, w_max=1.0):
    """Hebbian learning: Δw = η * x_pre * x_post. Modifies weights in-place. Returns new weights."""
    w = (ctypes.c_double * (n_pre * n_post))(*weights)
    pre = (ctypes.c_double * n_pre)(*pre_activity)
    post = (ctypes.c_double * n_post)(*post_activity)
    _lib.vitalis_neuro_hebbian_update(w, n_pre, n_post, pre, post, lr, w_max)
    return list(w)

def neuro_stdp_delta(dt_spike, a_plus=0.01, a_minus=0.012, tau_plus=20.0, tau_minus=20.0):
    """STDP weight change for a single spike pair."""
    return _lib.vitalis_neuro_stdp_delta(dt_spike, a_plus, a_minus, tau_plus, tau_minus)

def neuro_stdp_update(weights, n_pre, n_post, pre_times, post_times,
                      a_plus=0.01, a_minus=0.012, tau_plus=20.0, tau_minus=20.0, w_max=1.0):
    """Apply STDP to weight matrix. Returns updated weights."""
    w = (ctypes.c_double * (n_pre * n_post))(*weights)
    pre = (ctypes.c_double * n_pre)(*pre_times)
    post = (ctypes.c_double * n_post)(*post_times)
    _lib.vitalis_neuro_stdp_update(w, n_pre, n_post, pre, post, a_plus, a_minus, tau_plus, tau_minus, w_max)
    return list(w)

def neuro_bcm_update(weight, pre, post, theta, lr=0.01, w_max=1.0):
    """BCM learning rule: Δw = η*x*y*(y-θ). Returns new weight."""
    return _lib.vitalis_neuro_bcm_update(weight, pre, post, theta, lr, w_max)

def neuro_homeostatic_scaling(rates, target_rate, tau_homeo=100.0):
    """Homeostatic scaling factors to maintain target firing rate."""
    n = len(rates)
    r = (ctypes.c_double * n)(*rates)
    out = (ctypes.c_double * n)()
    _lib.vitalis_neuro_homeostatic_scaling(r, n, target_rate, tau_homeo, out)
    return list(out)

def neuro_firing_rate(spikes, dt=0.001):
    """Compute firing rate (Hz) from binary spike train."""
    n = len(spikes)
    s = (ctypes.c_uint8 * n)(*spikes)
    return _lib.vitalis_neuro_firing_rate(s, n, dt)

def neuro_isi_stats(spikes, dt=0.001):
    """Inter-spike interval statistics. Returns (n_isi, mean_isi, cv_isi)."""
    n = len(spikes)
    s = (ctypes.c_uint8 * n)(*spikes)
    mean_isi = ctypes.c_double(0.0)
    cv_isi = ctypes.c_double(0.0)
    n_isi = _lib.vitalis_neuro_isi_stats(s, n, dt, ctypes.byref(mean_isi), ctypes.byref(cv_isi))
    return n_isi, mean_isi.value, cv_isi.value

def neuro_population_decode(preferred_stimuli, responses):
    """Population vector decoding. Returns decoded stimulus value."""
    n = len(preferred_stimuli)
    pref = (ctypes.c_double * n)(*preferred_stimuli)
    resp = (ctypes.c_double * n)(*responses)
    return _lib.vitalis_neuro_population_decode(pref, resp, n)

def neuro_fano_factor(spikes, window_size=100):
    """Fano factor: variance/mean of spike counts in windows."""
    n = len(spikes)
    s = (ctypes.c_uint8 * n)(*spikes)
    return _lib.vitalis_neuro_fano_factor(s, n, window_size)

def neuro_small_world_network(n, k=4, beta=0.3, seed=42):
    """Generate Watts-Strogatz small-world network. Returns adjacency matrix."""
    adj = (ctypes.c_double * (n * n))()
    _lib.vitalis_neuro_small_world_network(n, k, beta, adj, seed)
    return [list(adj[i*n:(i+1)*n]) for i in range(n)]

def neuro_scale_free_network(n, m=2, seed=42):
    """Generate Barabási-Albert scale-free network. Returns adjacency matrix."""
    adj = (ctypes.c_double * (n * n))()
    _lib.vitalis_neuro_scale_free_network(n, m, adj, seed)
    return [list(adj[i*n:(i+1)*n]) for i in range(n)]

def neuro_esn_forward(input_data, n_steps, input_dim, reservoir_weights, res_size, input_weights, leak_rate=0.3):
    """Echo State Network forward pass. Returns state matrix [n_steps × res_size]."""
    inp = (ctypes.c_double * (n_steps * input_dim))(*input_data)
    w_res = (ctypes.c_double * (res_size * res_size))(*reservoir_weights)
    w_in = (ctypes.c_double * (res_size * input_dim))(*input_weights)
    out = (ctypes.c_double * (n_steps * res_size))()
    _lib.vitalis_neuro_esn_forward(inp, n_steps, input_dim, w_res, res_size, w_in, leak_rate, out)
    return [list(out[t*res_size:(t+1)*res_size]) for t in range(n_steps)]

def neuro_spike_correlation(spikes1, spikes2):
    """Cross-correlation between two spike trains at lag 0."""
    n = len(spikes1)
    s1 = (ctypes.c_uint8 * n)(*spikes1)
    s2 = (ctypes.c_uint8 * n)(*spikes2)
    return _lib.vitalis_neuro_spike_correlation(s1, s2, n)

def neuro_spike_entropy(spikes):
    """Spike train entropy (bits per bin)."""
    n = len(spikes)
    s = (ctypes.c_uint8 * n)(*spikes)
    return _lib.vitalis_neuro_spike_entropy(s, n)

def neuro_burst_detection(spikes, max_isi=5, min_spikes=3):
    """Detect bursts in spike train. Returns burst count."""
    n = len(spikes)
    s = (ctypes.c_uint8 * n)(*spikes)
    return _lib.vitalis_neuro_burst_detection(s, n, max_isi, min_spikes)

def neuro_neat_compatibility(genes1, n1, genes2, n2, c1=1.0, c2=1.0, c3=0.4):
    """NEAT genome compatibility distance. genes=[n×3] (innovation, weight, enabled)."""
    g1 = (ctypes.c_double * (n1 * 3))(*genes1)
    g2 = (ctypes.c_double * (n2 * 3))(*genes2)
    return _lib.vitalis_neuro_neat_compatibility(g1, n1, g2, n2, c1, c2, c3)

def neuro_sigmoid(x, steepness=1.0):
    """Sigmoid with configurable steepness."""
    return _lib.vitalis_neuro_sigmoid(x, steepness)

def neuro_mutual_information(spikes_in, spikes_out):
    """Mutual information between input and output spike trains (bits)."""
    n = len(spikes_in)
    si = (ctypes.c_uint8 * n)(*spikes_in)
    so = (ctypes.c_uint8 * n)(*spikes_out)
    return _lib.vitalis_neuro_mutual_information(si, so, n)

def neuro_kuramoto_step(phases, frequencies, coupling=1.0, dt=0.01):
    """Kuramoto coupled oscillator step. Returns (order_parameter, updated_phases)."""
    n = len(phases)
    ph = (ctypes.c_double * n)(*phases)
    freq = (ctypes.c_double * n)(*frequencies)
    r = _lib.vitalis_neuro_kuramoto_step(ph, freq, n, coupling, dt)
    return r, list(ph)


# ============================================================================
# v13.0: Advanced Evolutionary Computation
# ============================================================================

_lib.vitalis_evo_differential_evolution.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t,
    ctypes.c_int32, ctypes.c_double, ctypes.c_double,
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
    ctypes.POINTER(ctypes.c_double),
    ctypes.c_uint64,
]
_lib.vitalis_evo_differential_evolution.restype = ctypes.c_double

_lib.vitalis_evo_pso_step.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
    ctypes.c_size_t, ctypes.c_size_t,
    ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ctypes.c_int32,
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
    ctypes.c_uint64,
]
_lib.vitalis_evo_pso_step.restype = ctypes.c_double

_lib.vitalis_evo_cma_es_step.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.c_size_t, ctypes.c_int32,
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
    ctypes.c_uint64,
]
_lib.vitalis_evo_cma_es_step.restype = ctypes.c_double

_lib.vitalis_evo_nsga2_sort.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_int32),
]
_lib.vitalis_evo_nsga2_sort.restype = ctypes.c_int32

_lib.vitalis_evo_crowding_distance.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double),
]
_lib.vitalis_evo_crowding_distance.restype = ctypes.c_int32

_lib.vitalis_evo_novelty_score.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
]
_lib.vitalis_evo_novelty_score.restype = ctypes.c_double

_lib.vitalis_evo_map_elites_insert.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
    ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double), ctypes.c_double,
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
]
_lib.vitalis_evo_map_elites_insert.restype = ctypes.c_int32

_lib.vitalis_evo_map_elites_coverage.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
_lib.vitalis_evo_map_elites_coverage.restype = ctypes.c_size_t

_lib.vitalis_evo_island_migrate.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
    ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,
    ctypes.c_size_t,
]
_lib.vitalis_evo_island_migrate.restype = ctypes.c_int32

_lib.vitalis_evo_simulated_annealing.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
    ctypes.c_int32,
    ctypes.c_double, ctypes.c_double,
    ctypes.c_size_t, ctypes.c_double,
    ctypes.c_uint64,
]
_lib.vitalis_evo_simulated_annealing.restype = ctypes.c_double

_lib.vitalis_evo_coevolution_fitness.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double),
    ctypes.c_size_t, ctypes.c_uint64,
]
_lib.vitalis_evo_coevolution_fitness.restype = ctypes.c_double

_lib.vitalis_evo_adapt_f.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_evo_adapt_f.restype = ctypes.c_double

_lib.vitalis_evo_adapt_cr.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double]
_lib.vitalis_evo_adapt_cr.restype = ctypes.c_double

_lib.vitalis_evo_fitness_distance_correlation.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_double),
]
_lib.vitalis_evo_fitness_distance_correlation.restype = ctypes.c_double


def evo_differential_evolution(population, pop_size, dim, fitness_fn_id=0,
                               f_weight=0.8, crossover_rate=0.9,
                               bounds_lo=None, bounds_hi=None, seed=42):
    """Differential evolution (DE/rand/1/bin). Returns (best_fitness, population, fitness_values).
    fitness_fn_id: 0=sphere, 1=rastrigin, 2=rosenbrock, 3=ackley, 4=griewank."""
    pop = (ctypes.c_double * (pop_size * dim))(*population)
    fit = (ctypes.c_double * pop_size)()
    lo = (ctypes.c_double * dim)(*bounds_lo) if bounds_lo else None
    hi = (ctypes.c_double * dim)(*bounds_hi) if bounds_hi else None
    best = _lib.vitalis_evo_differential_evolution(pop, pop_size, dim, fitness_fn_id,
                                                    f_weight, crossover_rate, lo, hi, fit, seed)
    return best, list(pop), list(fit)

def evo_pso_step(positions, velocities, p_best, p_best_fit, g_best, g_best_fit,
                 n, dim, w_inertia=0.7, c1=1.5, c2=1.5, fitness_fn_id=0,
                 bounds_lo=None, bounds_hi=None, seed=42):
    """Particle Swarm Optimization step. Returns (global_best_fitness, positions, g_best)."""
    pos = (ctypes.c_double * (n * dim))(*positions)
    vel = (ctypes.c_double * (n * dim))(*velocities)
    pb = (ctypes.c_double * (n * dim))(*p_best)
    pbf = (ctypes.c_double * n)(*p_best_fit)
    gb = (ctypes.c_double * dim)(*g_best)
    gbf = ctypes.c_double(g_best_fit)
    lo = (ctypes.c_double * dim)(*bounds_lo) if bounds_lo else None
    hi = (ctypes.c_double * dim)(*bounds_hi) if bounds_hi else None
    best = _lib.vitalis_evo_pso_step(pos, vel, pb, pbf, gb, ctypes.byref(gbf),
                                      n, dim, w_inertia, c1, c2, fitness_fn_id, lo, hi, seed)
    return best, list(pos), list(gb)

def evo_cma_es_step(mean, sigma, dim, lam=20, fitness_fn_id=0, seed=42):
    """CMA-ES step. Returns (best_fitness, mean, sigma, population, fitness)."""
    m = (ctypes.c_double * dim)(*mean)
    s = ctypes.c_double(sigma)
    pop = (ctypes.c_double * (lam * dim))()
    fit = (ctypes.c_double * lam)()
    best = _lib.vitalis_evo_cma_es_step(m, ctypes.byref(s), dim, lam, fitness_fn_id, pop, fit, seed)
    return best, list(m), s.value, list(pop), list(fit)

def evo_nsga2_sort(objectives, n, n_obj):
    """NSGA-II non-dominated sorting. Returns (n_fronts, ranks)."""
    obj = (ctypes.c_double * (n * n_obj))(*objectives)
    ranks = (ctypes.c_int32 * n)()
    nf = _lib.vitalis_evo_nsga2_sort(obj, n, n_obj, ranks)
    return nf, list(ranks)

def evo_crowding_distance(objectives, n_obj, front_indices):
    """NSGA-II crowding distance for a Pareto front. Returns distances."""
    fs = len(front_indices)
    obj = (ctypes.c_double * len(objectives))(*objectives)
    fi = (ctypes.c_size_t * fs)(*front_indices)
    dist = (ctypes.c_double * fs)()
    _lib.vitalis_evo_crowding_distance(obj, n_obj, fi, fs, dist)
    return list(dist)

def evo_novelty_score(behaviors, dim, query, k_nearest=15):
    """Novelty score: mean distance to k-nearest in behavior space."""
    n = len(behaviors) // dim
    beh = (ctypes.c_double * (n * dim))(*behaviors)
    q = (ctypes.c_double * dim)(*query)
    return _lib.vitalis_evo_novelty_score(beh, n, dim, q, k_nearest)

def evo_map_elites_insert(archive_fitness, archive_solutions, grid_size, dim,
                           solution, fitness, behavior, grid_dims, behavior_lo, behavior_hi):
    """MAP-Elites insert. Returns cell index or -1 if rejected."""
    bd = len(behavior)
    af = (ctypes.c_double * grid_size)(*archive_fitness)
    as_ = (ctypes.c_double * (grid_size * dim))(*archive_solutions)
    sol = (ctypes.c_double * dim)(*solution)
    beh = (ctypes.c_double * bd)(*behavior)
    gd = (ctypes.c_size_t * bd)(*grid_dims)
    blo = (ctypes.c_double * bd)(*behavior_lo)
    bhi = (ctypes.c_double * bd)(*behavior_hi)
    cell = _lib.vitalis_evo_map_elites_insert(af, as_, grid_size, dim, sol, fitness, beh, bd, gd, blo, bhi)
    return cell, list(af), list(as_)

def evo_map_elites_coverage(archive_fitness):
    """Count non-empty cells in MAP-Elites archive."""
    n = len(archive_fitness)
    af = (ctypes.c_double * n)(*archive_fitness)
    return _lib.vitalis_evo_map_elites_coverage(af, n)

def evo_island_migrate(islands, island_fitness, n_islands, island_size, dim, n_migrants=2):
    """Island model migration (ring topology). Returns updated (islands, fitness)."""
    total = n_islands * island_size
    pop = (ctypes.c_double * (total * dim))(*islands)
    fit = (ctypes.c_double * total)(*island_fitness)
    _lib.vitalis_evo_island_migrate(pop, fit, n_islands, island_size, dim, n_migrants)
    return list(pop), list(fit)

def evo_simulated_annealing(initial, dim, fitness_fn_id=0,
                             initial_temp=100.0, cooling_rate=0.995,
                             n_iterations=1000, step_size=0.5, seed=42):
    """Simulated annealing. Returns (best_fitness, best_solution)."""
    x = (ctypes.c_double * dim)(*initial)
    best = _lib.vitalis_evo_simulated_annealing(x, dim, fitness_fn_id,
                                                 initial_temp, cooling_rate, n_iterations, step_size, seed)
    return best, list(x)

def evo_coevolution_fitness(solutions, n, dim, n_opponents=5, seed=42):
    """Competitive coevolution: evaluate fitness as win rate. Returns (mean_fitness, fitness_vec)."""
    sol = (ctypes.c_double * (n * dim))(*solutions)
    fit = (ctypes.c_double * n)()
    mean = _lib.vitalis_evo_coevolution_fitness(sol, n, dim, fit, n_opponents, seed)
    return mean, list(fit)

def evo_adapt_f(success_f, current_f):
    """Adaptive F for DE (Lehmer mean of successes)."""
    n = len(success_f)
    sf = (ctypes.c_double * n)(*success_f)
    return _lib.vitalis_evo_adapt_f(sf, n, current_f)

def evo_adapt_cr(success_cr, current_cr):
    """Adaptive CR for DE (arithmetic mean of successes)."""
    n = len(success_cr)
    scr = (ctypes.c_double * n)(*success_cr)
    return _lib.vitalis_evo_adapt_cr(scr, n, current_cr)

def evo_fitness_distance_correlation(solutions, fitness, dim, optimum):
    """Fitness-distance correlation. Returns FDC coefficient (-1 to 1)."""
    n = len(fitness)
    sol = (ctypes.c_double * (n * dim))(*solutions)
    fit = (ctypes.c_double * n)(*fitness)
    opt = (ctypes.c_double * dim)(*optimum)
    return _lib.vitalis_evo_fitness_distance_correlation(sol, fit, n, dim, opt)


# ============================================================================
# v123: Engine, Memory, and Meta-Evolution API
# ============================================================================

# ─── Engine API ──────────────────────────────────────────────────────────

# i32 vitalis_engine_init()
_lib.vitalis_engine_init.argtypes = []
_lib.vitalis_engine_init.restype = ctypes.c_int32

# i32 vitalis_engine_register(const char* name, const char* source)
_lib.vitalis_engine_register.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_lib.vitalis_engine_register.restype = ctypes.c_int32

# char* vitalis_engine_evolve(const char* name, const char* new_source)
_lib.vitalis_engine_evolve.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_lib.vitalis_engine_evolve.restype = ctypes.c_void_p

# char* vitalis_engine_validate(const char* source)
_lib.vitalis_engine_validate.argtypes = [ctypes.c_char_p]
_lib.vitalis_engine_validate.restype = ctypes.c_void_p

# char* vitalis_engine_cycle()
_lib.vitalis_engine_cycle.argtypes = []
_lib.vitalis_engine_cycle.restype = ctypes.c_void_p

# char* vitalis_engine_stats()
_lib.vitalis_engine_stats.argtypes = []
_lib.vitalis_engine_stats.restype = ctypes.c_void_p

# char* vitalis_engine_landscape()
_lib.vitalis_engine_landscape.argtypes = []
_lib.vitalis_engine_landscape.restype = ctypes.c_void_p

# char* vitalis_engine_population_summary()
_lib.vitalis_engine_population_summary.argtypes = []
_lib.vitalis_engine_population_summary.restype = ctypes.c_void_p

# char* vitalis_engine_diagnostics()
_lib.vitalis_engine_diagnostics.argtypes = []
_lib.vitalis_engine_diagnostics.restype = ctypes.c_void_p

# char* vitalis_engine_error_log(u64 limit)
_lib.vitalis_engine_error_log.argtypes = [ctypes.c_uint64]
_lib.vitalis_engine_error_log.restype = ctypes.c_void_p


def engine_init() -> bool:
    """Initialize the Vitalis evolution engine. Returns True on success."""
    return _lib.vitalis_engine_init() == 1


def engine_register(name: str, source: str) -> bool:
    """Register a function for engine-managed evolution. Returns True on success."""
    return _lib.vitalis_engine_register(name.encode("utf-8"), source.encode("utf-8")) == 1


def engine_evolve(name: str, new_source: str) -> dict:
    """Evolve a function with a new variant. Returns JSON result dict."""
    ptr = _lib.vitalis_engine_evolve(name.encode("utf-8"), new_source.encode("utf-8"))
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"error": result}


def engine_validate(source: str) -> dict:
    """Validate Vitalis source code. Returns validation result dict."""
    ptr = _lib.vitalis_engine_validate(source.encode("utf-8"))
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"error": result}


def engine_cycle() -> dict:
    """Run one full evolution cycle. Returns cycle result dict."""
    ptr = _lib.vitalis_engine_cycle()
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"error": result}


def engine_stats() -> dict:
    """Get engine statistics as a dict."""
    ptr = _lib.vitalis_engine_stats()
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"raw": result}


def engine_landscape() -> dict:
    """Get the fitness landscape (all functions + scores)."""
    ptr = _lib.vitalis_engine_landscape()
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"raw": result}


def engine_population_summary() -> dict:
    """Get population-wide summary (diversity, fitness stats, total variants)."""
    ptr = _lib.vitalis_engine_population_summary()
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"raw": result}


def engine_diagnostics() -> dict:
    """Get runtime diagnostics (OS/platform, compile latency, error log)."""
    ptr = _lib.vitalis_engine_diagnostics()
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"raw": result}


def engine_error_log(limit: int = 0) -> list:
    """Get the last N error log entries (0 = all, up to 200)."""
    ptr = _lib.vitalis_engine_error_log(limit)
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else []
    except json.JSONDecodeError:
        return []


# ─── Memory (Engram) API ────────────────────────────────────────────────

# u64 vitalis_memory_store(u32 kind, const char* content, const char* tags_json, f64 importance, const char* context, u64 cycle)
_lib.vitalis_memory_store.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_double, ctypes.c_char_p, ctypes.c_uint64]
_lib.vitalis_memory_store.restype = ctypes.c_uint64

# char* vitalis_memory_recall(const char* tag, u64 cycle)
_lib.vitalis_memory_recall.argtypes = [ctypes.c_char_p, ctypes.c_uint64]
_lib.vitalis_memory_recall.restype = ctypes.c_void_p

# i32 vitalis_memory_forget(u64 id)
_lib.vitalis_memory_forget.argtypes = [ctypes.c_uint64]
_lib.vitalis_memory_forget.restype = ctypes.c_int32

# void vitalis_memory_decay(u64 cycle)
_lib.vitalis_memory_decay.argtypes = [ctypes.c_uint64]
_lib.vitalis_memory_decay.restype = None

# char* vitalis_memory_consolidate(u64 cycle)
_lib.vitalis_memory_consolidate.argtypes = [ctypes.c_uint64]
_lib.vitalis_memory_consolidate.restype = ctypes.c_void_p

# char* vitalis_memory_stats()
_lib.vitalis_memory_stats.argtypes = []
_lib.vitalis_memory_stats.restype = ctypes.c_void_p

# u64 vitalis_memory_count()
_lib.vitalis_memory_count.argtypes = []
_lib.vitalis_memory_count.restype = ctypes.c_uint64


ENGRAM_EPISODIC = 0
ENGRAM_SEMANTIC = 1
ENGRAM_PROCEDURAL = 2
ENGRAM_WORKING = 3
ENGRAM_EMOTIONAL = 4


def memory_store(kind: int, content: str, tags: list[str], importance: float = 1.0, context: str = "", cycle: int = 0) -> int:
    """Store a new engram. kind: 0=episodic, 1=semantic, 2=procedural, 3=working, 4=emotional. Returns engram ID."""
    tags_json = json.dumps(tags)
    return _lib.vitalis_memory_store(
        kind, content.encode("utf-8"), tags_json.encode("utf-8"),
        importance, context.encode("utf-8"), cycle,
    )


def memory_recall(tag: str, cycle: int = 0) -> list[dict]:
    """Recall memories by tag. Returns list of engram dicts."""
    ptr = _lib.vitalis_memory_recall(tag.encode("utf-8"), cycle)
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else []
    except json.JSONDecodeError:
        return []


def memory_forget(engram_id: int) -> bool:
    """Forget a specific memory by ID. Returns True on success."""
    return _lib.vitalis_memory_forget(engram_id) == 1


def memory_decay(cycle: int) -> None:
    """Apply decay to all memories."""
    _lib.vitalis_memory_decay(cycle)


def memory_consolidate(cycle: int = 0) -> dict:
    """Consolidate memories. Returns result dict."""
    ptr = _lib.vitalis_memory_consolidate(cycle)
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"raw": result}


def memory_stats() -> dict:
    """Get memory statistics."""
    ptr = _lib.vitalis_memory_stats()
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"raw": result}


def memory_count() -> int:
    """Get total active engram count."""
    return _lib.vitalis_memory_count()


# ─── Meta-Evolution API ─────────────────────────────────────────────────

# char* vitalis_meta_select_strategy()
_lib.vitalis_meta_select_strategy.argtypes = []
_lib.vitalis_meta_select_strategy.restype = ctypes.c_void_p

# void vitalis_meta_record_result(const char* strategy, const char* function, f64, f64, i32, u64)
_lib.vitalis_meta_record_result.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_double, ctypes.c_double, ctypes.c_int32, ctypes.c_uint64]
_lib.vitalis_meta_record_result.restype = None

# char* vitalis_meta_evolve()
_lib.vitalis_meta_evolve.argtypes = []
_lib.vitalis_meta_evolve.restype = ctypes.c_void_p

# char* vitalis_meta_landscape()
_lib.vitalis_meta_landscape.argtypes = []
_lib.vitalis_meta_landscape.restype = ctypes.c_void_p

# char* vitalis_meta_stats()
_lib.vitalis_meta_stats.argtypes = []
_lib.vitalis_meta_stats.restype = ctypes.c_void_p

# char* vitalis_meta_active_params()
_lib.vitalis_meta_active_params.argtypes = []
_lib.vitalis_meta_active_params.restype = ctypes.c_void_p

# u64 vitalis_meta_strategy_count()
_lib.vitalis_meta_strategy_count.argtypes = []
_lib.vitalis_meta_strategy_count.restype = ctypes.c_uint64


def meta_select_strategy() -> str:
    """Select the best evolution strategy. Returns strategy name."""
    ptr = _lib.vitalis_meta_select_strategy()
    return _read_and_free(ptr) or "balanced"


def meta_record_result(strategy: str, function_name: str, fitness_before: float, fitness_after: float, success: bool, cycle: int = 0) -> None:
    """Record a strategy result for meta-evolution learning."""
    _lib.vitalis_meta_record_result(
        strategy.encode("utf-8"), function_name.encode("utf-8"),
        fitness_before, fitness_after, 1 if success else 0, cycle,
    )


def meta_evolve() -> dict:
    """Run a meta-evolution cycle. Returns result dict."""
    ptr = _lib.vitalis_meta_evolve()
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"raw": result}


def meta_landscape() -> dict:
    """Get the strategy landscape."""
    ptr = _lib.vitalis_meta_landscape()
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"raw": result}


def meta_stats() -> dict:
    """Get meta-evolution statistics."""
    ptr = _lib.vitalis_meta_stats()
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"raw": result}


def meta_active_params() -> dict:
    """Get active strategy parameters."""
    ptr = _lib.vitalis_meta_active_params()
    result = _read_and_free(ptr)
    try:
        return json.loads(result) if result else {}
    except json.JSONDecodeError:
        return {"raw": result}


def meta_strategy_count() -> int:
    """Get the number of evolution strategies."""
    return _lib.vitalis_meta_strategy_count()


# ============================================================================
# Module metadata
# ============================================================================

__version__ = version()
__all__ = [
    # Core compiler
    "compile_and_run", "run_file", "check", "parse_ast", "lex", "dump_ir", "version",
    # Evolution
    "evo_load", "evo_register", "evo_evolve", "evo_set_fitness", "evo_get_fitness",
    "evo_get_generation", "evo_list", "evo_run", "evo_get_source", "evo_rollback",
    # Hot-path native ops
    "hotpath_sliding_window_count", "hotpath_token_bucket",
    "hotpath_p95", "hotpath_percentile", "hotpath_mean",
    "hotpath_weighted_score", "hotpath_code_quality_score",
    "hotpath_tally_votes", "hotpath_tally_string_votes",
    "hotpath_cognitive_complexity",
    "hotpath_quantum_anneal_accept", "hotpath_bayesian_ucb",
    "hotpath_boltzmann_select", "hotpath_shannon_diversity",
    "hotpath_pareto_dominates", "hotpath_pareto_front",
    "hotpath_cma_es_mean_update", "hotpath_ema_update",
    "hotpath_levy_step", "hotpath_adaptive_fitness",
    "hotpath_cosine_similarity", "hotpath_l2_normalize",
    "hotpath_stddev", "hotpath_median",
    "hotpath_exponential_moving_average", "hotpath_entropy",
    "hotpath_min_max_normalize", "hotpath_hamming_distance",
    "hotpath_softmax", "hotpath_cross_entropy",
    "hotpath_batch_sigmoid", "hotpath_argmax", "hotpath_batch_relu",
    "hotpath_batch_leaky_relu", "hotpath_batch_norm",
    "hotpath_kl_divergence", "hotpath_gelu_batch", "hotpath_clip",
    "hotpath_layer_norm", "hotpath_dropout_mask",
    "hotpath_cosine_distance", "hotpath_huber_loss", "hotpath_mse_loss",
    # v7.0: Signal Processing
    "fft", "ifft", "power_spectrum", "convolve", "cross_correlate",
    "window_hann", "window_hamming", "window_blackman",
    "fir_filter", "iir_biquad", "zero_crossing_rate", "rms_energy",
    "spectral_centroid", "autocorrelation", "resample_linear",
    # v7.0: Crypto
    "sha256", "hmac_sha256", "base64_encode", "base64_decode",
    "crc32", "fnv1a_64", "constant_time_eq", "xorshift128plus",
    # v7.0: Graph
    "bfs", "dfs", "dijkstra", "has_cycle", "is_bipartite",
    "connected_components", "toposort", "pagerank", "tarjan_scc",
    # v7.0: String Algorithms
    "levenshtein", "lcs_length", "lcs_string", "longest_common_substring",
    "str_hamming_distance", "jaro_winkler", "soundex",
    "is_rotation", "ngram_count", "kmp_search", "rabin_karp", "bmh_search",
    # v7.0: Numerical
    "mat_mul", "mat_det", "mat_inverse", "solve_linear",
    "simpson", "trapezoid", "horner", "lagrange_interp",
    "power_iteration", "mat_trace", "dot_product", "vec_norm",
    "cross_product", "newton_root", "bisection_root",
    # v7.0: Compression
    "rle_encode", "rle_decode", "huffman_encode",
    "delta_encode", "delta_decode", "lz77_compress",
    # v7.0: Probability & Statistics
    "stats_mean", "stats_median", "stats_variance", "stats_stddev",
    "stats_skewness", "stats_kurtosis", "stats_mode",
    "normal_pdf", "normal_cdf", "normal_inv_cdf",
    "exponential_pdf", "exponential_cdf",
    "poisson_pmf", "binomial_pmf",
    "pearson_correlation", "spearman_correlation", "linear_regression",
    "data_entropy", "chi_squared", "ks_statistic",
    # v9.0: Quantum
    "QuantumRegister",
    # v9.0: Quantum Math
    "complex_mul", "complex_abs", "complex_exp",
    "gamma", "lgamma", "beta_func", "bessel_j0", "bessel_j1",
    "riemann_zeta", "monte_carlo_pi", "monte_carlo_integrate",
    "rk4_step", "rk4_solve", "mod_pow", "is_prime", "gcd", "lcm",
    "haar_forward", "haar_inverse", "legendre_poly", "assoc_legendre",
    "fibonacci", "golden_ratio", "euler_totient",
    "quat_mul", "quat_rotate", "quat_slerp",
    "quantum_anneal_prob", "outer_product", "kronecker_product",
    # v9.0: Advanced Math
    "math_factorial", "math_binomial", "math_catalan", "math_erf",
    "mandelbrot", "partition_count", "bell_number",
    # v9.0: Science
    "physical_constant",
    "kinematic_v", "kinematic_s", "kinematic_v_from_s",
    "kinetic_energy", "potential_energy", "pendulum_period",
    "orbital_velocity", "escape_velocity",
    "projectile_range", "projectile_max_height",
    "ideal_gas_pressure", "ideal_gas_temperature",
    "carnot_efficiency", "radiation_power", "heat_transfer", "entropy_change",
    "coulomb_force", "electric_field", "ohms_law_v",
    "electrical_power", "capacitor_energy", "magnetic_force",
    "wavelength", "photon_energy", "doppler", "snell", "de_broglie",
    "decay_constant", "radioactive_decay", "activity", "mass_energy",
    "ph", "poh", "arrhenius", "nernst", "dilution",
    "schwarzschild_radius", "luminosity", "hubble_velocity", "redshift",
    "reynolds_number", "drag_force", "bernoulli_pressure",
    "celsius_to_kelvin", "kelvin_to_celsius",
    "ev_to_joules", "joules_to_ev", "deg_to_rad", "rad_to_deg",
    # v9.0: Analytics
    "sma", "ema", "wma", "dema",
    "anomaly_zscore", "anomaly_iqr", "anomaly_mad",
    "linear_trend", "turning_points",
    "ses_forecast", "holt_forecast",
    "minmax_scale", "zscore_normalize",
    "sla_uptime", "error_rate", "throughput", "apdex", "mtbf", "mttr",
    "cardinality",
    # v9.0: Security
    "validate_email", "validate_ipv4", "validate_range",
    "validate_length", "validate_url",
    "detect_sqli", "detect_xss", "detect_path_traversal",
    "detect_command_injection",
    "password_strength", "password_entropy",
    "check_memory_quota", "check_time_budget", "check_recursion_depth",
    "resource_utilization", "code_safety_score",
    "audit_hash", "hash_chain",
    "sec_token_bucket_check", "sec_sliding_window_check",
    "sandbox_grant", "sandbox_revoke", "sandbox_check", "sandbox_count",
    "html_escape",
    # v9.0: Scoring
    "maintainability_index", "tech_debt_ratio", "code_quality_composite",
    "halstead_metrics", "weighted_fitness",
    "score_pareto_dominates", "pareto_rank",
    "elo_update", "elo_expected",
    "welch_t", "cohens_d", "mann_whitney_u",
    "conversion_rate", "bayesian_ab",
    "regression_score", "regression_count",
    "geometric_mean", "harmonic_mean", "power_mean_val",
    "latency_score", "efficiency_ratio", "throughput_efficiency",
    "system_health", "score_decay_fitness", "sigmoid_fitness",
    "tournament_fitness",
    # v10.0: Machine Learning
    "ml_kmeans", "ml_knn", "ml_naive_bayes", "ml_logistic_regression",
    "ml_logistic_predict", "ml_pca", "ml_decision_stump", "ml_dbscan",
    "ml_accuracy", "ml_precision", "ml_recall", "ml_f1",
    "ml_mse", "ml_mae", "ml_r2", "ml_cosine_similarity", "ml_silhouette",
    # v10.0: Computational Geometry
    "geo_convex_hull", "geo_point_in_polygon", "geo_line_intersection",
    "geo_closest_pair", "geo_polygon_area", "geo_polygon_perimeter",
    "geo_triangle_area", "geo_is_convex", "geo_distance_3d",
    # v10.0: Sorting & Searching
    "sort_quicksort", "sort_mergesort", "sort_heapsort", "sort_radixsort",
    "sort_binary_search", "sort_quickselect", "sort_is_sorted", "sort_inversion_count",
    # v10.0: Automata & Pattern Matching
    "auto_aho_corasick", "auto_regex_match", "auto_levenshtein_within",
    "Trie", "BloomFilter",
    # v10.0: Combinatorial Optimization
    "opt_knapsack_01", "opt_knapsack_fractional", "opt_simplex",
    "opt_bin_packing", "opt_coin_change", "opt_lis_length",
    "opt_job_scheduling", "opt_activity_selection", "opt_matrix_chain",
    "opt_genetic_sphere", "opt_tsp_nearest_neighbor",
    # v13.0: Quantum Algorithms
    "quantum_deutsch_jozsa", "quantum_bernstein_vazirani", "quantum_phase_estimation",
    "quantum_shor_factor", "quantum_vqe", "quantum_qaoa_maxcut",
    "quantum_walk_line", "quantum_teleport", "quantum_qec_bitflip",
    "quantum_bb84", "quantum_simon", "quantum_grover",
    # v13.0: Bioinformatics
    "bio_gc_content", "bio_dna_complement", "bio_reverse_complement",
    "bio_transcribe", "bio_nucleotide_freq", "bio_translate",
    "bio_needleman_wunsch", "bio_smith_waterman",
    "bio_hamming_distance", "bio_edit_distance",
    "bio_kmer_count", "bio_linguistic_complexity",
    "bio_hardy_weinberg", "bio_lotka_volterra",
    "bio_sir_model", "bio_seir_model", "bio_r0",
    "bio_michaelis_menten", "bio_competitive_inhibition", "bio_hill_equation",
    "bio_jukes_cantor", "bio_kimura_distance",
    "bio_protein_mw", "bio_gravy",
    "bio_logistic_growth", "bio_wright_fisher",
    "bio_shannon_diversity", "bio_simpson_diversity",
    # v13.0: Advanced Chemistry
    "chem_henderson_hasselbalch", "chem_buffer_capacity", "chem_ionization_fraction",
    "chem_keq_from_gibbs", "chem_gibbs_free_energy", "chem_vant_hoff", "chem_clausius_clapeyron",
    "chem_first_order", "chem_second_order", "chem_half_life_first_order",
    "chem_eyring", "chem_arrhenius_advanced",
    "chem_butler_volmer", "chem_tafel", "chem_faraday_mass",
    "chem_boltzmann_prob", "chem_partition_function",
    "chem_fermi_dirac", "chem_bose_einstein",
    "chem_maxwell_boltzmann_speed", "chem_mean_thermal_energy",
    "chem_einstein_specific_heat", "chem_debye_specific_heat",
    "chem_hydrogen_energy", "chem_rydberg_wavelength",
    "chem_de_broglie_wavelength", "chem_heisenberg_min_dp",
    "chem_particle_in_box", "chem_harmonic_oscillator_energy", "chem_morse_potential",
    "chem_ideal_gas_pressure_adv", "chem_van_der_waals_pressure", "chem_compressibility",
    # v13.0: Advanced Physics (Relativity)
    "phys_lorentz_factor", "phys_time_dilation", "phys_length_contraction",
    "phys_relativistic_momentum", "phys_relativistic_energy", "phys_relativistic_kinetic_energy",
    "phys_velocity_addition", "phys_mass_energy_equiv", "phys_relativistic_doppler",
    "phys_schwarzschild_radius_adv", "phys_gravitational_time_dilation",
    "phys_gravitational_redshift", "phys_isco_radius",
    # v13.0: Material Science
    "mat_hooke_stress", "mat_thermal_expansion", "mat_poisson_transverse_strain",
    "mat_bulk_modulus", "mat_shear_modulus", "mat_fourier_heat_flux",
    # v13.0: Neuromorphic Computing
    "neuro_lif", "neuro_izhikevich", "neuro_adex",
    "neuro_hebbian_update", "neuro_stdp_delta", "neuro_stdp_update",
    "neuro_bcm_update", "neuro_homeostatic_scaling",
    "neuro_firing_rate", "neuro_isi_stats", "neuro_population_decode", "neuro_fano_factor",
    "neuro_small_world_network", "neuro_scale_free_network",
    "neuro_esn_forward",
    "neuro_spike_correlation", "neuro_spike_entropy", "neuro_burst_detection",
    "neuro_neat_compatibility", "neuro_sigmoid", "neuro_mutual_information",
    "neuro_kuramoto_step",
    # v13.0: Advanced Evolutionary Computation
    "evo_differential_evolution", "evo_pso_step", "evo_cma_es_step",
    "evo_nsga2_sort", "evo_crowding_distance",
    "evo_novelty_score", "evo_map_elites_insert", "evo_map_elites_coverage",
    "evo_island_migrate", "evo_simulated_annealing",
    "evo_coevolution_fitness", "evo_adapt_f", "evo_adapt_cr",
    "evo_fitness_distance_correlation",
    # v15.0: JIT stdlib wrappers
    "v15_str_upper", "v15_str_lower", "v15_str_contains", "v15_str_len",
    "v15_str_index_of", "v15_str_split_count", "v15_parse_int",
    "v15_map_new_set_get", "v15_map_len",
    "v15_error_set_check", "v15_file_roundtrip",
    "v15_pid", "v15_epoch_secs",
    "v15_fibonacci", "v15_factorial", "v15_is_prime", "v15_gcd",
    # v123: Engine API
    "engine_init", "engine_register", "engine_evolve", "engine_validate",
    "engine_cycle", "engine_stats", "engine_landscape", "engine_population_summary",
    "engine_diagnostics", "engine_error_log",
    # v123: Memory (Engram) API
    "memory_store", "memory_recall", "memory_forget", "memory_decay",
    "memory_consolidate", "memory_stats", "memory_count",
    "ENGRAM_EPISODIC", "ENGRAM_SEMANTIC", "ENGRAM_PROCEDURAL", "ENGRAM_WORKING", "ENGRAM_EMOTIONAL",
    # v123: Meta-Evolution API
    "meta_select_strategy", "meta_record_result", "meta_evolve",
    "meta_landscape", "meta_stats", "meta_active_params", "meta_strategy_count",
]

