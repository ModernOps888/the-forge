"""
The Forge — Agent Factory (Multi-Language)

The centerpiece of The Forge Nexus Protocol.

Builds production-grade code artifacts in ANY language from natural language:
  Python | Rust | TypeScript | Go | C | YAML | Dockerfile | Vitalis .sl

  Artifact types:
    agent, mcp_server, api, cli, pipeline, integration, workflow,
    extension, docker, skill, full_project, rust_lib, go_service,
    react_app, terraform, sdk, webhook

  Scoring pipeline:
    1. LLM generation (4 models compete)
    2. Vitalis hotpath-based heuristic scoring (line count, import density,
       error handling ratio, complexity estimate) — sub-µs via native Rust
    3. LLM scoring (Gemini, cheapest) for semantic quality
    4. Final score = 0.4*LLM + 0.6*heuristic (trust math over LLM)
    5. Winner saved + registered to marketplace
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .providers import call_openrouter, MODELS, CODE_SYSTEM
from .budget import get_tracker, RequestTrace
from .governance import get_provenance
from .skills import SkillDNA, certify_from_compilation

# Try Vitalis hotpaths for heuristic scoring
try:
    from .vitalis_ffi import VitalisDLL
    _vdll = VitalisDLL()
    _HAS_VITALIS = True
except Exception:
    _vdll = None
    _HAS_VITALIS = False


# ── Output Types (Multi-Language) ───────────────────────────────────────────────

ARTIFACT_TYPES = {
    # Python
    "agent":        "Full autonomous agent with tools, memory, and multi-step reasoning",
    "mcp_server":   "MCP server with tool definitions, JSON-RPC, stdio/HTTP transport",
    "api":          "REST API with endpoints, request/response models, error handling",
    "cli":          "Command-line tool with argparse, rich output, config management",
    "pipeline":     "Data pipeline: ingest, transform, validate, output",
    "integration":  "Third-party integration connector (Slack, GitHub, Notion, etc.)",
    "sdk":          "Python SDK / client library with retries, auth, pagination",
    "webhook":      "Webhook receiver with signature verification, queue, retry",
    # Multi-language
    "rust_lib":     "Rust library crate with pub API, error types, tests, Cargo.toml",
    "go_service":   "Go microservice with HTTP handlers, middleware, graceful shutdown",
    "react_app":    "React/Next.js component or page with TypeScript, hooks, CSS",
    "terraform":    "Terraform IaC module with variables, outputs, remote state",
    # Infra
    "workflow":     "GitHub Actions / CI-CD workflow YAML",
    "extension":    "VS Code extension in TypeScript",
    "docker":       "Dockerfile + docker-compose.yml",
    # Native
    "skill":        "Vitalis .sl hot-path function (compiled to native)",
    "full_project": "Complete multi-file project with structure, deps, README",
}

LANG_MAP = {
    "agent":        "python",
    "mcp_server":   "python",
    "api":          "python",
    "cli":          "python",
    "pipeline":     "python",
    "integration":  "python",
    "sdk":          "python",
    "webhook":      "python",
    "rust_lib":     "rust",
    "go_service":   "go",
    "react_app":    "typescript",
    "terraform":    "hcl",
    "workflow":     "yaml",
    "extension":    "typescript",
    "docker":       "dockerfile",
    "skill":        "vitalis",
    "full_project": "python",
}


# ── Prompts ───────────────────────────────────────────────────────────────────

FACTORY_SYSTEM = """You are a principal software engineer and AI architect.
You write production-grade, elite-level code. No tutorials. No boilerplate comments.
Code that would pass a FAANG principal review.

Rules:
1. Return ONLY the code. No explanation. No markdown preamble.
2. Use code blocks with language tag: ```python ... ``` or ```typescript ... ```
3. Include ALL imports, ALL error handling, ALL type hints.
4. Production-ready: logging, config via env vars, docstrings on public APIs.
5. If given a signature or interface, match it exactly.
6. Security: validate inputs, never hardcode secrets, use env vars.
"""

SCORER_SYSTEM = """You are a principal software engineer doing a code review.
Score this code from 0-100 on these dimensions:
- correctness (0-100): does it do what was asked?
- completeness (0-100): are all features implemented?
- security (0-100): no hardcoded secrets, validates inputs, safe patterns?
- quality (0-100): idiomatic, readable, no obvious bugs?
- production_ready (0-100): error handling, logging, config?

Return ONLY valid JSON, no explanation:
{"correctness": 85, "completeness": 90, "security": 95, "quality": 88, "production_ready": 80}
"""


# ── Artifact ──────────────────────────────────────────────────────────────────

@dataclass
class Artifact:
    """A generated code artifact from the factory."""
    artifact_id: str = ""
    artifact_type: str = ""
    language: str = ""
    description: str = ""
    provider_name: str = ""
    model_id: str = ""
    source: str = ""                    # the full source code
    files: dict[str, str] = field(default_factory=dict)  # filename -> content

    # Scoring
    score_correctness: float = 0.0
    score_completeness: float = 0.0
    score_security: float = 0.0
    score_quality: float = 0.0
    score_production: float = 0.0
    score_total: float = 0.0

    # Metadata
    tokens_used: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    generated_at: float = 0.0
    provenance_hash: str = ""

    def __post_init__(self):
        if not self.artifact_id:
            self.artifact_id = f"art_{uuid.uuid4().hex[:8]}"
        if not self.generated_at:
            self.generated_at = time.time()

    # Heuristic scores (from Vitalis hotpaths)
    heuristic_line_score: float = 0.0
    heuristic_import_density: float = 0.0
    heuristic_error_handling: float = 0.0
    heuristic_complexity: float = 0.0
    heuristic_total: float = 0.0

    def compute_total_score(self) -> float:
        """Combined score: 40% LLM judgment + 60% heuristic math (trust the compiler)."""
        llm_weights = {
            "correctness": 0.30,
            "completeness": 0.25,
            "quality": 0.20,
            "security": 0.15,
            "production": 0.10,
        }
        llm_score = (
            self.score_correctness * llm_weights["correctness"]
            + self.score_completeness * llm_weights["completeness"]
            + self.score_quality * llm_weights["quality"]
            + self.score_security * llm_weights["security"]
            + self.score_production * llm_weights["production"]
        )
        # Blend: trust heuristics more than LLM self-evaluation
        if self.heuristic_total > 0:
            self.score_total = 0.4 * llm_score + 0.6 * self.heuristic_total
        else:
            self.score_total = llm_score
        return self.score_total

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "language": self.language,
            "description": self.description,
            "provider": self.provider_name,
            "model": self.model_id,
            "score_total": round(self.score_total, 2),
            "score_correctness": self.score_correctness,
            "score_completeness": self.score_completeness,
            "score_security": self.score_security,
            "score_quality": self.score_quality,
            "score_production": self.score_production,
            "tokens": self.tokens_used,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "generated_at": self.generated_at,
        }


@dataclass
class FactoryResult:
    """Result from a factory run — all artifacts + winner."""
    run_id: str = ""
    description: str = ""
    artifact_type: str = ""
    artifacts: list[Artifact] = field(default_factory=list)
    winner: Optional[Artifact] = None
    total_cost: float = 0.0
    total_tokens: int = 0
    elapsed_s: float = 0.0
    output_path: Optional[Path] = None

    def __post_init__(self):
        if not self.run_id:
            self.run_id = f"run_{uuid.uuid4().hex[:8]}"

    def leaderboard(self) -> str:
        lines = [f"  Factory Result: {self.run_id}", f"  Task: {self.description[:60]}"]
        lines.append(f"  {'─' * 52}")
        sorted_arts = sorted(self.artifacts, key=lambda a: a.score_total, reverse=True)
        for i, a in enumerate(sorted_arts):
            trophy = "🏆" if i == 0 else f" {i+1}"
            lines.append(
                f"  {trophy} {a.provider_name:12s} │ score: {a.score_total:.1f} │ "
                f"${a.cost_usd:.4f} │ {a.tokens_used} tok │ {a.latency_ms:.0f}ms"
            )
        lines.append(f"  {'─' * 52}")
        lines.append(f"  Total cost: ${self.total_cost:.4f} | Tokens: {self.total_tokens:,} | Time: {self.elapsed_s:.1f}s")
        return "\n".join(lines)


# ── Core Factory Function ─────────────────────────────────────────────────────

def build(
    description: str,
    artifact_type: str = "auto",
    models: list[str] | None = None,
    extra_context: str = "",
    save_dir: Path | None = None,
    score: bool = True,
) -> FactoryResult:
    """
    Multi-LLM Agent Factory.
    
    Generates a complete code artifact from a natural language description.
    All selected models generate implementations in parallel (sequential for safety).
    Each implementation is scored. The best becomes the winner.
    
    Args:
        description: What to build ("build a GitHub MCP server with PR tool")
        artifact_type: One of ARTIFACT_TYPES keys, or "auto" to detect
        models: List of model names. Defaults to all 4 primary models.
        extra_context: Additional constraints, interfaces, or requirements
        save_dir: Where to save artifacts (defaults to C:/TheForge/output/)
        score: Whether to LLM-score each artifact (costs tokens but improves quality)
    
    Returns:
        FactoryResult with all artifacts and the winner
    """
    t0 = time.time()
    tracker = get_tracker()
    provenance = get_provenance()

    # Resolve type
    if artifact_type == "auto":
        artifact_type = _detect_type(description)
    language = LANG_MAP.get(artifact_type, "python")

    models = models or ["claude", "gpt", "gemini", "deepseek"]
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    provenance.record("factory_start", run_id, "factory", {
        "description": description[:100],
        "type": artifact_type,
        "models": models,
    })

    print(f"\n  🏭 FORGE AGENT FACTORY")
    print(f"  Run ID:  {run_id}")
    print(f"  Task:    {description[:70]}")
    print(f"  Type:    {artifact_type} ({language})")
    print(f"  Models:  {', '.join(models)}")
    print(f"  {'─' * 52}")

    # Build generation prompt
    gen_prompt = _build_prompt(description, artifact_type, language, extra_context)

    # Generate from each model
    cost_before = tracker.total_cost
    artifacts: list[Artifact] = []

    for model_name in models:
        model_id = MODELS.get(model_name, model_name)
        print(f"  ⚙️  Generating with {model_name}...", end="", flush=True)

        try:
            t_model = time.time()
            messages = [
                {"role": "system", "content": FACTORY_SYSTEM},
                {"role": "user", "content": gen_prompt},
            ]
            content, usage = call_openrouter(
                model_id, messages,
                max_tokens=4096, temperature=0.4,
                provider_name=model_name, purpose="factory_gen",
            )
            latency = (time.time() - t_model) * 1000

            source = _extract_code(content, language)
            artifact = Artifact(
                artifact_type=artifact_type,
                language=language,
                description=description,
                provider_name=model_name,
                model_id=model_id,
                source=source,
                tokens_used=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                cost_usd=usage.get("cost_usd", 0),
                latency_ms=latency,
            )
            artifacts.append(artifact)
            print(f" ✅ ({artifact.tokens_used} tok, ${artifact.cost_usd:.4f})")

        except Exception as e:
            print(f" ❌ {e}")

    if not artifacts:
        raise RuntimeError("All models failed to generate")

    # Heuristic score via Vitalis hotpaths (instant, free)
    print(f"\n  ⚡ Heuristic scoring via {'Vitalis native' if _HAS_VITALIS else 'Python fallback'}...")
    for art in artifacts:
        _heuristic_score(art)

    # LLM score each artifact (costs tokens but adds semantic judgment)
    if score and artifacts:
        print(f"  📊 LLM scoring {len(artifacts)} implementations (Gemini)...")
        _score_artifacts(artifacts)

    # Compute blended score
    for art in artifacts:
        art.compute_total_score()

    # Select winner
    winner = max(artifacts, key=lambda a: a.score_total)

    # Save output
    output_path = _save_artifacts(run_id, description, artifact_type, artifacts, winner, save_dir)

    result = FactoryResult(
        run_id=run_id,
        description=description,
        artifact_type=artifact_type,
        artifacts=artifacts,
        winner=winner,
        total_cost=round(tracker.total_cost - cost_before, 6),
        total_tokens=sum(a.tokens_used for a in artifacts),
        elapsed_s=round(time.time() - t0, 1),
        output_path=output_path,
    )

    # Provenance
    provenance.record("factory_complete", run_id, "factory", {
        "winner_provider": winner.provider_name,
        "winner_score": round(winner.score_total, 2),
        "total_cost": result.total_cost,
        "output": str(output_path),
    })

    print(f"\n{result.leaderboard()}")
    print(f"\n  💾 Saved to: {output_path}")

    return result


def _score_artifacts(artifacts: list[Artifact]) -> None:
    """LLM-score each artifact using gemini (cheapest)."""
    scorer_model = MODELS.get("gemini-lite", "google/gemini-3.5-flash")

    for art in artifacts:
        if not art.source or len(art.source) < 20:
            art.score_total = 0
            continue
        try:
            messages = [
                {"role": "system", "content": SCORER_SYSTEM},
                {"role": "user", "content": f"Task: {art.description}\n\nCode:\n```\n{art.source[:3000]}\n```\n\nScore as JSON:"},
            ]
            content, usage = call_openrouter(
                scorer_model, messages,
                max_tokens=128, temperature=0.0,
                provider_name="gemini", purpose="factory_score",
            )
            # Extract JSON
            m = re.search(r'\{[^}]+\}', content)
            if m:
                scores = json.loads(m.group())
                art.score_correctness = scores.get("correctness", 70)
                art.score_completeness = scores.get("completeness", 70)
                art.score_security = scores.get("security", 70)
                art.score_quality = scores.get("quality", 70)
                art.score_production = scores.get("production_ready", 70)
                art.compute_total_score()
        except Exception:
            # Fallback: simple heuristic scoring
            art.score_correctness = 70
            art.score_completeness = 60 if art.source else 0
            art.score_quality = min(90, len(art.source.split("\n")) * 0.5)
            art.score_security = 70
            art.score_production = 65
            art.compute_total_score()


def _save_artifacts(
    run_id: str,
    description: str,
    artifact_type: str,
    artifacts: list[Artifact],
    winner: Artifact,
    save_dir: Path | None,
) -> Path:
    """Save all artifacts to disk."""
    base = save_dir or Path("C:/TheForge/output")
    run_dir = base / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Save winner (primary output)
    ext = _ext_for_lang(winner.language)
    winner_path = run_dir / f"winner_{winner.provider_name}{ext}"
    winner_path.write_text(winner.source, encoding="utf-8")

    # Save all alternatives
    for art in artifacts:
        alt_path = run_dir / f"{art.provider_name}{ext}"
        alt_path.write_text(art.source, encoding="utf-8")

    # Save manifest
    manifest = {
        "run_id": run_id,
        "description": description,
        "artifact_type": artifact_type,
        "winner": winner.to_dict(),
        "all_artifacts": [a.to_dict() for a in sorted(artifacts, key=lambda x: x.score_total, reverse=True)],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return run_dir


def _detect_type(description: str) -> str:
    """Heuristic type detection from description."""
    d = description.lower()
    if any(k in d for k in ("mcp server", "mcp tool", "mcp")):
        return "mcp_server"
    if any(k in d for k in ("fastapi", "flask", "rest api", "api server", "http server")):
        return "api"
    if any(k in d for k in ("cli", "command line", "terminal tool")):
        return "cli"
    if any(k in d for k in ("rust", "cargo", "crate", ".rs")):
        return "rust_lib"
    if any(k in d for k in ("golang", "go service", "go http", "gin ", "fiber ")):
        return "go_service"
    if any(k in d for k in ("react", "next.js", "nextjs", "component")):
        return "react_app"
    if any(k in d for k in ("terraform", "iac", "infra as code")):
        return "terraform"
    if any(k in d for k in ("sdk", "client library", "api client")):
        return "sdk"
    if any(k in d for k in ("webhook", "callback")):
        return "webhook"
    if any(k in d for k in ("github action", "ci/cd", "workflow yaml")):
        return "workflow"
    if any(k in d for k in ("dockerfile", "docker", "container")):
        return "docker"
    if any(k in d for k in ("vscode", "vs code", "extension", "cursor plugin")):
        return "extension"
    if any(k in d for k in ("pipeline", "etl", "data processing")):
        return "pipeline"
    if any(k in d for k in (".sl", "vitalis", "sort function", "hot path")):
        return "skill"
    if any(k in d for k in ("agent", "autonomous", "multi-agent", "crew", "autogen")):
        return "agent"
    return "agent"  # safe default


def _build_prompt(description: str, artifact_type: str, language: str, extra: str) -> str:
    type_context = {
        "mcp_server": (
            "Build a complete MCP (Model Context Protocol) server. "
            "JSON-RPC 2.0 over stdio transport. handle_initialize returning capabilities. "
            "handle_tools_list returning tool schemas. handle_tool_call executing tools. "
            "Production error handling. Python stdlib only. Runnable: python server.py"
        ),
        "api": (
            "Build a complete FastAPI REST API. Include: "
            "Pydantic models, proper HTTP status codes, async endpoints, "
            "CORS middleware, health check endpoint, env-var based config, "
            "OpenAPI docs auto-generation. Runnable as: uvicorn main:app"
        ),
        "agent": (
            "Build a complete autonomous AI agent in Python. Include: "
            "tool definitions, multi-step reasoning loop, memory/state management, "
            "structured output, graceful error recovery, configurable LLM provider, "
            "conversation history. Use only stdlib + requests if needed. No LangChain."
        ),
        "cli": (
            "Build a complete CLI tool with argparse. Include: "
            "subcommands, rich colored output (ANSI), config file support, "
            "verbose/quiet modes, proper exit codes, version flag."
        ),
        "pipeline": (
            "Build a complete data pipeline. Include: "
            "configurable data source, transformation stages, validation, "
            "error recovery, progress tracking, output to multiple sinks."
        ),
        "workflow": (
            "Write a complete GitHub Actions workflow YAML. Include: "
            "trigger events, job dependencies, caching, secrets handling, "
            "matrix builds if appropriate, artifact upload, deployment steps."
        ),
        "rust_lib": (
            "Write a complete Rust library crate. Include: "
            "pub API with proper visibility, error types with thiserror, "
            "comprehensive tests (#[cfg(test)]), documentation comments (///), "
            "Cargo.toml with dependencies. Follow Rust idioms: Result, Option, traits."
        ),
        "go_service": (
            "Write a complete Go microservice. Include: "
            "net/http or gin handlers, middleware (logging, auth), "
            "graceful shutdown with context, structured logging, "
            "health check endpoint, environment config."
        ),
        "react_app": (
            "Write a complete React/Next.js component or page in TypeScript. Include: "
            "proper hooks (useState, useEffect, useMemo), CSS modules or styled-components, "
            "error boundaries, loading states, accessibility (aria labels), "
            "responsive design. Export as default."
        ),
        "terraform": (
            "Write a complete Terraform module. Include: "
            "variables.tf, outputs.tf, main.tf, provider config, "
            "remote state backend, resource dependencies, tags."
        ),
        "sdk": (
            "Write a complete Python SDK / client library. Include: "
            "HTTP client with retries and exponential backoff, "
            "authentication (API key + OAuth), pagination helpers, "
            "typed response models, error hierarchy, rate limiting."
        ),
        "webhook": (
            "Write a complete webhook receiver. Include: "
            "signature verification (HMAC-SHA256), request validation, "
            "event routing, retry queue, idempotency, health check."
        ),
    }.get(artifact_type, "")

    prompt = f"""TASK: {description}

ARTIFACT TYPE: {artifact_type}
LANGUAGE: {language}
{type_context}

{"ADDITIONAL REQUIREMENTS: " + extra if extra else ""}

Write the complete, production-ready implementation.
Every import, every function, every edge case.
Principal engineer quality — not a tutorial, not a demo.
Return ONLY the code."""
    return prompt


def _extract_code(response: str, language: str) -> str:
    """Extract code block from LLM response."""
    # Try explicit language block first
    for lang in (language, "python", "typescript", "yaml", ""):
        fence = f"```{lang}"
        if fence in response:
            start = response.index(fence) + len(fence)
            end = response.rfind("```")
            if end > start:
                return response[start:end].strip()

    # If no fences, return as-is (model obeyed "return only code")
    return response.strip()


def _ext_for_lang(lang: str) -> str:
    return {
        "python": ".py", "typescript": ".ts", "yaml": ".yml",
        "dockerfile": ".dockerfile", "vitalis": ".sl", "javascript": ".js",
        "rust": ".rs", "go": ".go", "hcl": ".tf", "c": ".c",
    }.get(lang, ".txt")


# ── Vitalis Hotpath Heuristic Scoring ───────────────────────────────────────

def _heuristic_score(art: "Artifact") -> None:
    """
    Score an artifact using deterministic heuristics.
    If Vitalis FFI is available, delegates statistical ops to native Rust hotpaths
    for sub-microsecond execution. Otherwise falls back to Python.
    
    Metrics:
      - Line count score: rewards non-trivial implementations (50-500 lines sweet spot)
      - Import density: % of lines that are imports (lower = better)
      - Error handling: presence of try/except/catch/Result/Error
      - Complexity: function/method count relative to LOC
    """
    if not art.source or len(art.source) < 10:
        return

    lines = art.source.split("\n")
    loc = len(lines)
    non_empty = [l for l in lines if l.strip()]

    # 1. Line count score (bell curve: peak at ~200 lines)
    if loc < 20:
        line_score = 30.0
    elif loc < 50:
        line_score = 50.0
    elif loc < 150:
        line_score = 75.0
    elif loc < 400:
        line_score = 90.0
    elif loc < 800:
        line_score = 85.0
    else:
        line_score = 70.0  # too long can mean bloat

    # 2. Import density (lower = more actual logic)
    import_keywords = ("import ", "from ", "require(", "use ", "#include")
    import_lines = sum(1 for l in non_empty if any(l.strip().startswith(k) for k in import_keywords))
    import_ratio = import_lines / max(len(non_empty), 1)
    import_score = max(40.0, 100.0 - import_ratio * 300)  # penalty for >30% imports

    # 3. Error handling density
    error_keywords = ("try", "except", "catch", "raise", "Error", "Result<", "unwrap",
                      "panic", "error", "if err", "return err", "throw")
    error_lines = sum(1 for l in non_empty if any(k in l for k in error_keywords))
    error_ratio = error_lines / max(len(non_empty), 1)
    error_score = min(100.0, 50.0 + error_ratio * 500)  # reward error handling

    # 4. Function/method complexity (moderate is best)
    func_keywords = ("def ", "fn ", "func ", "function ", "async def ", "pub fn ",
                     "class ", "struct ", "impl ", "type ", "interface ")
    func_count = sum(1 for l in non_empty if any(l.strip().startswith(k) for k in func_keywords))
    func_ratio = func_count / max(loc, 1) * 100
    if func_ratio < 2:
        complexity_score = 50.0  # too monolithic
    elif func_ratio < 8:
        complexity_score = 90.0  # sweet spot
    elif func_ratio < 15:
        complexity_score = 80.0
    else:
        complexity_score = 60.0  # over-fragmented

    # Use Vitalis native weighted_score if available (sub-µs)
    scores = [line_score, import_score, error_score, complexity_score]
    weights = [0.25, 0.20, 0.30, 0.25]

    if _HAS_VITALIS and _vdll:
        try:
            total = _vdll.hotpath_weighted_score(scores, weights)
        except Exception:
            total = sum(s * w for s, w in zip(scores, weights))
    else:
        total = sum(s * w for s, w in zip(scores, weights))

    art.heuristic_line_score = round(line_score, 1)
    art.heuristic_import_density = round(import_score, 1)
    art.heuristic_error_handling = round(error_score, 1)
    art.heuristic_complexity = round(complexity_score, 1)
    art.heuristic_total = round(total, 1)
