"""
The Forge — Vitalis Compiler Integration (Elite FFI Edition)

Primary path: ctypes DLL — zero subprocess overhead, <5ms compile+JIT.
Fallback path: cargo run CLI — ~400ms, used only if DLL unavailable.

Exposes the full Vitalis compiler pipeline:
  - lex / parse_ast / dump_ir / type_check / compile_and_run
  - validate_safety (capability gate)
  - Native evolution engine (slang_evo_*)
  - Hotpath native Rust ops (hotpath_*)
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from .models import CompilationGate, CompilationResult

# ── DLL State ────────────────────────────────────────────────────────────────

_ffi: Optional[object] = None          # vitalis_ffi module
_ffi_ready: Optional[bool] = None      # None = untested, True/False = result

VITALIS_ROOT = Path(os.getenv("VITALIS_ROOT", r"C:\Vitalis-V60"))
VITALIS_CARGO = VITALIS_ROOT / "Cargo.toml"


def _ensure_ffi() -> bool:
    """Lazy-load the Vitalis FFI wrapper. Returns True if DLL available."""
    global _ffi, _ffi_ready
    if _ffi_ready is not None:
        return _ffi_ready
    try:
        from . import vitalis_ffi as v
        ver = v.version()           # smoke-test: raises if DLL missing/broken
        _ffi = v
        _ffi_ready = True
        dll_size_mb = _dll_size_mb()
        print(f"[Forge] ✅  Vitalis FFI → {ver}  ({dll_size_mb}MB DLL, FFI mode active)")
    except Exception as exc:
        _ffi_ready = False
        print(f"[Forge] ⚠️   Vitalis FFI unavailable ({exc}) — CLI fallback (~400ms/call)")
    return _ffi_ready


def _dll_size_mb() -> int:
    for suffix in ("release", "debug"):
        p = VITALIS_ROOT / "target" / suffix / "vitalis.dll"
        if p.exists():
            return p.stat().st_size // 1024 // 1024
    return 0


# ── FFI Paths (<5ms) ─────────────────────────────────────────────────────────

def _ffi_run(source: str, timeout: float) -> CompilationResult:
    """Compile and run via FFI — zero subprocess overhead.
    Pre-validates with type-checker to avoid DLL panics on malformed code.
    """
    t0 = time.perf_counter()
    try:
        # Pre-flight: type-check first (safe — no codegen, no panic risk)
        errors = _ffi.check(source)
        if errors:
            ms = (time.perf_counter() - t0) * 1000
            return CompilationResult(
                success=False,
                gate_reached=CompilationGate.TYPE_CHECK,
                error="; ".join(errors),
                compile_time_ms=ms,
            )
        # Safe to JIT now
        val = _ffi.compile_and_run(source)
        return CompilationResult(
            success=True, gate_reached=CompilationGate.EXECUTE,
            output=str(val), compile_time_ms=(time.perf_counter() - t0) * 1000,
        )
    except RuntimeError as e:
        return CompilationResult(
            success=False, gate_reached=_gate_from_error(str(e)),
            error=str(e), compile_time_ms=(time.perf_counter() - t0) * 1000,
        )
    except Exception as e:
        # FFI crash fallback — switch to CLI for this call
        ms = (time.perf_counter() - t0) * 1000
        cli_result = _cli_run(source, timeout)
        cli_result = CompilationResult(
            success=cli_result.success,
            gate_reached=cli_result.gate_reached,
            output=cli_result.output,
            error=cli_result.error,
            compile_time_ms=cli_result.compile_time_ms + ms,
            warnings=cli_result.warnings,
        )
        return cli_result


def _ffi_check(source: str) -> CompilationResult:
    t0 = time.perf_counter()
    errors = _ffi.check(source)
    ms = (time.perf_counter() - t0) * 1000
    return CompilationResult(
        success=not errors, gate_reached=CompilationGate.TYPE_CHECK,
        output="OK" if not errors else "",
        error="; ".join(errors), compile_time_ms=ms,
    )


def _ffi_lex(source: str) -> CompilationResult:
    t0 = time.perf_counter()
    tokens = _ffi.lex(source)
    ms = (time.perf_counter() - t0) * 1000
    return CompilationResult(
        success=True, gate_reached=CompilationGate.LEX,
        output=str(tokens), compile_time_ms=ms, token_count=len(tokens),
    )


def _ffi_bench(source: str, runs: int, timeout_per_run: float) -> list[float]:
    """Sub-millisecond per call — no subprocess, no cargo."""
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        try:
            _ffi.compile_and_run(source)
            times.append((time.perf_counter() - t0) * 1000)
        except Exception:
            times.append(float("inf"))
    return times


# ── CLI Fallback (~400ms) ─────────────────────────────────────────────────────

def _vtc(cmd: str, sl_path: Path, timeout: float) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["cargo", "run", "--manifest-path", str(VITALIS_CARGO), "--", cmd, str(sl_path)],
            capture_output=True, text=True, timeout=timeout, cwd=str(VITALIS_ROOT),
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Timeout after {timeout}s"
    except FileNotFoundError:
        return -1, "", "cargo not found"


def _tmp_sl(source: str) -> Path:
    t = tempfile.NamedTemporaryFile(mode="w", suffix=".sl", prefix="forge_", delete=False)
    t.write(source)
    t.close()
    return Path(t.name)


def _cli_run(source: str, timeout: float) -> CompilationResult:
    f = _tmp_sl(source)
    try:
        t0 = time.perf_counter()
        rc, out, err = _vtc("run", f, timeout)
        ms = (time.perf_counter() - t0) * 1000
        if rc != 0 and not out:
            return CompilationResult(success=False, gate_reached=_gate_from_error(err), error=err, compile_time_ms=ms)
        return CompilationResult(success=True, gate_reached=CompilationGate.EXECUTE, output=out,
                                  compile_time_ms=ms, warnings=_extract_warnings(err))
    finally:
        try: os.unlink(f)
        except OSError: pass


def _cli_bench(source: str, runs: int, tpr: float) -> list[float]:
    f = _tmp_sl(source)
    times = []
    try:
        for _ in range(runs):
            t0 = time.perf_counter()
            rc, out, _ = _vtc("run", f, tpr)
            times.append((time.perf_counter() - t0) * 1000 if (rc == 0 or out) else float("inf"))
    finally:
        try: os.unlink(f)
        except OSError: pass
    return times


# ── Public Compiler API ───────────────────────────────────────────────────────

def compile_and_run(source_code: str, timeout_seconds: float = 30.0) -> CompilationResult:
    """Compile + JIT-execute .sl source. FFI (<5ms) or CLI fallback (~400ms).
    Safety: Force CLI isolation by default to prevent Rust DLL panics from crashing the daemon.
    """
    if os.getenv("VITALIS_ISOLATED", "1") == "1":
        return _cli_run(source_code, timeout_seconds)
    return _ffi_run(source_code, timeout_seconds) if _ensure_ffi() else _cli_run(source_code, timeout_seconds)


def type_check(source_code: str) -> CompilationResult:
    """Run the Vitalis type-checker only (no JIT, no execution)."""
    if _ensure_ffi():
        return _ffi_check(source_code)
    f = _tmp_sl(source_code)
    try:
        t0 = time.perf_counter()
        rc, out, err = _vtc("check", f, 15.0)
        ms = (time.perf_counter() - t0) * 1000
        return CompilationResult(success=(rc == 0),
                                  gate_reached=CompilationGate.TYPE_CHECK if rc == 0 else _gate_from_error(err),
                                  output=out, error=err if rc != 0 else "", compile_time_ms=ms)
    finally:
        try: os.unlink(f)
        except OSError: pass


def lex(source_code: str) -> CompilationResult:
    """Lex source code — returns token stream."""
    if _ensure_ffi():
        return _ffi_lex(source_code)
    f = _tmp_sl(source_code)
    try:
        t0 = time.perf_counter()
        rc, out, err = _vtc("lex", f, 10.0)
        ms = (time.perf_counter() - t0) * 1000
        return CompilationResult(success=(rc == 0), gate_reached=CompilationGate.LEX,
                                  output=out, error=err if rc != 0 else "",
                                  compile_time_ms=ms, token_count=out.count("\n") + 1 if out else 0)
    finally:
        try: os.unlink(f)
        except OSError: pass


def dump_ir(source_code: str) -> str:
    """Dump the SSA IR for source code (FFI only)."""
    return _ffi.dump_ir(source_code) if _ensure_ffi() else "(IR dump requires FFI)"


def parse_ast(source_code: str) -> str:
    """Parse source and return AST debug string (FFI only)."""
    return _ffi.parse_ast(source_code) if _ensure_ffi() else "(AST requires FFI)"


def benchmark_execution(source_code: str, runs: int = 30, timeout_per_run: float = 10.0) -> list[float]:
    """
    Execute source N times, return wall-clock times (ms).
    FFI: <5ms per call. CLI: ~400ms per call.
    """
    return _ffi_bench(source_code, runs, timeout_per_run) if _ensure_ffi() else _cli_bench(source_code, runs, timeout_per_run)


def validate_safety(source_code: str) -> tuple[bool, list[str]]:
    """Capability gate — reject unsafe FFI patterns before compilation."""
    denied = {
        "file_write": "Unauthorized file write",
        "file_delete": "Unauthorized file delete",
        "file_append": "Unauthorized file append",
        "http_get":    "Unauthorized HTTP GET",
        "http_post":   "Unauthorized HTTP POST",
        "tcp_connect": "Unauthorized TCP connect",
        "env_get":     "Unauthorized env var access",
        "process_exec":"Unauthorized process execution",
        "dlopen":      "Unauthorized dynamic library load",
    }
    violations = [msg for pat, msg in denied.items() if pat in source_code]
    code_flat = source_code.replace(" ", "").lower()
    if "whiletrue" in code_flat and "break" not in source_code:
        violations.append("Unbounded loop (no break)")
    return (not violations, violations)


def vitalis_version() -> str:
    """Return the loaded Vitalis version string."""
    return _ffi.version() if _ensure_ffi() else "unknown (CLI mode)"


# ── Native Vitalis Evolution Engine ──────────────────────────────────────────
# Wraps slang_evo_* — Vitalis's own compile-time evolution tracker.
# Use alongside (not instead of) The Forge evolution engine for dual-track evolution.

def evo_register(name: str, source: str) -> None:
    """Register a .sl function with Vitalis's native evolution engine."""
    if _ensure_ffi(): _ffi.evo_register(name, source)


def evo_submit_variant(name: str, new_source: str) -> int:
    """Submit a new variant. Returns generation number (-1 on error)."""
    if _ensure_ffi(): return _ffi.evo_evolve(name, new_source)
    return -1


def evo_set_fitness(name: str, score: float) -> None:
    """Feed a fitness score back to Vitalis's evolution tracker."""
    if _ensure_ffi(): _ffi.evo_set_fitness(name, score)


def evo_get_generation(name: str) -> int:
    """Get current generation number from Vitalis's tracker."""
    return _ffi.evo_get_generation(name) if _ensure_ffi() else 0


def evo_rollback(name: str, generation: int) -> bool:
    """Rollback Vitalis's native tracker to a previous generation."""
    return _ffi.evo_rollback(name, generation) if _ensure_ffi() else False


def evo_get_source(name: str) -> str:
    """Get current source from Vitalis's native tracker."""
    return _ffi.evo_get_source(name) if _ensure_ffi() else ""


# ── Hotpath Native Rust API ───────────────────────────────────────────────────
# Re-export the most useful hotpath functions for use by benchmark/evolution layers.
# These run in native Rust — no Python overhead for inner loops.

def hotpath_p95(values: list[float]) -> float:
    return _ffi.hotpath_p95(values) if _ensure_ffi() else -1.0

def hotpath_percentile(values: list[float], pct: float) -> float:
    return _ffi.hotpath_percentile(values, pct) if _ensure_ffi() else -1.0

def hotpath_mean(values: list[float]) -> float:
    return _ffi.hotpath_mean(values) if _ensure_ffi() else 0.0

def hotpath_stddev(values: list[float]) -> float:
    return _ffi.hotpath_stddev(values) if _ensure_ffi() else 0.0

def hotpath_median(values: list[float]) -> float:
    return _ffi.hotpath_median(values) if _ensure_ffi() else 0.0

def hotpath_weighted_score(metrics: list[float], weights: list[float]) -> float:
    return _ffi.hotpath_weighted_score(metrics, weights) if _ensure_ffi() else 0.0

def hotpath_code_quality_score(cyclomatic: float, cognitive: float, loc: float,
                                num_fns: float, security_issues: float, has_tests: bool) -> float:
    return _ffi.hotpath_code_quality_score(cyclomatic, cognitive, loc, num_fns, security_issues, has_tests) if _ensure_ffi() else 0.5

def hotpath_adaptive_fitness(speed: float, correctness: float, complexity: float,
                              security: float, generation: int = 0) -> float:
    return _ffi.hotpath_adaptive_fitness(speed, correctness, complexity, security, generation) if _ensure_ffi() else 0.5

def hotpath_boltzmann_select(fitnesses: list[float], temperature: float = 1.0) -> list[float]:
    return _ffi.hotpath_boltzmann_select(fitnesses, temperature) if _ensure_ffi() else ([1.0 / len(fitnesses)] * len(fitnesses) if fitnesses else [])

def hotpath_quantum_anneal_accept(old_fitness: float, new_fitness: float,
                                   temperature: float, tunnel: float = 0.1) -> bool:
    return _ffi.hotpath_quantum_anneal_accept(old_fitness, new_fitness, temperature, tunnel) if _ensure_ffi() else (new_fitness >= old_fitness)

def hotpath_bayesian_ucb(mean_fitness: float, num_trials: int,
                          total_trials: int, kappa: float = 1.414) -> float:
    return _ffi.hotpath_bayesian_ucb(mean_fitness, num_trials, total_trials, kappa) if _ensure_ffi() else mean_fitness

def hotpath_levy_step(u: float, v: float, beta: float = 1.5, scale: float = 1.0) -> float:
    return _ffi.hotpath_levy_step(u, v, beta, scale) if _ensure_ffi() else abs(u)

def hotpath_shannon_diversity(probs: list[float]) -> float:
    return _ffi.hotpath_shannon_diversity(probs) if _ensure_ffi() else 0.5

def hotpath_pareto_front(solutions: list[list[float]]) -> list[int]:
    return _ffi.hotpath_pareto_front(solutions) if _ensure_ffi() else list(range(len(solutions)))

def hotpath_pareto_dominates(a: list[float], b: list[float]) -> bool:
    return _ffi.hotpath_pareto_dominates(a, b) if _ensure_ffi() else False

def hotpath_cosine_similarity(a: list[float], b: list[float]) -> float:
    return _ffi.hotpath_cosine_similarity(a, b) if _ensure_ffi() else 0.0

def hotpath_cma_es_mean_update(solutions: list[list[float]], fitnesses: list[float], mu: int | None = None) -> list[float]:
    return _ffi.hotpath_cma_es_mean_update(solutions, fitnesses, mu) if _ensure_ffi() else []

def hotpath_ema_update(ema_old: float, new_value: float, alpha: float = 0.3) -> float:
    return _ffi.hotpath_ema_update(ema_old, new_value, alpha) if _ensure_ffi() else new_value

def hotpath_softmax(values: list[float]) -> list[float]:
    return _ffi.hotpath_softmax(values) if _ensure_ffi() else values

def hotpath_entropy(probs: list[float]) -> float:
    return _ffi.hotpath_entropy(probs) if _ensure_ffi() else 0.0


# ── Internal Helpers ─────────────────────────────────────────────────────────

def _gate_from_error(err: str) -> CompilationGate:
    s = err.lower()
    if any(k in s for k in ("unexpected token", "lexer")): return CompilationGate.LEX
    if any(k in s for k in ("parse", "expected", "syntax")): return CompilationGate.PARSE
    if any(k in s for k in ("type", "mismatch", "undefined", "unresolved")): return CompilationGate.TYPE_CHECK
    if "lint" in s: return CompilationGate.LINT
    if any(k in s for k in ("codegen", "cranelift", "jit")): return CompilationGate.COMPILE
    return CompilationGate.COMPILE


def _extract_warnings(stderr: str) -> list[str]:
    return [l.strip() for l in stderr.split("\n") if "warning" in l.lower()] if stderr else []
