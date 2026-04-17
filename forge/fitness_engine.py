"""
The Forge — Vitalis Polyglot Fitness Engine

Scores code in ANY language across the 6 Vitalis fitness dimensions.
No external linters required — pure AST + heuristic analysis.

Dimensions:
  1. Correctness   (30%) — Parseable/runnable, no obvious errors
  2. Performance   (25%) — Complexity, anti-patterns, algorithmic efficiency
  3. Code Quality  (15%) — Naming, docs, structure, style
  4. Robustness    (15%) — Error handling, input validation, edge cases
  5. Efficiency    (10%) — Memory patterns, allocations, resource usage
  6. Novelty       ( 5%) — Code distance from trivial/boilerplate patterns
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Score Result ──────────────────────────────────────────────────────────────

WEIGHTS = {
    "correctness":  0.30,
    "performance":  0.25,
    "code_quality": 0.15,
    "robustness":   0.15,
    "efficiency":   0.10,
    "novelty":      0.05,
}


@dataclass
class DimensionScore:
    score: float          # 0.0 - 1.0
    rationale: str        # Human-readable explanation
    flags: list[str] = field(default_factory=list)   # specific findings


@dataclass
class VitalisFitnessReport:
    language: str
    lines: int
    tokens: int

    correctness:  DimensionScore = field(default_factory=lambda: DimensionScore(0, ""))
    performance:  DimensionScore = field(default_factory=lambda: DimensionScore(0, ""))
    code_quality: DimensionScore = field(default_factory=lambda: DimensionScore(0, ""))
    robustness:   DimensionScore = field(default_factory=lambda: DimensionScore(0, ""))
    efficiency:   DimensionScore = field(default_factory=lambda: DimensionScore(0, ""))
    novelty:      DimensionScore = field(default_factory=lambda: DimensionScore(0, ""))

    scored_at_ms: float = 0.0
    engine_version: str = "vitalis-fitness/1.0"

    @property
    def total(self) -> float:
        """Weighted composite 0-100."""
        return round((
            self.correctness.score  * WEIGHTS["correctness"]
            + self.performance.score  * WEIGHTS["performance"]
            + self.code_quality.score * WEIGHTS["code_quality"]
            + self.robustness.score   * WEIGHTS["robustness"]
            + self.efficiency.score   * WEIGHTS["efficiency"]
            + self.novelty.score      * WEIGHTS["novelty"]
        ) * 100, 1)

    @property
    def grade(self) -> str:
        t = self.total
        if t >= 90: return "A+"
        if t >= 80: return "A"
        if t >= 70: return "B"
        if t >= 60: return "C"
        if t >= 50: return "D"
        return "F"

    @property
    def certificate(self) -> dict:
        return {
            "engine": self.engine_version,
            "language": self.language,
            "lines": self.lines,
            "total": self.total,
            "grade": self.grade,
            "dimensions": {
                "correctness":  {"score": round(self.correctness.score * 100, 1),  "weight": "30%", "rationale": self.correctness.rationale,  "flags": self.correctness.flags},
                "performance":  {"score": round(self.performance.score * 100, 1),  "weight": "25%", "rationale": self.performance.rationale,  "flags": self.performance.flags},
                "code_quality": {"score": round(self.code_quality.score * 100, 1), "weight": "15%", "rationale": self.code_quality.rationale, "flags": self.code_quality.flags},
                "robustness":   {"score": round(self.robustness.score * 100, 1),   "weight": "15%", "rationale": self.robustness.rationale,   "flags": self.robustness.flags},
                "efficiency":   {"score": round(self.efficiency.score * 100, 1),   "weight": "10%", "rationale": self.efficiency.rationale,   "flags": self.efficiency.flags},
                "novelty":      {"score": round(self.novelty.score * 100, 1),      "weight": "5%",  "rationale": self.novelty.rationale,      "flags": self.novelty.flags},
            },
            "scored_at_ms": round(self.scored_at_ms, 1),
        }


# ── Main Entry Point ───────────────────────────────────────────────────────────

def score(code: str, language: str = "auto") -> VitalisFitnessReport:
    """Score code across all 6 Vitalis fitness dimensions."""
    t0 = time.perf_counter()

    if language == "auto":
        language = _detect_language(code)

    lines = len(code.split("\n"))
    tokens = len(code.split())

    report = VitalisFitnessReport(language=language, lines=lines, tokens=tokens)

    scorer = _SCORERS.get(language, _generic_set)
    report.correctness  = scorer["correctness"](code)
    report.performance  = scorer["performance"](code)
    report.code_quality = scorer["code_quality"](code)
    report.robustness   = scorer["robustness"](code)
    report.efficiency   = scorer["efficiency"](code)
    report.novelty      = _score_novelty(code, language)

    report.scored_at_ms = (time.perf_counter() - t0) * 1000
    return report


# ── Language Detection ─────────────────────────────────────────────────────────

def _detect_language(code: str) -> str:
    code_lower = code.lower()
    if "def " in code and ("import " in code or "print(" in code):
        return "python"
    if "fn " in code and ("->" in code or "let mut" in code or "impl " in code):
        return "rust"
    if "func " in code and ("package " in code or "fmt." in code):
        return "go"
    if ("const " in code or "let " in code or "interface " in code) and ("=>" in code or "async " in code):
        return "typescript"
    if "#!/bin/bash" in code or ("echo " in code and "fi" in code):
        return "bash"
    if "function " in code or "var " in code or "require(" in code:
        return "javascript"
    if "public class" in code or "public static void main" in code:
        return "java"
    if "#include" in code and ("{" in code):
        return "cpp"
    return "generic"


# ── Python Scorers ─────────────────────────────────────────────────────────────

def _py_correctness(code: str) -> DimensionScore:
    flags = []
    score = 1.0

    # Syntax parse
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return DimensionScore(0.05, f"Syntax error: {e}", ["SYNTAX_ERROR"])

    # Check for undefined name patterns
    if "NameError" in code:
        flags.append("REFERENCES_NAMEERROR")
        score -= 0.1

    # Check has at least one function/class
    has_func = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for n in ast.walk(tree))
    if not has_func:
        flags.append("NO_FUNCTIONS_OR_CLASSES")
        score -= 0.15

    # Check for bare except (hides errors)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            flags.append("BARE_EXCEPT")
            score -= 0.1
            break

    # Check imports resolve to known-good stdlib
    good = {"os", "sys", "re", "json", "time", "math", "pathlib", "typing", "dataclasses",
            "collections", "itertools", "functools", "subprocess", "hashlib", "logging",
            "argparse", "datetime", "threading", "asyncio", "http", "urllib"}
    imports = [n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)]
    from_imports = [n.module.split(".")[0] if n.module else "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    all_imports = set(imports + from_imports) - {""}
    unknown = all_imports - good
    if unknown:
        flags.append(f"THIRD_PARTY_IMPORTS: {', '.join(list(unknown)[:3])}")
        # Not a deduction — third-party is fine

    return DimensionScore(max(0.0, min(1.0, score)), "AST parse successful" if not flags else "; ".join(flags[:2]), flags)


def _py_performance(code: str) -> DimensionScore:
    flags = []
    score = 1.0
    try:
        tree = ast.parse(code)
    except Exception:
        return DimensionScore(0.3, "Could not parse for performance analysis", [])

    # Nested loops (O(n²) risk)
    for node in ast.walk(tree):
        if isinstance(node, ast.For):
            inner = any(isinstance(n, ast.For) for n in ast.walk(node))
            if inner:
                flags.append("NESTED_LOOPS (O(n²) risk)")
                score -= 0.2
                break

    # List comprehension vs append loop
    append_count = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "append")
    listcomp_count = sum(1 for n in ast.walk(tree) if isinstance(n, ast.ListComp))
    if append_count > 2 and listcomp_count == 0:
        flags.append("USES_APPEND_LOOP (consider list comprehension)")
        score -= 0.1

    # Re-compiling regex in loops
    if "re.compile" not in code and "re." in code:
        flags.append("REGEX_NOT_PRECOMPILED")
        score -= 0.05

    # String concatenation in loops
    if "+=" in code and "str" in code.lower():
        flags.append("STRING_CONCAT (consider join())")
        score -= 0.05

    rationale = "Good algorithmic patterns" if not flags else "; ".join(flags[:2])
    return DimensionScore(max(0.0, min(1.0, score)), rationale, flags)


def _py_code_quality(code: str) -> DimensionScore:
    flags = []
    score = 1.0
    try:
        tree = ast.parse(code)
    except Exception:
        return DimensionScore(0.2, "Parse failed", [])

    # Docstrings
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    documented = sum(1 for f in funcs if ast.get_docstring(f))
    if funcs:
        doc_ratio = documented / len(funcs)
        if doc_ratio < 0.5:
            flags.append(f"POOR_DOCUMENTATION ({documented}/{len(funcs)} functions documented)")
            score -= 0.2

    # Type annotations
    annotated = sum(1 for f in funcs if f.returns or any(a.annotation for a in f.args.args))
    if funcs and annotated / len(funcs) < 0.5:
        flags.append("MISSING_TYPE_HINTS")
        score -= 0.15

    # Magic numbers
    magic = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and n.value not in (0, 1, -1, 2, True, False)]
    if len(magic) > 5:
        flags.append(f"MAGIC_NUMBERS ({len(magic)} found)")
        score -= 0.1

    # has main guard
    if "if __name__" not in code and len(funcs) > 0:
        flags.append("NO_MAIN_GUARD")
        score -= 0.05

    # Line length
    long_lines = [i+1 for i, l in enumerate(code.split("\n")) if len(l) > 120]
    if long_lines:
        flags.append(f"LONG_LINES (>{120} chars on lines {long_lines[:3]})")
        score -= 0.05

    rationale = "Good code quality" if not flags else "; ".join(flags[:2])
    return DimensionScore(max(0.0, min(1.0, score)), rationale, flags)


def _py_robustness(code: str) -> DimensionScore:
    flags = []
    score = 0.5  # Start at 50 — must earn points

    try:
        tree = ast.parse(code)
    except Exception:
        return DimensionScore(0.1, "Parse failed", [])

    # Has try/except
    has_try = any(isinstance(n, ast.Try) for n in ast.walk(tree))
    if has_try:
        score += 0.2

    # Has logging
    if "logging" in code or "logger" in code:
        score += 0.1

    # Has input validation (isinstance, len check, if not x)
    has_validation = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "isinstance"
        for n in ast.walk(tree)
    )
    if has_validation:
        score += 0.1
        flags.append("HAS_TYPE_VALIDATION")

    # Has argparse or click (CLI robustness)
    if "argparse" in code or "click" in code or "typer" in code:
        score += 0.1
        flags.append("HAS_CLI_ARGUMENT_PARSING")

    # Raises specific exceptions
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    if raises:
        score += 0.05

    # None checks
    if "is None" in code or "is not None" in code:
        score += 0.05

    deductions = []
    if not has_try:
        deductions.append("NO_ERROR_HANDLING")
    rationale = "Robust error handling" if has_try else "Missing error handling"
    return DimensionScore(max(0.0, min(1.0, score)), rationale, deductions + flags)


def _py_efficiency(code: str) -> DimensionScore:
    flags = []
    score = 1.0
    try:
        tree = ast.parse(code)
    except Exception:
        return DimensionScore(0.5, "Parse failed", [])

    # Global variables (memory leak risk)
    globals_count = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Global))
    if globals_count > 2:
        flags.append(f"EXCESSIVE_GLOBALS ({globals_count})")
        score -= 0.15

    # Reading full file into memory
    if ".read()" in code and "with open" in code:
        lines_code = code.split("\n")
        for line in lines_code:
            if ".read()" in line and "chunk" not in line:
                flags.append("READS_ENTIRE_FILE (use chunk/readline for large files)")
                score -= 0.1
                break

    # Generator vs list when iterating once
    list_then_loop = re.findall(r"list\(.*\)\s*\n.*for .* in ", code)
    if list_then_loop:
        flags.append("LIST_WHEN_GENERATOR_SUFFICIENT")
        score -= 0.05

    # Repeated dict/list creation inside loops
    if re.search(r"for .+:\s*\n.*(= \[\]|= \{\})", code, re.MULTILINE):
        flags.append("OBJECT_CREATION_IN_LOOP")
        score -= 0.1

    rationale = "Efficient memory patterns" if not flags else "; ".join(flags[:2])
    return DimensionScore(max(0.0, min(1.0, score)), rationale, flags)


# ── Generic/Cross-language Scorers ─────────────────────────────────────────────

def _generic_correctness(code: str) -> DimensionScore:
    flags = []
    score = 0.7

    if len(code.strip()) < 10:
        return DimensionScore(0.0, "Empty or trivial code", ["TRIVIAL"])

    # Balanced braces
    if code.count("{") != code.count("}"):
        flags.append("UNBALANCED_BRACES")
        score -= 0.4
    if code.count("(") != code.count(")"):
        flags.append("UNBALANCED_PARENS")
        score -= 0.3

    # Has function definition
    has_fn = bool(re.search(r"\b(fn |func |function |def |sub |method )\w+\s*\(", code))
    if has_fn:
        score += 0.2
    else:
        flags.append("NO_FUNCTION_DEFINITIONS")
        score -= 0.1

    # Has entry point
    has_main = bool(re.search(r"\b(main|__name__|if __name__|async fn main|func main)\b", code))
    if has_main:
        score += 0.1

    return DimensionScore(max(0.0, min(1.0, score)), "Structural analysis" if not flags else "; ".join(flags[:2]), flags)


def _generic_performance(code: str) -> DimensionScore:
    flags = []
    score = 0.7

    lines = code.split("\n")

    # Nesting depth
    max_depth = 0
    cur_depth = 0
    for line in lines:
        cur_depth += line.count("{") - line.count("}")
        max_depth = max(max_depth, cur_depth)

    if max_depth > 5:
        flags.append(f"DEEP_NESTING (depth {max_depth})")
        score -= 0.2
    elif max_depth > 3:
        flags.append(f"MODERATE_NESTING (depth {max_depth})")
        score -= 0.1

    # Function length (God functions)
    fn_lengths = re.findall(r"(?:fn|func|def|function)\s+\w+[^{]*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", code, re.DOTALL)
    for fn_body in fn_lengths:
        if len(fn_body.split("\n")) > 80:
            flags.append("GOD_FUNCTION (>80 lines)")
            score -= 0.15
            break

    rationale = "Acceptable complexity" if not flags else "; ".join(flags[:2])
    return DimensionScore(max(0.0, min(1.0, score)), rationale, flags)


def _generic_code_quality(code: str) -> DimensionScore:
    flags = []
    score = 0.6

    # Has comments
    comment_lines = len([l for l in code.split("\n") if l.strip().startswith(("//", "#", "/*", "*", "--"))])
    code_lines = len([l for l in code.split("\n") if l.strip()])
    if code_lines > 0:
        comment_ratio = comment_lines / code_lines
        if comment_ratio >= 0.15:
            score += 0.2
        elif comment_ratio >= 0.05:
            score += 0.1
        else:
            flags.append("POOR_DOCUMENTATION (<5% comment ratio)")
            score -= 0.1

    # Naming convention (snake_case or camelCase consistency)
    snake = len(re.findall(r"\b[a-z]+_[a-z]+\b", code))
    camel = len(re.findall(r"\b[a-z][a-zA-Z]+\b", code))
    if snake > 5 and camel > 5:
        flags.append("MIXED_NAMING_CONVENTIONS")
        score -= 0.1

    # TODO/FIXME/HACK
    hacks = len(re.findall(r"\b(TODO|FIXME|HACK|XXX)\b", code, re.IGNORECASE))
    if hacks > 0:
        flags.append(f"TECHNICAL_DEBT ({hacks} TODO/FIXME/HACK markers)")
        score -= min(0.15, hacks * 0.05)

    # Has logging
    if re.search(r"\b(log\.|logger\.|logging\.|console\.log|fmt\.Print|println!)\b", code):
        score += 0.1

    # Long lines
    long = sum(1 for l in code.split("\n") if len(l) > 120)
    if long > 3:
        flags.append(f"LONG_LINES ({long} lines >120 chars)")
        score -= 0.05

    rationale = "Code style analysis" if not flags else "; ".join(flags[:2])
    return DimensionScore(max(0.0, min(1.0, score)), rationale, flags)


def _generic_robustness(code: str) -> DimensionScore:
    flags = []
    score = 0.5

    # Error handling patterns
    error_patterns = [
        r"\b(try|catch|except|rescue|recover|defer|Result|Option|unwrap_or)\b",
        r"\b(throw|raise|panic|error|err\b)",
        r"\b(if err != nil|if error|if e != nil)\b",
    ]
    for p in error_patterns:
        if re.search(p, code):
            score += 0.1
            break

    # Input validation
    if re.search(r"\b(validate|assert|check|guard|precondition|require)\b", code, re.IGNORECASE):
        score += 0.1
        flags.append("HAS_VALIDATION")

    # Null/nil checks
    if re.search(r"\b(nil|null|None|undefined|isEmpty|isNil)\b", code):
        if re.search(r"\b(== nil|!= nil|== null|!= null|is None|is not None|??\s)", code):
            score += 0.1
            flags.append("HAS_NULL_CHECKS")

    # CLI argument handling
    if re.search(r"\b(args|argv|flags|flag\.|cobra|argparse|clap|docopt)\b", code):
        score += 0.1
        flags.append("HAS_ARG_HANDLING")

    # Timeout/context
    if re.search(r"\b(timeout|context|ctx|deadline|cancel)\b", code):
        score += 0.05
        flags.append("HAS_TIMEOUT_HANDLING")

    rationale = "Robustness analysis" if score > 0.6 else "Insufficient error handling"
    return DimensionScore(max(0.0, min(1.0, score)), rationale, flags)


def _generic_efficiency(code: str) -> DimensionScore:
    flags = []
    score = 0.7

    # Streaming vs loading all
    if re.search(r"\b(stream|chunk|buffer|iter|generator|yield|lazy)\b", code):
        score += 0.1
        flags.append("USES_STREAMING_PATTERNS")

    # Caching
    if re.search(r"\b(cache|memo|lru_cache|cached|memoize)\b", code, re.IGNORECASE):
        score += 0.1
        flags.append("HAS_CACHING")

    # Connection pooling
    if re.search(r"\b(pool|Pool|connection_pool)\b", code):
        score += 0.05

    # Obvious memory waste
    if re.search(r"\b(readlines\(\)|read\(\))\b", code):
        flags.append("READS_ENTIRE_FILE_INTO_MEMORY")
        score -= 0.1

    rationale = "Efficiency patterns" if not flags or any("USES" in f or "HAS" in f for f in flags) else "; ".join(flags[:2])
    return DimensionScore(max(0.0, min(1.0, score)), rationale, flags)


def _score_novelty(code: str, language: str) -> DimensionScore:
    """Score how novel/non-boilerplate the code is."""
    score = 0.5
    flags = []

    lines = [l.strip() for l in code.split("\n") if l.strip()]
    total = len(lines)

    if total < 10:
        return DimensionScore(0.1, "Too short to assess novelty", ["TRIVIAL"])

    # Boilerplate patterns that score low
    boilerplate = [
        r"print\(\"Hello, World\"\)",
        r"return a \+ b",
        r"def add\(a, b\):",
        r"console\.log\(\"Hello\"\)",
        r"fmt\.Println\(\"Hello\"\)",
    ]
    bp_hits = sum(1 for p in boilerplate if re.search(p, code, re.IGNORECASE))
    score -= bp_hits * 0.2

    # Novel patterns score high
    novel_patterns = [
        r"\basync\b.*\bawait\b",      # async/await
        r"\byield\b",                  # generators
        r"\bdecorator\b|@\w+",        # decorators
        r"\bContextManager\b|__enter__",
        r"\bprotocol\b|\binterface\b",
        r"\bgeneric\b|<T>|\[T\]",     # generics
        r"\bfunctional\b|\.map\(|\.filter\(|\.reduce\(",
    ]
    for p in novel_patterns:
        if re.search(p, code):
            score += 0.05
            flags.append(f"USES_ADVANCED_PATTERN")
            if score >= 0.9:
                break

    # Length bonus (more code = more novel by default)
    if total > 100:
        score += 0.1
    elif total > 50:
        score += 0.05

    rationale = "Novel / non-trivial implementation" if score > 0.6 else "Generic/boilerplate patterns detected"
    return DimensionScore(max(0.0, min(1.0, score)), rationale, list(set(flags))[:3])


# ── Generic fallback set ───────────────────────────────────────────────────────

_generic_set = {
    "correctness":  _generic_correctness,
    "performance":  _generic_performance,
    "code_quality": _generic_code_quality,
    "robustness":   _generic_robustness,
    "efficiency":   _generic_efficiency,
}

_python_set = {
    "correctness":  _py_correctness,
    "performance":  _py_performance,
    "code_quality": _py_code_quality,
    "robustness":   _py_robustness,
    "efficiency":   _py_efficiency,
}

# Rust, Go, TypeScript, Bash all use generic with tuned weights
_SCORERS = {
    "python":     _python_set,
    "generic":    _generic_set,
    "rust":       _generic_set,
    "go":         _generic_set,
    "typescript": _generic_set,
    "javascript": _generic_set,
    "bash":       _generic_set,
    "java":       _generic_set,
    "cpp":        _generic_set,
    "csharp":     _generic_set,
}

def _score_generic(dim):
    return _generic_set[dim]
