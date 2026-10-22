"""
The Forge — Vitalis Compiler Integration (Elite FFI Edition)

Primary path: ctypes DLL — zero subprocess overhead, <5ms compile+JIT.
Secondary path: cargo run CLI — ~400ms, used if DLL unavailable but Vitalis repo present.
Fallback path: built-in sandboxed Python .sl compiler & evaluator (zero external deps).

Exposes the full Vitalis compiler pipeline:
  - lex / parse_ast / dump_ir / type_check / compile_and_run
  - validate_safety (capability gate)
  - Native evolution engine (slang_evo_*)
  - Hotpath native Rust ops (hotpath_*) with exact pure-Python math fallbacks
"""

from __future__ import annotations

import ast
import math
import os
import random
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .models import CompilationGate, CompilationResult

# ── DLL State ────────────────────────────────────────────────────────────────

_ffi: Optional[object] = None          # vitalis_ffi module
_ffi_ready: Optional[bool] = None      # None = untested, True/False = result
_evo_store: dict[str, list[tuple[str, float]]] = {}  # pure-Python evo fallback store

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
    except Exception:
        _ffi_ready = False
    return _ffi_ready


def _dll_size_mb() -> int:
    if _ffi is not None and hasattr(_ffi, "_dll_path"):
        try:
            p = Path(getattr(_ffi, "_dll_path"))
            if p.exists():
                return p.stat().st_size // 1024 // 1024
        except OSError:
            pass
    for suffix in ("release", "debug"):
        p = VITALIS_ROOT / "target" / suffix / "vitalis.dll"
        if p.exists():
            return p.stat().st_size // 1024 // 1024
    return 0


# ── Built-in Pure-Python .sl Compiler & Sandboxed Evaluator Fallback ─────────

class _SlList(list):
    """Vitalis .sl array runtime wrapper supporting .len(), .get(), .set(), .push(), .slice()."""

    def len(self) -> int:
        return len(self)

    def get(self, idx: int) -> Any:
        i = int(idx)
        if 0 <= i < len(self):
            return self[i]
        raise IndexError(f"Array index {i} out of bounds (len={len(self)})")

    def set(self, idx: int, val: Any) -> "_SlList":
        i = int(idx)
        if 0 <= i < len(self):
            self[i] = val
        return self

    def push(self, val: Any) -> "_SlList":
        new_list = _SlList(self)
        new_list.append(val)
        return new_list

    def slice(self, start: int, end: int) -> "_SlList":
        return _SlList(self[max(0, int(start)):max(0, int(end))])

    def __repr__(self) -> str:
        return "[" + ", ".join(repr(x) for x in self) + "]"


def _sl_merge_sorted(left: list, right: list) -> _SlList:
    res = _SlList()
    i, j = 0, 0
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            res.append(left[i])
            i += 1
        else:
            res.append(right[j])
            j += 1
    res.extend(left[i:])
    res.extend(right[j:])
    return res


def _sl_insertion_sort(arr: list) -> _SlList:
    res = _SlList(arr)
    for i in range(1, len(res)):
        key = res[i]
        j = i - 1
        while j >= 0 and res[j] > key:
            res[j + 1] = res[j]
            j -= 1
        res[j + 1] = key
    return res


def _sl_array_concat(a: list, b: list) -> _SlList:
    res = _SlList(a)
    res.extend(b)
    return res


_SL_TOKEN_RE = re.compile(
    r"""
    (?P<COMMENT>//[^\n]*)
  | (?P<STRING>"(?:\\.|[^"\\])*")
  | (?P<ARROW>->)
  | (?P<OP>==|!=|<=|>=|&&|\|\||[+\-*/%<>=!])
  | (?P<PUNCT>[{}()\[\]:,;.\#])
  | (?P<NUMBER>\b\d+(?:\.\d+)?\b)
  | (?P<IDENT>\b[a-zA-Z_][a-zA-Z0-9_]*\b)
  | (?P<WS>\s+)
  | (?P<INVALID>.)
    """,
    re.VERBOSE,
)


def _py_lex(source: str) -> CompilationResult:
    """Tokenize .sl source code in pure Python."""
    t0 = time.perf_counter()
    if not source or not source.strip():
        return CompilationResult(
            success=False,
            gate_reached=CompilationGate.LEX,
            error="Lexer error: empty source input",
            compile_time_ms=(time.perf_counter() - t0) * 1000,
        )
    # Check for unbalanced quotes outside comments
    clean_lines = [ re.sub(r"//.*$", "", ln) for ln in source.splitlines() ]
    for ln_no, ln in enumerate(clean_lines, 1):
        # Count unescaped double quotes
        unescaped_quotes = len(re.findall(r'(?<!\\)"', ln))
        if unescaped_quotes % 2 != 0:
            return CompilationResult(
                success=False,
                gate_reached=CompilationGate.LEX,
                error=f"Lexer error at line {ln_no}: unterminated string literal",
                compile_time_ms=(time.perf_counter() - t0) * 1000,
            )

    tokens: list[str] = []
    for m in _SL_TOKEN_RE.finditer(source):
        kind = m.lastgroup
        val = m.group()
        if kind in ("WS", "COMMENT"):
            continue
        if kind == "INVALID" and val not in ("@", "$"):
            return CompilationResult(
                success=False,
                gate_reached=CompilationGate.LEX,
                error=f"Lexer error: unexpected token {val!r}",
                compile_time_ms=(time.perf_counter() - t0) * 1000,
            )
        tokens.append(f"{kind}:{val}")

    ms = (time.perf_counter() - t0) * 1000
    return CompilationResult(
        success=True,
        gate_reached=CompilationGate.LEX,
        output="\n".join(tokens),
        compile_time_ms=ms,
        token_count=len(tokens),
    )


def _strip_comments_and_strings(line: str) -> str:
    """Remove // comments for structural brace analysis."""
    out = []
    in_str = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"' and (i == 0 or line[i - 1] != "\\"):
            in_str = not in_str
            out.append(ch)
        elif not in_str and line[i:i + 2] == "//":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _transpile_sl_to_python(source: str) -> tuple[bool, CompilationGate, str, list[str]]:
    """
    Validate Lex -> Parse -> Type-Check gates on .sl code and transpile to Python AST.
    Returns (ok, gate_reached, py_code_or_error, warnings).
    """
    lex_res = _py_lex(source)
    if not lex_res.success:
        return False, CompilationGate.LEX, lex_res.error, []

    warnings: list[str] = []
    clean_lines = [_strip_comments_and_strings(ln) for ln in source.splitlines()]
    joined = "\n".join(clean_lines)

    # Check balanced delimiters (Parse gate)
    pairs = {"{": "}", "(": ")", "[": "]"}
    closing = {v: k for k, v in pairs.items()}
    stack: list[str] = []
    for ch in joined:
        if ch in pairs:
            stack.append(ch)
        elif ch in closing:
            if not stack or stack[-1] != closing[ch]:
                return False, CompilationGate.PARSE, f"Parse error: unexpected closing '{ch}'", warnings
            stack.pop()
    if stack:
        return False, CompilationGate.PARSE, f"Parse error: unclosed '{stack[-1]}'", warnings

    # Must contain at least one fn declaration
    fn_matches = list(re.finditer(
        r"\bfn\s+([a-zA-Z_]\w*)\s*\(([^)]*)\)\s*(?:->\s*([^{]+?))?\s*\{",
        joined,
    ))
    if not fn_matches:
        return False, CompilationGate.PARSE, "Parse error: expected function definition 'fn <name>(...)'", warnings

    # Validate parameter and return type annotations (Type-Check gate)
    valid_base_types = {"i64", "i32", "u64", "u32", "f64", "f32", "bool", "str", "string", "void", "()"}
    defined_fns: dict[str, tuple[list[tuple[str, str]], str]] = {}

    for fm in fn_matches:
        fn_name = fm.group(1)
        raw_params = fm.group(2).strip()
        ret_type = (fm.group(3) or "void").strip()

        ret_clean = ret_type.replace("[", "").replace("]", "").strip()
        if ret_clean and ret_clean not in valid_base_types:
            return False, CompilationGate.TYPE_CHECK, f"Type error: unknown return type '{ret_type}' in fn {fn_name}", warnings

        parsed_params: list[tuple[str, str]] = []
        if raw_params:
            for p in raw_params.split(","):
                p = p.strip()
                if not p:
                    continue
                if ":" in p:
                    pname, ptype = p.split(":", 1)
                    pname = pname.replace("mut ", "").strip()
                    ptype = ptype.strip()
                    p_clean = ptype.replace("[", "").replace("]", "").strip()
                    if p_clean not in valid_base_types:
                        return False, CompilationGate.TYPE_CHECK, f"Type error: unknown parameter type '{ptype}' for '{pname}'", warnings
                    parsed_params.append((pname, ptype))
                else:
                    parsed_params.append((p.replace("mut ", "").strip(), "i64"))
        defined_fns[fn_name] = (parsed_params, ret_type)

    # Extract top-level functions with brace matching and convert bodies to Python
    py_lines: list[str] = []
    pos = 0
    src_len = len(joined)

    def _convert_expr(expr: str) -> str:
        e = expr.strip().rstrip(";").strip()
        # Logical operators
        e = re.sub(r"&&", " and ", e)
        e = re.sub(r"\|\|", " or ", e)
        e = re.sub(r"\btrue\b", "True", e)
        e = re.sub(r"\bfalse\b", "False", e)
        # Integer division for i64 arithmetic when `/` is used between expressions
        e = re.sub(r"(?<![/])/(?![/])", "//", e)
        # Convert array literals `[...]` to `_SlList([...])`
        e = re.sub(r"\[([^\[\]]*)\]", r"_SlList([\1])", e)
        return e

    def _convert_block(block_src: str, indent: int, ret_type: str) -> tuple[bool, CompilationGate, list[str]]:
        # Expand inline `{` and `}` onto separate lines so brace-depth conversion is exact
        normalized = re.sub(r"\{", "{\n", block_src)
        normalized = re.sub(r"\}", "\n}\n", normalized)
        normalized = re.sub(r";", ";\n", normalized)
        raw_stmts = [s.strip() for s in normalized.splitlines() if s.strip()]

        out_stmts: list[str] = []
        cur_indent = indent
        idx = 0

        while idx < len(raw_stmts):
            st = raw_stmts[idx]
            pad = "    " * cur_indent

            if st == "}":
                cur_indent = max(indent, cur_indent - 1)
                idx += 1
                continue

            # Merge `else` + `if ... {`
            if st == "else" and idx + 1 < len(raw_stmts) and raw_stmts[idx + 1].startswith("if "):
                raw_stmts[idx + 1] = "elif " + raw_stmts[idx + 1][3:]
                idx += 1
                continue

            if st.startswith("else") and st.endswith("{"):
                inner_after_else = st[4:-1].strip()
                if inner_after_else.startswith("if "):
                    cond = _convert_expr(inner_after_else[3:])
                    out_stmts.append(f"{pad}elif {cond}:")
                else:
                    out_stmts.append(f"{pad}else:")
                cur_indent += 1
                out_stmts.append(f"{'    ' * cur_indent}pass")
                idx += 1
                continue

            if st.startswith("if ") and st.endswith("{"):
                cond = _convert_expr(st[3:-1])
                out_stmts.append(f"{pad}if {cond}:")
                cur_indent += 1
                out_stmts.append(f"{'    ' * cur_indent}pass")
                idx += 1
                continue

            if st.startswith("elif ") and st.endswith("{"):
                cond = _convert_expr(st[5:-1])
                out_stmts.append(f"{pad}elif {cond}:")
                cur_indent += 1
                out_stmts.append(f"{'    ' * cur_indent}pass")
                idx += 1
                continue

            if st.startswith("while ") and st.endswith("{"):
                cond = _convert_expr(st[6:-1])
                out_stmts.append(f"{pad}while {cond}:")
                cur_indent += 1
                out_stmts.append(f"{'    ' * cur_indent}_tick()")
                idx += 1
                continue

            if st.startswith("for ") and st.endswith("{"):
                m_for = re.match(r"^for\s+([a-zA-Z_]\w*)\s+in\s+(.+)\{$", st)
                if not m_for:
                    return False, CompilationGate.PARSE, [f"Parse error in for loop: {st}"]
                var_name = m_for.group(1)
                iter_expr = _convert_expr(m_for.group(2))
                if ".." in iter_expr:
                    lo, hi = iter_expr.split("..", 1)
                    iter_expr = f"range({lo.strip()}, {hi.strip()})"
                out_stmts.append(f"{pad}for {var_name} in {iter_expr}:")
                cur_indent += 1
                out_stmts.append(f"{'    ' * cur_indent}_tick()")
                idx += 1
                continue

            # `let [mut] x [: type] = expr;`
            m_let = re.match(r"^let\s+(?:mut\s+)?([a-zA-Z_]\w*)(?:\s*:\s*[^=]+)?\s*=\s*(.+?);?$", st)
            if m_let:
                vname = m_let.group(1)
                vexpr = _convert_expr(m_let.group(2))
                out_stmts.append(f"{pad}{vname} = {vexpr}")
                idx += 1
                continue

            # `return expr;`
            if st.startswith("return ") or st == "return;" or st == "return":
                rexpr = _convert_expr(st[6:]) if len(st) > 6 else "None"
                out_stmts.append(f"{pad}return {rexpr}")
                idx += 1
                continue

            if st in ("break;", "break"):
                out_stmts.append(f"{pad}break")
                idx += 1
                continue

            if st in ("continue;", "continue"):
                out_stmts.append(f"{pad}continue")
                idx += 1
                continue

            # Assignment `x = expr;`
            m_assign = re.match(r"^([a-zA-Z_]\w*(?:\[\d+\])?)\s*=\s*([^=].*?);?$", st)
            if m_assign:
                lhs = m_assign.group(1)
                rhs = _convert_expr(m_assign.group(2))
                out_stmts.append(f"{pad}{lhs} = {rhs}")
                idx += 1
                continue

            # Expression statement; if it's the final top-level statement in the function without `;`, treat as implicit return
            is_last_top = (cur_indent == indent) and all(s == "}" for s in raw_stmts[idx + 1:])
            expr_conv = _convert_expr(st)
            if is_last_top and not st.endswith(";"):
                out_stmts.append(f"{pad}return {expr_conv}")
            else:
                out_stmts.append(f"{pad}{expr_conv}")
            idx += 1

        if not out_stmts:
            out_stmts.append(f"{'    ' * indent}pass")
        return True, CompilationGate.TYPE_CHECK, out_stmts

    for fm in fn_matches:
        fn_name = fm.group(1)
        params, ret_type = defined_fns[fn_name]
        body_start = fm.end()
        depth = 1
        i = body_start
        while i < src_len and depth > 0:
            if joined[i] == "{":
                depth += 1
            elif joined[i] == "}":
                depth -= 1
            i += 1
        body_src = joined[body_start:i - 1]

        param_names = [p[0] for p in params]
        py_lines.append(f"def {fn_name}({', '.join(param_names)}):")
        py_lines.append("    _tick()")
        for pname, ptype in params:
            if "[" in ptype:
                py_lines.append(f"    {pname} = _SlList({pname}) if isinstance({pname}, list) else {pname}")

        ok, gate, body_py = _convert_block(body_src, 1, ret_type)
        if not ok:
            return False, gate, body_py[0], warnings
        py_lines.extend(body_py)
        py_lines.append("")

    py_source = "\n".join(py_lines)
    try:
        tree = ast.parse(py_source)
    except SyntaxError as e:
        return False, CompilationGate.PARSE, f"Parse error: {e.msg} (line {e.lineno})", warnings

    # Type-check undefined function calls and argument counts
    builtin_allowed = {
        "_tick", "_SlList", "range", "len", "min", "max", "abs", "int", "float", "str", "bool",
        "isinstance", "list", "merge_sorted", "insertion_sort", "array_concat", "print",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            callee = node.func.id
            if callee not in defined_fns and callee not in builtin_allowed:
                return (
                    False,
                    CompilationGate.TYPE_CHECK,
                    f"Type error: undefined function '{callee}'",
                    warnings,
                )
            if callee in defined_fns:
                expected_arity = len(defined_fns[callee][0])
                if len(node.args) != expected_arity:
                    return (
                        False,
                        CompilationGate.TYPE_CHECK,
                        f"Type error: function '{callee}' expected {expected_arity} argument(s), got {len(node.args)}",
                        warnings,
                    )

    return True, CompilationGate.TYPE_CHECK, py_source, warnings


def _py_check(source: str) -> CompilationResult:
    """Run Lex -> Parse -> Type-Check in pure Python."""
    t0 = time.perf_counter()
    safe, violations = validate_safety(source)
    if not safe:
        return CompilationResult(
            success=False,
            gate_reached=CompilationGate.CAPABILITY,
            error="; ".join(violations),
            compile_time_ms=(time.perf_counter() - t0) * 1000,
        )
    ok, gate, msg_or_py, warnings = _transpile_sl_to_python(source)
    ms = (time.perf_counter() - t0) * 1000
    if not ok:
        return CompilationResult(
            success=False,
            gate_reached=gate,
            error=msg_or_py,
            compile_time_ms=ms,
            warnings=warnings,
        )
    return CompilationResult(
        success=True,
        gate_reached=CompilationGate.TYPE_CHECK,
        output="OK",
        compile_time_ms=ms,
        warnings=warnings,
    )


def _py_run(source: str, timeout: float = 5.0) -> CompilationResult:
    """Compile and execute .sl source code in a restricted pure-Python sandbox."""
    t0 = time.perf_counter()
    safe, violations = validate_safety(source)
    if not safe:
        return CompilationResult(
            success=False,
            gate_reached=CompilationGate.CAPABILITY,
            error="; ".join(violations),
            compile_time_ms=(time.perf_counter() - t0) * 1000,
        )

    ok, gate, py_code_or_err, warnings = _transpile_sl_to_python(source)
    if not ok:
        return CompilationResult(
            success=False,
            gate_reached=gate,
            error=py_code_or_err,
            compile_time_ms=(time.perf_counter() - t0) * 1000,
            warnings=warnings,
        )

    try:
        code_obj = compile(py_code_or_err, "<vitalis_sl>", "exec")
    except Exception as e:
        return CompilationResult(
            success=False,
            gate_reached=CompilationGate.COMPILE,
            error=f"Codegen error: {e}",
            compile_time_ms=(time.perf_counter() - t0) * 1000,
            warnings=warnings,
        )

    steps = 0
    max_steps = 250_000
    deadline = t0 + max(timeout, 0.5)

    def _tick() -> None:
        nonlocal steps
        steps += 1
        if steps > max_steps or (steps & 1023 == 0 and time.perf_counter() > deadline):
            raise TimeoutError(f"Execution exceeded time/step limit ({timeout}s)")

    printed: list[str] = []
    safe_builtins = {
        "range": range,
        "len": len,
        "min": min,
        "max": max,
        "abs": abs,
        "int": int,
        "float": float,
        "str": str,
        "bool": bool,
        "list": list,
        "isinstance": isinstance,
        "True": True,
        "False": False,
        "None": None,
        "print": lambda *args: printed.append(" ".join(str(a) for a in args)),
    }
    env: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "_tick": _tick,
        "_SlList": _SlList,
        "merge_sorted": _sl_merge_sorted,
        "insertion_sort": _sl_insertion_sort,
        "array_concat": _sl_array_concat,
    }

    try:
        exec(code_obj, env, env)
        if "main" in env and callable(env["main"]):
            res = env["main"]()
            out_str = "\n".join(printed + ([str(res)] if res is not None else []))
        else:
            out_str = "\n".join(printed) if printed else "OK"
        ms = (time.perf_counter() - t0) * 1000
        return CompilationResult(
            success=True,
            gate_reached=CompilationGate.EXECUTE,
            output=out_str.strip(),
            compile_time_ms=ms,
            warnings=warnings,
        )
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        err_msg = str(e)
        gate_err = CompilationGate.TYPE_CHECK if isinstance(e, (NameError, TypeError)) else CompilationGate.EXECUTE
        return CompilationResult(
            success=False,
            gate_reached=gate_err,
            error=err_msg,
            compile_time_ms=ms,
            warnings=warnings,
        )


def _py_bench(source: str, runs: int, tpr: float) -> list[float]:
    times: list[float] = []
    for _ in range(runs):
        res = _py_run(source, tpr)
        times.append(res.compile_time_ms if res.success else float("inf"))
    return times


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
            err_joined = "; ".join(errors)
            return CompilationResult(
                success=False,
                gate_reached=_gate_from_error(err_joined),
                error=err_joined,
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
    except Exception:
        # FFI crash fallback — switch to CLI/Python fallback for this call
        ms = (time.perf_counter() - t0) * 1000
        cli_result = _cli_run(source, timeout)
        return CompilationResult(
            success=cli_result.success,
            gate_reached=cli_result.gate_reached,
            output=cli_result.output,
            error=cli_result.error,
            compile_time_ms=cli_result.compile_time_ms + ms,
            warnings=cli_result.warnings,
        )


def _ffi_check(source: str) -> CompilationResult:
    t0 = time.perf_counter()
    errors = _ffi.check(source)
    ms = (time.perf_counter() - t0) * 1000
    err_str = "; ".join(errors)
    return CompilationResult(
        success=not errors,
        gate_reached=CompilationGate.TYPE_CHECK if not errors else _gate_from_error(err_str),
        output="OK" if not errors else "",
        error=err_str,
        compile_time_ms=ms,
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


# ── CLI Fallback (~400ms) & Pure-Python Fallback ──────────────────────────────

def _vtc(cmd: str, sl_path: Path, timeout: float) -> tuple[int, str, str]:
    if not VITALIS_CARGO.exists():
        return -1, "", "vitalis_cargo_missing"
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
    t = tempfile.NamedTemporaryFile(mode="w", suffix=".sl", prefix="forge_", delete=False, encoding="utf-8")
    t.write(source)
    t.close()
    return Path(t.name)


def _cli_run(source: str, timeout: float) -> CompilationResult:
    if not VITALIS_CARGO.exists():
        return _py_run(source, timeout)
    f = _tmp_sl(source)
    try:
        t0 = time.perf_counter()
        rc, out, err = _vtc("run", f, timeout)
        ms = (time.perf_counter() - t0) * 1000
        if rc != 0:
            err_msg = err or out or "Compilation failed"
            return CompilationResult(success=False, gate_reached=_gate_from_error(err_msg), error=err_msg, compile_time_ms=ms)
        return CompilationResult(success=True, gate_reached=CompilationGate.EXECUTE, output=out,
                                  compile_time_ms=ms, warnings=_extract_warnings(err))
    finally:
        try:
            os.unlink(f)
        except OSError:
            pass


def _cli_bench(source: str, runs: int, tpr: float) -> list[float]:
    if not VITALIS_CARGO.exists():
        return _py_bench(source, runs, tpr)
    f = _tmp_sl(source)
    times = []
    try:
        for _ in range(runs):
            t0 = time.perf_counter()
            rc, _, _ = _vtc("run", f, tpr)
            times.append((time.perf_counter() - t0) * 1000 if rc == 0 else float("inf"))
    finally:
        try:
            os.unlink(f)
        except OSError:
            pass
    return times


# ── Public Compiler API ───────────────────────────────────────────────────────

def compile_and_run(source_code: str, timeout_seconds: float = 30.0) -> CompilationResult:
    """Compile + JIT-execute .sl source across the 7-gate pipeline.
    Uses FFI (<5ms), Vitalis CLI (~400ms), or built-in Python .sl fallback.
    """
    t0 = time.perf_counter()
    safe, violations = validate_safety(source_code)
    if not safe:
        return CompilationResult(
            success=False,
            gate_reached=CompilationGate.CAPABILITY,
            error="; ".join(violations),
            compile_time_ms=(time.perf_counter() - t0) * 1000,
        )

    if os.getenv("VITALIS_ISOLATED", "0") == "1":
        return _cli_run(source_code, timeout_seconds)
    return _ffi_run(source_code, timeout_seconds) if _ensure_ffi() else _cli_run(source_code, timeout_seconds)


def type_check(source_code: str) -> CompilationResult:
    """Run the Vitalis type-checker only (no JIT, no execution)."""
    if _ensure_ffi():
        return _ffi_check(source_code)
    if not VITALIS_CARGO.exists():
        return _py_check(source_code)
    f = _tmp_sl(source_code)
    try:
        t0 = time.perf_counter()
        rc, out, err = _vtc("check", f, 15.0)
        ms = (time.perf_counter() - t0) * 1000
        return CompilationResult(success=(rc == 0),
                                  gate_reached=CompilationGate.TYPE_CHECK if rc == 0 else _gate_from_error(err),
                                  output=out or ("OK" if rc == 0 else ""), error=err if rc != 0 else "", compile_time_ms=ms)
    finally:
        try:
            os.unlink(f)
        except OSError:
            pass


def lex(source_code: str) -> CompilationResult:
    """Lex source code — returns token stream."""
    if _ensure_ffi():
        return _ffi_lex(source_code)
    if not VITALIS_CARGO.exists():
        return _py_lex(source_code)
    f = _tmp_sl(source_code)
    try:
        t0 = time.perf_counter()
        rc, out, err = _vtc("lex", f, 10.0)
        ms = (time.perf_counter() - t0) * 1000
        return CompilationResult(success=(rc == 0), gate_reached=CompilationGate.LEX,
                                  output=out, error=err if rc != 0 else "",
                                  compile_time_ms=ms, token_count=out.count("\n") + 1 if out else 0)
    finally:
        try:
            os.unlink(f)
        except OSError:
            pass


def dump_ir(source_code: str) -> str:
    """Dump the SSA IR for source code (FFI or Python AST summary)."""
    if _ensure_ffi():
        return _ffi.dump_ir(source_code)
    ok, _, py_or_err, _ = _transpile_sl_to_python(source_code)
    return py_or_err if ok else f"(IR error: {py_or_err})"


def parse_ast(source_code: str) -> str:
    """Parse source and return AST debug string."""
    if _ensure_ffi():
        return _ffi.parse_ast(source_code)
    ok, _, py_or_err, _ = _transpile_sl_to_python(source_code)
    if not ok:
        return f"(AST error: {py_or_err})"
    try:
        return ast.dump(ast.parse(py_or_err))
    except Exception as e:
        return f"(AST error: {e})"


def benchmark_execution(source_code: str, runs: int = 30, timeout_per_run: float = 10.0) -> list[float]:
    """
    Execute source N times, return wall-clock times (ms).
    FFI: <5ms per call. CLI: ~400ms per call. Python fallback: <1ms per call.
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
    if "whiletrue" in code_flat and "break" not in source_code and "return" not in source_code:
        violations.append("Unbounded loop (no break)")
    return (not violations, violations)


def vitalis_version() -> str:
    """Return the loaded Vitalis version string."""
    if _ensure_ffi():
        return _ffi.version()
    if VITALIS_CARGO.exists():
        return "v60 (CLI mode)"
    return "v60-py (Python fallback)"


# ── Native Vitalis Evolution Engine ──────────────────────────────────────────

def evo_register(name: str, source: str) -> None:
    """Register a .sl function with Vitalis's native evolution engine."""
    if _ensure_ffi():
        _ffi.evo_register(name, source)
    else:
        _evo_store[name] = [(source, 0.0)]


def evo_submit_variant(name: str, new_source: str) -> int:
    """Submit a new variant. Returns generation number (-1 on error)."""
    if _ensure_ffi():
        return _ffi.evo_evolve(name, new_source)
    history = _evo_store.setdefault(name, [])
    history.append((new_source, 0.0))
    return len(history) - 1


def evo_set_fitness(name: str, score: float) -> None:
    """Feed a fitness score back to Vitalis's evolution tracker."""
    if _ensure_ffi():
        _ffi.evo_set_fitness(name, score)
    elif name in _evo_store and _evo_store[name]:
        src, _ = _evo_store[name][-1]
        _evo_store[name][-1] = (src, float(score))


def evo_get_generation(name: str) -> int:
    """Get current generation number from Vitalis's tracker."""
    if _ensure_ffi():
        return _ffi.evo_get_generation(name)
    return max(0, len(_evo_store.get(name, [])) - 1)


def evo_rollback(name: str, generation: int) -> bool:
    """Rollback Vitalis's native tracker to a previous generation."""
    if _ensure_ffi():
        return _ffi.evo_rollback(name, generation)
    history = _evo_store.get(name, [])
    if 0 <= generation < len(history):
        _evo_store[name] = history[: generation + 1]
        return True
    return False


def evo_get_source(name: str) -> str:
    """Get current source from Vitalis's native tracker."""
    if _ensure_ffi():
        return _ffi.evo_get_source(name)
    history = _evo_store.get(name, [])
    return history[-1][0] if history else ""


# ── Hotpath Native Rust API (with Exact Pure-Python Math Fallbacks) ───────────

def hotpath_percentile(values: list[float], pct: float) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_percentile(values, pct)
    valid = sorted(v for v in values if math.isfinite(v))
    if not valid:
        return 0.0
    p = pct / 100.0 if pct > 1.0 else max(0.0, min(1.0, pct))
    if len(valid) == 1:
        return float(valid[0])
    idx = p * (len(valid) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    frac = idx - lo
    return float(valid[lo] + frac * (valid[hi] - valid[lo]))


def hotpath_p95(values: list[float]) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_p95(values)
    return hotpath_percentile(values, 0.95)


def hotpath_mean(values: list[float]) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_mean(values)
    valid = [v for v in values if math.isfinite(v)]
    if not valid:
        return 0.0
    # Welford online mean
    mean = 0.0
    for i, x in enumerate(valid, 1):
        mean += (x - mean) / i
    return float(mean)


def hotpath_stddev(values: list[float]) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_stddev(values)
    valid = [v for v in values if math.isfinite(v)]
    if len(valid) < 2:
        return 0.0
    # Welford online variance
    mean = 0.0
    m2 = 0.0
    for i, x in enumerate(valid, 1):
        delta = x - mean
        mean += delta / i
        delta2 = x - mean
        m2 += delta * delta2
    return float(math.sqrt(m2 / (len(valid) - 1)))


def hotpath_median(values: list[float]) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_median(values)
    return hotpath_percentile(values, 0.50)


def hotpath_weighted_score(metrics: list[float], weights: list[float]) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_weighted_score(metrics, weights)
    if not metrics or not weights:
        return 0.0
    total_w = sum(weights[: len(metrics)])
    if total_w <= 0:
        return 0.0
    val = sum(m * w for m, w in zip(metrics, weights)) / total_w
    return float(max(0.0, min(1.0, val)))


def hotpath_code_quality_score(
    cyclomatic: float,
    cognitive: float,
    loc: float,
    num_fns: float,
    security_issues: float,
    has_tests: bool,
) -> float:
    """Returns code quality score on a [0.0, 100.0] scale matching the native Rust FFI."""
    if _ensure_ffi():
        return _ffi.hotpath_code_quality_score(
            cyclomatic, cognitive, loc, num_fns, security_issues, has_tests
        )
    score = 82.0
    # Cyclomatic penalty above 8
    if cyclomatic > 8:
        score -= min(25.0, (cyclomatic - 8) * 2.0)
    # Cognitive indentation penalty above 6
    if cognitive > 6:
        score -= min(20.0, (cognitive - 6) * 1.5)
    # LOC sweet spot
    if loc < 3:
        score -= 15.0
    elif loc > 250:
        score -= min(15.0, (loc - 250) * 0.05)
    # Modularity bonus
    if num_fns >= 1:
        score += min(8.0, num_fns * 2.5)
    # Security penalty
    score -= security_issues * 20.0
    if has_tests:
        score += 10.0
    return float(max(5.0, min(100.0, score)))


def hotpath_adaptive_fitness(
    speed: float,
    correctness: float,
    complexity: float,
    security: float,
    generation: int = 0,
) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_adaptive_fitness(speed, correctness, complexity, security, generation)
    progress = min(max(generation, 0) / 50.0, 1.0)
    w_corr = 0.45 - 0.10 * progress
    w_comp = 0.25 - 0.10 * progress
    w_spd = 0.15 + 0.15 * progress
    w_sec = 0.15 + 0.05 * progress
    return float(
        max(
            0.0,
            min(1.0, correctness * w_corr + speed * w_spd + complexity * w_comp + security * w_sec),
        )
    )


def hotpath_boltzmann_select(fitnesses: list[float], temperature: float = 1.0) -> list[float]:
    if _ensure_ffi():
        return _ffi.hotpath_boltzmann_select(fitnesses, temperature)
    if not fitnesses:
        return []
    t = max(float(temperature), 1e-4)
    mx = max(fitnesses)
    exps = [math.exp(max(-60.0, min(60.0, (f - mx) / t))) for f in fitnesses]
    total = sum(exps) or 1.0
    return [e / total for e in exps]


def hotpath_quantum_anneal_accept(
    old_fitness: float,
    new_fitness: float,
    temperature: float,
    tunnel: float = 0.1,
) -> bool:
    if _ensure_ffi():
        return _ffi.hotpath_quantum_anneal_accept(old_fitness, new_fitness, temperature, tunnel)
    if new_fitness >= old_fitness:
        return True
    t = max(float(temperature), 1e-4)
    delta = new_fitness - old_fitness
    thermal_prob = math.exp(max(-60.0, delta / t))
    tunnel_prob = max(0.0, float(tunnel)) * math.exp(max(-60.0, delta / (t * 2.5 + 1.0)))
    return random.random() < min(1.0, thermal_prob + tunnel_prob)


def hotpath_bayesian_ucb(
    mean_fitness: float,
    num_trials: int,
    total_trials: int,
    kappa: float = 1.414,
) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_bayesian_ucb(mean_fitness, num_trials, total_trials, kappa)
    n = max(int(num_trials), 1)
    total = max(int(total_trials), n)
    return float(mean_fitness + kappa * math.sqrt(math.log(total + 1.0) / n))


def hotpath_levy_step(u: float, v: float, beta: float = 1.5, scale: float = 1.0) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_levy_step(u, v, beta, scale)
    b = max(0.5, min(2.0, float(beta)))
    denom = max(abs(float(v)), 1e-4) ** (1.0 / b)
    step = float(scale) * (float(u) / denom)
    if not math.isfinite(step):
        return float(scale)
    return float(max(-50.0, min(50.0, step)))


def hotpath_shannon_diversity(probs: list[float]) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_shannon_diversity(probs)
    n = len(probs)
    if n <= 1:
        return 0.0
    total = sum(max(0.0, p) for p in probs)
    if total <= 0:
        return 0.0
    norm = [max(0.0, p) / total for p in probs]
    h = -sum(p * math.log(p) for p in norm if p > 1e-12)
    max_h = math.log(n)
    return float(max(0.0, min(1.0, h / max_h))) if max_h > 0 else 0.0


def hotpath_pareto_dominates(a: list[float], b: list[float]) -> bool:
    if _ensure_ffi():
        return _ffi.hotpath_pareto_dominates(a, b)
    if not a or not b or len(a) != len(b):
        return False
    return all(x >= y for x, y in zip(a, b)) and any(x > y for x, y in zip(a, b))


def hotpath_pareto_front(solutions: list[list[float]]) -> list[int]:
    if _ensure_ffi():
        return _ffi.hotpath_pareto_front(solutions)
    n = len(solutions)
    front: list[int] = []
    for i in range(n):
        dominated = False
        for j in range(n):
            if i != j and hotpath_pareto_dominates(solutions[j], solutions[i]):
                dominated = True
                break
        if not dominated:
            front.append(i)
    return front


def hotpath_cosine_similarity(a: list[float], b: list[float]) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_cosine_similarity(a, b)
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return float(max(-1.0, min(1.0, dot / (na * nb))))


def hotpath_cma_es_mean_update(
    solutions: list[list[float]],
    fitnesses: list[float],
    mu: int | None = None,
) -> list[float]:
    if _ensure_ffi():
        return _ffi.hotpath_cma_es_mean_update(solutions, fitnesses, mu)
    if not solutions or not fitnesses or len(solutions) != len(fitnesses):
        return []
    dim = len(solutions[0])
    if any(len(s) != dim for s in solutions):
        return []
    m = mu or max(1, len(solutions) // 2)
    m = min(m, len(solutions))
    ranked = sorted(zip(fitnesses, solutions), key=lambda pair: pair[0], reverse=True)[:m]
    raw_weights = [math.log(m + 0.5) - math.log(i + 1) for i in range(m)]
    w_sum = sum(raw_weights) or 1.0
    weights = [w / w_sum for w in raw_weights]
    mean_vec = [0.0] * dim
    for w, (_, sol) in zip(weights, ranked):
        for d in range(dim):
            mean_vec[d] += w * sol[d]
    return mean_vec


def hotpath_ema_update(ema_old: float, new_value: float, alpha: float = 0.3) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_ema_update(ema_old, new_value, alpha)
    a = max(0.0, min(1.0, float(alpha)))
    if ema_old == 0.0:
        return float(new_value)
    return float(a * new_value + (1.0 - a) * ema_old)


def hotpath_softmax(values: list[float]) -> list[float]:
    if _ensure_ffi():
        return _ffi.hotpath_softmax(values)
    return hotpath_boltzmann_select(values, temperature=1.0)


def hotpath_entropy(probs: list[float]) -> float:
    if _ensure_ffi():
        return _ffi.hotpath_entropy(probs)
    if not probs:
        return 0.0
    total = sum(max(0.0, p) for p in probs)
    if total <= 0:
        return 0.0
    norm = [max(0.0, p) / total for p in probs]
    return float(-sum(p * math.log(p) for p in norm if p > 1e-12))


# ── Internal Helpers ─────────────────────────────────────────────────────────

def _gate_from_error(err: str) -> CompilationGate:
    s = err.lower()
    if any(k in s for k in ("capability", "unauthorized", "unbounded loop")):
        return CompilationGate.CAPABILITY
    if any(k in s for k in ("unexpected token", "lexer", "unterminated string")):
        return CompilationGate.LEX
    if any(k in s for k in ("parse", "expected", "syntax", "unclosed", "unexpected closing")):
        return CompilationGate.PARSE
    if any(k in s for k in ("type", "mismatch", "undefined", "unresolved", "argument")):
        return CompilationGate.TYPE_CHECK
    if "lint" in s:
        return CompilationGate.LINT
    if any(k in s for k in ("codegen", "cranelift", "jit")):
        return CompilationGate.COMPILE
    return CompilationGate.COMPILE


def _extract_warnings(stderr: str) -> list[str]:
    return [l.strip() for l in stderr.split("\n") if "warning" in l.lower()] if stderr else []
