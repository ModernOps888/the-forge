#!/usr/bin/env python3
"""
The Forge — Nexus MCP Server

A full Model Context Protocol server that exposes The Forge's entire engine
as callable tools for Claude, Cursor, VS Code Copilot, or any MCP-compatible agent.

Transport: JSON-RPC 2.0 over stdio (standard MCP transport)

Tools exposed:
  forge_compile        — Compile .sl code through 7-gate Vitalis pipeline
  forge_build          — Agent Factory: build any artifact from description
  forge_research       — Fan query to all selected models
  forge_consensus      — All models → synthesis
  forge_chat           — Chat with any model, cost-tracked
  forge_benchmark      — Benchmark .sl code via Vitalis native hotpaths
  forge_budget         — Current spend, tokens, remaining budget
  forge_market_search  — Search the skill marketplace
  forge_market_publish — Publish an artifact to marketplace
  forge_provenance     — Audit trail for any entity

Usage (add to Cursor / VS Code MCP config):
  {
    "mcpServers": {
      "the-forge": {
        "command": "python",
        "args": ["C:/TheForge/forge_mcp.py"],
        "env": {
          "OPENROUTER_API_KEY": "sk-or-v1-...",
          "VITALIS_ROOT": "C:/Vitalis-V60"
        }
      }
    }
  }
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from forge.compiler import compile_and_run, vitalis_version
from forge.providers import call_model, MODELS, CHAT_SYSTEM, RESEARCH_SYSTEM, get_vendor_status
from forge.budget import get_tracker, BudgetConfig
from forge.governance import get_provenance, get_policy_engine
from forge.factory import build as factory_build, ARTIFACT_TYPES
from forge.marketplace import get_marketplace
from forge.skills import certify_from_compilation
from forge.fitness_engine import score as fitness_score

TRACKER = get_tracker(BudgetConfig(max_budget_usd=50.0))
PROVENANCE = get_provenance()


# ── Tool Definitions (MCP Schema) ─────────────────────────────────────────────

TOOLS = [
    {
        "name": "vitalis_score",
        "description": (
            "VITALIS FITNESS ENGINE. Score any code in any language (Python, TypeScript, Rust, Go, Bash, Java, C++) "
            "across 6 quality dimensions: Correctness (30%), Performance (25%), Code Quality (15%), "
            "Robustness (15%), Efficiency (10%), Novelty (5%). Returns a 0-100 composite score + grade (A+/A/B/C/D/F) "
            "+ per-dimension breakdown with flags and rationale. Zero external tools required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Source code to evaluate"},
                "language": {
                    "type": "string",
                    "enum": ["auto", "python", "typescript", "javascript", "rust", "go", "java", "cpp", "bash", "csharp"],
                    "description": "Language of the code (use 'auto' to detect)",
                    "default": "auto",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "forge_orchestrate",
        "description": (
            "MASTER ORCHESTRATOR. Describe any coding task in plain English. Claude architects the solution spec, "
            "then multiple LLMs compete to generate production-quality, runnable code. The winner is scored by the "
            "Vitalis Fitness Engine and saved to disk. Supports Python, TypeScript, Rust, Go, Bash, PowerShell."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What you want built, in plain English"},
                "language": {
                    "type": "string",
                    "enum": ["auto", "python", "typescript", "rust", "go", "bash", "powershell"],
                    "default": "auto",
                },
                "models": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(MODELS.keys())},
                    "description": "Models to compete (default: claude + gemini)",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "forge_compile",
        "description": "Compile and execute Vitalis .sl code through the 7-gate JIT pipeline. Returns output, gate reached, compile time, and any errors.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Vitalis .sl source code to compile and run"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "forge_build",
        "description": (
            "THE AGENT FACTORY. Build any complete production artifact from a natural language description. "
            "Artifact types: agent, mcp_server, api, cli, pipeline, integration, workflow, extension, docker, skill, full_project. "
            "4 LLMs generate implementations in parallel. Each is scored. The best wins. Full cost tracking."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What to build — be specific and technical"},
                "artifact_type": {
                    "type": "string",
                    "enum": list(ARTIFACT_TYPES.keys()) + ["auto"],
                    "description": "Type of artifact to generate (use 'auto' to detect)",
                    "default": "auto",
                },
                "models": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(MODELS.keys())},
                    "description": "Models to use (default: all 4 primary models)",
                },
                "extra_context": {"type": "string", "description": "Additional requirements or constraints"},
            },
            "required": ["description"],
        },
    },
    {
        "name": "forge_research",
        "description": "Send a research query to multiple LLMs simultaneously. Each model responds independently. Full cost tracking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Research question or topic"},
                "models": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(MODELS.keys())},
                    "description": "Models to query (default: claude, gpt, gemini, deepseek)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "forge_consensus",
        "description": "Ask multiple models the same question, then synthesize all responses into one authoritative answer. Most expensive but highest quality.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "models": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(MODELS.keys())},
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "forge_chat",
        "description": "Chat with any model via smart vendor routing (direct API or OpenRouter fallback). Cost-tracked. Use cheapest model (gemini) for simple tasks, claude for complex reasoning.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "model": {"type": "string", "enum": list(MODELS.keys()), "default": "gemini"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "forge_budget",
        "description": "Get current API cost report: total spend, remaining budget, per-provider breakdown, token usage, optimization hints.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "forge_market_search",
        "description": "Search the Forge marketplace for skills and artifacts by name, category, or tag.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term"},
                "kind": {"type": "string", "enum": ["skill", "artifact", ""], "default": ""},
            },
        },
    },
    {
        "name": "forge_provenance",
        "description": "Get the cryptographic audit trail from The Forge's provenance chain. Shows all events, actors, and hash links.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max entries to return", "default": 20},
            },
        },
    },
    {
        "name": "forge_provider_status",
        "description": "Get status of all configured LLM vendors (Anthropic, OpenAI, Google, DeepSeek, OpenRouter, Ollama). Shows which have active API keys and their endpoints.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ── Tool Handlers ─────────────────────────────────────────────────────────────

def handle_forge_compile(params: dict) -> dict:
    code = params.get("code", "")
    if not code:
        return {"error": "code is required"}

    policy = get_policy_engine()
    violations = policy.check_all({"source_code": code})
    if any(v["action"] == "block" for v in violations):
        return {"success": False, "error": f"Policy: {violations[0]['description']}"}

    result = compile_and_run(code)
    return {
        "success": result.success,
        "output": result.output,
        "gate_reached": result.gate_reached.value if result.gate_reached else "unknown",
        "compile_time_ms": round(result.compile_time_ms, 2),
        "error": result.error,
        "warnings": result.warnings or [],
    }


def handle_forge_build(params: dict) -> dict:
    description = params.get("description", "")
    if not description:
        return {"error": "description is required"}

    artifact_type = params.get("artifact_type", "auto")
    models = params.get("models", ["claude", "gpt", "gemini", "deepseek"])
    extra = params.get("extra_context", "")

    try:
        result = factory_build(
            description=description,
            artifact_type=artifact_type,
            models=models,
            extra_context=extra,
            score=True,
        )
        return {
            "run_id": result.run_id,
            "winner_provider": result.winner.provider_name if result.winner else None,
            "winner_score": round(result.winner.score_total, 2) if result.winner else 0,
            "winner_code": result.winner.source[:8000] if result.winner else "",
            "output_path": str(result.output_path),
            "total_cost": result.total_cost,
            "total_tokens": result.total_tokens,
            "elapsed_s": result.elapsed_s,
            "leaderboard": [
                {
                    "provider": a.provider_name,
                    "score": round(a.score_total, 2),
                    "cost": a.cost_usd,
                    "tokens": a.tokens_used,
                }
                for a in sorted(result.artifacts, key=lambda x: x.score_total, reverse=True)
            ],
        }
    except Exception as e:
        return {"error": str(e)}


def handle_forge_research(params: dict) -> dict:
    query = params.get("query", "")
    models = params.get("models", ["claude", "gpt", "gemini", "deepseek"])

    results = {}
    cost_before = TRACKER.total_cost

    for name in models:
        model_id = MODELS.get(name, name)
        messages = [
            {"role": "system", "content": RESEARCH_SYSTEM},
            {"role": "user", "content": query},
        ]
        try:
            content, usage = call_model(
                model_id, messages, max_tokens=2048, temperature=0.4,
                provider_name=name, purpose="mcp_research",
            )
            results[name] = {
                "response": content,
                "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                "cost": round(usage.get("cost_usd", 0), 6),
            }
        except Exception as e:
            results[name] = {"error": str(e)}

    return {
        "results": results,
        "total_cost": round(TRACKER.total_cost - cost_before, 6),
    }


def handle_forge_consensus(params: dict) -> dict:
    query = params.get("query", "")
    models = params.get("models", ["claude", "gpt", "gemini", "deepseek"])

    individual = {}
    cost_before = TRACKER.total_cost

    for name in models:
        model_id = MODELS.get(name, name)
        messages = [
            {"role": "system", "content": RESEARCH_SYSTEM},
            {"role": "user", "content": query},
        ]
        try:
            content, _ = call_model(
                model_id, messages, max_tokens=2048, temperature=0.4,
                provider_name=name, purpose="mcp_consensus",
            )
            individual[name] = content
        except Exception as e:
            individual[name] = f"[ERROR: {e}]"

    # Synthesize
    synth_prompt = f"Question: {query}\n\n"
    for name, ans in individual.items():
        synth_prompt += f"=== {name.upper()} ===\n{ans[:1500]}\n\n"
    synth_prompt += "Synthesize into one authoritative answer. Note agreements/divergences."

    synth_model = MODELS.get("gemini", "google/gemini-2.5-flash")
    synth_msgs = [
        {"role": "system", "content": "You are a synthesis agent."},
        {"role": "user", "content": synth_prompt},
    ]
    merged, _ = call_model(synth_model, synth_msgs, max_tokens=2048, temperature=0.3,
                                 provider_name="gemini", purpose="mcp_synthesis")

    return {
        "individual": individual,
        "consensus": merged,
        "cost": round(TRACKER.total_cost - cost_before, 6),
    }


def handle_forge_chat(params: dict) -> dict:
    message = params.get("message", "")
    model = params.get("model", "gemini")
    model_id = MODELS.get(model, model)

    messages = [
        {"role": "system", "content": CHAT_SYSTEM},
        {"role": "user", "content": message},
    ]
    content, usage = call_model(
        model_id, messages, max_tokens=2048, temperature=0.5,
        provider_name=model, purpose="mcp_chat",
    )
    return {
        "response": content,
        "model": model,
        "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        "cost": round(usage.get("cost_usd", 0), 6),
        "vendor": usage.get("vendor", "unknown"),
    }


def handle_forge_budget(_: dict) -> dict:
    return {
        "total_cost": round(TRACKER.total_cost, 6),
        "remaining": round(TRACKER.remaining_budget, 4),
        "budget_max": TRACKER.budget.max_budget_usd,
        "pct_used": round(TRACKER.pct_used * 100, 2),
        "total_tokens": TRACKER.total_tokens,
        "request_count": TRACKER._request_count,
        "providers": TRACKER.cost_per_provider(),
        "hints": TRACKER.optimization_hints(),
    }


def handle_forge_market_search(params: dict) -> dict:
    mp = get_marketplace()
    results = mp.search(
        query=params.get("query", ""),
        kind=params.get("kind", ""),
    )
    return {
        "count": len(results),
        "items": [
            {
                "id": e.id, "name": e.name, "kind": e.kind,
                "category": e.category, "fitness": e.fitness,
                "provider": e.provider, "description": e.description[:120],
            }
            for e in results[:20]
        ],
    }


def handle_forge_provenance(params: dict) -> dict:
    limit = params.get("limit", 20)
    valid, count = PROVENANCE.verify_integrity()
    entries = [
        {
            "timestamp": e.timestamp,
            "event_type": e.event_type,
            "entity_id": e.entity_id,
            "actor": e.actor,
            "hash": e.entry_hash[:16],
        }
        for e in PROVENANCE._entries[-limit:]
    ]
    return {
        "total_entries": count,
        "integrity": valid,
        "head": PROVENANCE.head[:16],
        "entries": entries,
    }


def handle_forge_orchestrate(params: dict) -> dict:
    from forge.models import Challenge
    from forge.arena import Arena, ArenaConfig
    from forge.providers import openrouter_provider, ollama_provider
    import json
    
    task = params.get("task", "")
    if not task:
        return {"error": "task is required"}
        
    models = params.get("models", ["qwen-local"])
    
    try:
        prompt = (
            "You are the Master Orchestrator for The Forge. "
            "Output ONLY valid JSON representing a `Challenge` object.\n\n"
            "Format:\n"
            "{\n"
            '  "name": "...",\n'
            '  "description": "...",\n'
            '  "function_signature": "fn solve(...) -> ...",\n'
            '  "difficulty": 5,\n'
            '  "test_cases": [{"call": "solve(...)", "expected": "result"}]\n'
            "}\n\n"
            f"USER TASK: {task}"
        )
        content, _ = call_model(
            MODELS["claude"],
            [{"role": "user", "content": prompt}],
            max_tokens=1500,
            purpose="orchestrate"
        )
        content = content.replace("```json", "").replace("```", "").strip()
        data = json.loads(content)
        challenge = Challenge(
            name=data.get("name", "Generated Challenge"),
            description=data.get("description", ""),
            function_signature=data.get("function_signature", "fn main() -> i64"),
            test_cases=data.get("test_cases", []),
            difficulty=data.get("difficulty", 5)
        )
        
        # Run arena
        arena = Arena(config=ArenaConfig(max_generations=5, population_size=8, verbose=False))
        for m in models:
            provider_fn = ollama_provider(m) if m.endswith("-local") else openrouter_provider(m)
            arena.register_provider(m, provider_fn)
            
        tournament = arena.run(challenge)
        
        return {
            "challenge_name": challenge.name,
            "generations_run": len(tournament.generations),
            "status": tournament.status,
            "champion_source": tournament.champion.source_code if tournament.champion else None,
            "champion_fitness": tournament.champion.fitness.total if tournament.champion and tournament.champion.fitness else 0,
        }
    except Exception as e:
        return {"error": str(e)}

def handle_vitalis_score(params: dict) -> dict:
    code = params.get("code", "")
    language = params.get("language", "auto")
    if not code:
        return {"error": "code is required"}
    try:
        report = fitness_score(code, language)
        return {"success": True, "report": report.certificate}
    except Exception as e:
        return {"error": str(e)}


# ── MCP Dispatch ──────────────────────────────────────────────────────────────

HANDLERS = {
    "forge_compile": handle_forge_compile,
    "forge_build": handle_forge_build,
    "forge_research": handle_forge_research,
    "forge_consensus": handle_forge_consensus,
    "forge_chat": handle_forge_chat,
    "forge_budget": handle_forge_budget,
    "forge_market_search": handle_forge_market_search,
    "forge_provenance": handle_forge_provenance,
    "forge_orchestrate": handle_forge_orchestrate,
    "forge_provider_status": lambda _: get_vendor_status(),
    "vitalis_score": handle_vitalis_score,
}


# ── JSON-RPC 2.0 over stdio ───────────────────────────────────────────────────

def send(obj: dict):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle_request(req: dict) -> dict | None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "the-forge", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        handler = HANDLERS.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }
        try:
            result = handler(tool_args)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    "isError": "error" in result,
                },
            }
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32603, "message": str(e)},
            }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # No response needed for notifications

    return {
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def main():
    print(f"[Forge MCP] Server starting — {len(TOOLS)} tools", file=sys.stderr)
    print(f"[Forge MCP] Vitalis: {vitalis_version()}", file=sys.stderr)
    print(f"[Forge MCP] Models: {list(MODELS.keys())}", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            if resp is not None:
                send(resp)
        except json.JSONDecodeError as e:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": f"Parse error: {e}"}})
        except Exception as e:
            traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    main()
