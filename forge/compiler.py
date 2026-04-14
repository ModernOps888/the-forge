"""
The Forge — Vitalis Compiler Integration

Wraps the Vitalis CLI (vtc) to compile, type-check, lint, and execute
.sl programs. This is the impartial judge in the arena.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

from .models import CompilationGate, CompilationResult

# Path to the Vitalis project root
VITALIS_ROOT = Path(os.getenv("VITALIS_ROOT", r"C:\Vitalis-V60"))
VITALIS_CARGO_TOML = VITALIS_ROOT / "Cargo.toml"


def _run_vtc(subcommand: str, source_file: Path, timeout_seconds: float = 30.0) -> tuple[int, str, str]:
    """Run a vtc subcommand on a .sl file. Returns (exit_code, stdout, stderr)."""
    cmd = [
        "cargo", "run", "--manifest-path", str(VITALIS_CARGO_TOML),
        "--", subcommand, str(source_file)
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(VITALIS_ROOT),
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Timeout after {timeout_seconds}s"
    except FileNotFoundError:
        return -1, "", "cargo not found — is Rust installed?"


def _write_temp_sl(source_code: str) -> Path:
    """Write source code to a temporary .sl file."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".sl", prefix="forge_", delete=False, dir=tempfile.gettempdir()
    )
    tmp.write(source_code)
    tmp.close()
    return Path(tmp.name)


def compile_and_run(source_code: str, timeout_seconds: float = 30.0) -> CompilationResult:
    """
    Run source code through the full Vitalis pipeline:
    lex → parse → type-check → JIT compile → execute.
    
    Returns a CompilationResult with the gate reached and output.
    """
    sl_file = _write_temp_sl(source_code)
    
    try:
        start = time.perf_counter()
        exit_code, stdout, stderr = _run_vtc("run", sl_file, timeout_seconds)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Parse the output to determine which gate was reached
        if "Timeout" in stderr:
            return CompilationResult(
                success=False,
                gate_reached=CompilationGate.EXECUTE,
                error="Execution timed out",
                compile_time_ms=elapsed_ms,
            )

        # Check for compilation errors in stderr
        if exit_code != 0 and not stdout:
            gate = _detect_failure_gate(stderr)
            return CompilationResult(
                success=False,
                gate_reached=gate,
                error=stderr,
                compile_time_ms=elapsed_ms,
            )

        # Success — extract output
        return CompilationResult(
            success=True,
            gate_reached=CompilationGate.EXECUTE,
            output=stdout,
            compile_time_ms=elapsed_ms,
            warnings=_extract_warnings(stderr),
        )
    finally:
        try:
            os.unlink(sl_file)
        except OSError:
            pass


def type_check(source_code: str) -> CompilationResult:
    """Run only the type-checker (no execution)."""
    sl_file = _write_temp_sl(source_code)
    try:
        start = time.perf_counter()
        exit_code, stdout, stderr = _run_vtc("check", sl_file, timeout_seconds=15.0)
        elapsed_ms = (time.perf_counter() - start) * 1000

        return CompilationResult(
            success=(exit_code == 0),
            gate_reached=CompilationGate.TYPE_CHECK if exit_code == 0 else _detect_failure_gate(stderr),
            output=stdout,
            error=stderr if exit_code != 0 else "",
            compile_time_ms=elapsed_ms,
        )
    finally:
        try:
            os.unlink(sl_file)
        except OSError:
            pass


def lex(source_code: str) -> CompilationResult:
    """Run only the lexer — returns token stream."""
    sl_file = _write_temp_sl(source_code)
    try:
        start = time.perf_counter()
        exit_code, stdout, stderr = _run_vtc("lex", sl_file, timeout_seconds=10.0)
        elapsed_ms = (time.perf_counter() - start) * 1000

        token_count = stdout.count("\n") + 1 if stdout else 0

        return CompilationResult(
            success=(exit_code == 0),
            gate_reached=CompilationGate.LEX,
            output=stdout,
            error=stderr if exit_code != 0 else "",
            compile_time_ms=elapsed_ms,
            token_count=token_count,
        )
    finally:
        try:
            os.unlink(sl_file)
        except OSError:
            pass


def benchmark_execution(source_code: str, runs: int = 30, timeout_per_run: float = 10.0) -> list[float]:
    """
    Execute source code N times and return wall-clock times in milliseconds.
    Used by the benchmark layer for statistical analysis.
    """
    sl_file = _write_temp_sl(source_code)
    timings = []

    try:
        for _ in range(runs):
            start = time.perf_counter()
            exit_code, stdout, stderr = _run_vtc("run", sl_file, timeout_per_run)
            elapsed_ms = (time.perf_counter() - start) * 1000

            if exit_code == 0 or stdout:  # successful execution
                timings.append(elapsed_ms)
            else:
                timings.append(float("inf"))  # failed run
    finally:
        try:
            os.unlink(sl_file)
        except OSError:
            pass

    return timings


def validate_safety(source_code: str) -> tuple[bool, list[str]]:
    """
    Check source code for unsafe operations.
    Returns (is_safe, list_of_violations).
    """
    violations = []
    
    # Check for dangerous stdlib calls
    dangerous_patterns = {
        "file_write": "Unauthorized file write access",
        "file_delete": "Unauthorized file deletion",
        "file_append": "Unauthorized file append",
        "http_get": "Unauthorized network access (HTTP GET)",
        "http_post": "Unauthorized network access (HTTP POST)",
        "tcp_connect": "Unauthorized network access (TCP)",
        "env_get": "Unauthorized environment variable access",
    }

    for pattern, message in dangerous_patterns.items():
        if pattern in source_code:
            violations.append(message)

    # Check for potential infinite loops (heuristic)
    if "while true" in source_code.replace(" ", "").lower():
        if "break" not in source_code:
            violations.append("Potentially unbounded loop without break")

    return (len(violations) == 0, violations)


def _detect_failure_gate(stderr: str) -> CompilationGate:
    """Heuristically detect which compilation gate failed from error output."""
    stderr_lower = stderr.lower()
    if "unexpected token" in stderr_lower or "lexer" in stderr_lower:
        return CompilationGate.LEX
    if "parse" in stderr_lower or "expected" in stderr_lower:
        return CompilationGate.PARSE
    if "type" in stderr_lower or "mismatch" in stderr_lower or "undefined" in stderr_lower:
        return CompilationGate.TYPE_CHECK
    if "lint" in stderr_lower:
        return CompilationGate.LINT
    if "codegen" in stderr_lower or "cranelift" in stderr_lower:
        return CompilationGate.COMPILE
    return CompilationGate.COMPILE  # default assumption


def _extract_warnings(stderr: str) -> list[str]:
    """Extract warning messages from compiler output."""
    if not stderr:
        return []
    return [line.strip() for line in stderr.split("\n") if "warning" in line.lower()]
