#!/usr/bin/env python3
"""
The Forge CLI — Multi-Agent Code Evolution Platform

Usage:
    python forge_cli.py arena                          # Real LLMs via OpenRouter
    python forge_cli.py chat                           # Interactive multi-agent chat
    python forge_cli.py research "topic"               # Multi-agent research
    python forge_cli.py consensus "question"           # All models + synthesis
    python forge_cli.py budget                         # Cost & token report
    python forge_cli.py compile "fn main() -> i64 { 42 }"
    python forge_cli.py test-providers                 # Smoke-test each LLM
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from forge import Arena, ArenaConfig, Challenge, compile_and_run, type_check, lex
from forge.arena import mock_provider
from forge.providers import openrouter_provider, ollama_provider, MODELS, test_provider, chat, multi_agent_research, consensus, auto_route, call_openrouter
from forge.budget import get_tracker, BudgetConfig
from forge.factory import build as factory_build, ARTIFACT_TYPES
from forge.marketplace import get_marketplace


BANNER = r"""
==============================================================
   THE FORGE - Multi-Agent Code Evolution Platform
==============================================================
"""


# ═══════════════════════════════════════════════════════════════════════════════
# ARENA — Real LLM tournament via OpenRouter
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_arena(args):
    """Run a real tournament with actual LLM providers via OpenRouter."""
    print(BANNER)

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("❌ OPENROUTER_API_KEY not set. Run:")
        print('   $env:OPENROUTER_API_KEY = "sk-or-v1-..."')
        sys.exit(1)

    print(f"🔥 LIVE TOURNAMENT — Real LLMs via OpenRouter")
    print(f"   Available models: {', '.join(MODELS.keys())}\n")

    # Parse models
    model_names = [m.strip() for m in args.models.split(",")]
    for m in model_names:
        if m not in MODELS:
            print(f"⚠️  Unknown model '{m}', available: {list(MODELS.keys())}")
            sys.exit(1)

    challenge = _load_or_default_challenge(args)

    arena = Arena(config=ArenaConfig(
        max_generations=args.generations,
        population_size=args.population,
        elite_count=max(2, args.population // 5),
        benchmark_runs=5,
        verbose=True,
    ))

    # Register REAL providers
    for name in model_names:
        print(f"   Registering: {name} → {MODELS[name]}")
        provider_fn = ollama_provider(name) if name.endswith("-local") else openrouter_provider(name)
        arena.register_provider(name, provider_fn)

    print()
    start = time.time()
    tournament = arena.run(challenge)
    elapsed = time.time() - start

    print(f"\n⏱️  Total time: {elapsed:.1f}s")
    print(f"📊 Generations: {len(tournament.generations)}")
    print(f"🏆 Status: {tournament.status}")

    _save_results(tournament, challenge)


# ═══════════════════════════════════════════════════════════════════════════════
# DEMO — Offline mock tournament
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_demo(args):
    """Run a demo tournament with mock providers (no API keys needed)."""
    print(BANNER)
    print("🔥 Demo tournament (mock providers, offline)...\n")

    challenge = Challenge(
        name="Sort Integers",
        description="Write a function that sorts an array of integers in ascending order",
        function_signature="fn sort(arr: [i64]) -> [i64]",
        test_cases=[],
        constraints={"time_ms": 100},
        difficulty=3,
    )

    arena = Arena(config=ArenaConfig(
        max_generations=args.generations,
        population_size=args.population,
        elite_count=max(2, args.population // 5),
        verbose=True,
    ))

    for name in ["claude", "gpt", "gemini"]:
        arena.register_provider(name, mock_provider(name))

    start = time.time()
    tournament = arena.run(challenge)
    elapsed = time.time() - start

    print(f"\n⏱️  Total time: {elapsed:.1f}s")
    print(f"📊 Generations: {len(tournament.generations)}")
    print(f"🏆 Status: {tournament.status}")
    _save_results(tournament, challenge)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST-PROVIDERS — Smoke-test each LLM
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_test_providers(args):
    """Smoke-test: ask each model to write a simple function, then compile it."""
    print(BANNER)
    print("🧪 Testing real LLM providers via OpenRouter...\n")

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("❌ OPENROUTER_API_KEY not set")
        sys.exit(1)

    model_names = [m.strip() for m in args.models.split(",")]

    for name in model_names:
        model_id = MODELS.get(name, name)
        print(f"{'─' * 60}")
        print(f"  🤖 {name} ({model_id})")

        try:
            provider = ollama_provider(name) if name.endswith("-local") else openrouter_provider(name)
            t0 = time.time()
            source = provider(
                "Write a function that adds two integers",
                "fn add(a: i64, b: i64) -> i64",
            )
            api_time = time.time() - t0
            print(f"  ⏱️  API response: {api_time:.1f}s")
            print(f"  📝 Source ({len(source)} chars):")
            for line in source.split("\n")[:10]:
                print(f"      {line}")
            if source.count("\n") > 10:
                print(f"      ... ({source.count(chr(10)) - 10} more lines)")

            # Compile it
            result = compile_and_run(source)
            if result.success:
                print(f"  ✅ Compiled! Output: {result.output} ({result.compile_time_ms:.0f}ms)")
            else:
                print(f"  ❌ Failed at gate: {result.gate_reached.value}")
                print(f"     Error: {result.error[:150]}")

        except Exception as e:
            print(f"  ❌ Error: {e}")

        print()

    print(f"{'─' * 60}")
    print("Done.\n")


# ═══════════════════════════════════════════════════════════════════════════════
# COMPILE / CHECK
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_compile(args):
    if Path(args.source).exists():
        source = Path(args.source).read_text(encoding="utf-8")
    else:
        source = args.source

    print(f"🔨 Compiling via Vitalis...")
    result = compile_and_run(source)
    if result.success:
        print(f"✅ Success (gate: {result.gate_reached.value}, {result.compile_time_ms:.0f}ms)")
        print(f"   Output: {result.output}")
    else:
        print(f"❌ Failed at gate: {result.gate_reached.value}")
        print(f"   Error: {result.error}")
    for w in (result.warnings or []):
        print(f"   ⚠️  {w}")


def cmd_check(args):
    if Path(args.source).exists():
        source = Path(args.source).read_text(encoding="utf-8")
    else:
        source = args.source
    result = type_check(source)
    print(f"{'✅ OK' if result.success else '❌ Fail'} ({result.compile_time_ms:.0f}ms)")
    if result.error:
        print(f"   {result.error}")


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _load_or_default_challenge(args) -> Challenge:
    if hasattr(args, 'challenge') and args.challenge:
        path = Path(args.challenge)
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return Challenge(
                name=data["name"],
                description=data["description"],
                function_signature=data["function_signature"],
                test_cases=data.get("test_cases", []),
                constraints=data.get("constraints", {}),
                difficulty=data.get("difficulty", 5),
            )
    # Default challenge
    return Challenge(
        name="Add Two Numbers",
        description="Write a function that adds two 64-bit integers and returns the result",
        function_signature="fn add(a: i64, b: i64) -> i64",
        test_cases=[],
        constraints={"time_ms": 50},
        difficulty=1,
    )


def _save_results(tournament, challenge):
    if tournament.champion:
        tools_dir = Path("tools")
        tools_dir.mkdir(exist_ok=True)
        slug = challenge.name.lower().replace(" ", "_")

        # Save champion source
        champ_path = tools_dir / f"champion_{slug}.sl"
        champ_path.write_text(tournament.champion.source_code, encoding="utf-8")
        print(f"\n💾 Champion code → {champ_path}")

        # Save tournament summary
        summary = {
            "challenge": challenge.name,
            "generations": len(tournament.generations),
            "status": tournament.status,
            "champion_fitness": tournament.champion.fitness.total if tournament.champion and tournament.champion.fitness else 0,
            "champion_provider": tournament.champion.provider.value if tournament.champion else None,
            "champion_source": tournament.champion.source_code if tournament.champion else "",
        }
        results_path = tools_dir / f"tournament_{slug}.json"
        results_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"💾 Results     → {results_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# CHAT — Interactive multi-agent conversation
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_chat(args):
    """Interactive chat with any LLM, cost-tracked."""
    print(BANNER)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("❌ OPENROUTER_API_KEY not set"); sys.exit(1)

    model = args.model
    tracker = get_tracker(BudgetConfig(max_budget_usd=args.budget))
    print(f"💬 Chat mode — model: {model} ({MODELS.get(model, model)})")
    print(f"💰 Budget: ${args.budget:.2f}  │  Type 'quit' to exit, 'switch <model>' to change")
    print(f"   Models: {', '.join(MODELS.keys())}")
    print(f"{'─' * 60}\n")

    history = []
    while True:
        try:
            user_input = input(f"  You ({model}) › ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if user_input.lower().startswith("switch "):
            new_model = user_input.split(" ", 1)[1].strip()
            if new_model in MODELS:
                model = new_model
                print(f"  ✅ Switched to {model}")
            else:
                print(f"  ❌ Unknown model. Available: {list(MODELS.keys())}")
            continue
        if user_input.lower() == "budget":
            print(tracker.summary())
            continue
        if user_input.lower() == "cost":
            print(f"  💰 Spent: ${tracker.total_cost:.4f} / ${tracker.budget.max_budget_usd:.2f}")
            continue

        history.append({"role": "user", "content": user_input})
        try:
            response = chat(user_input, model_name=model, history=history[:-1])
            history.append({"role": "assistant", "content": response})
            print(f"\n  🤖 {model}:")
            for line in response.split("\n"):
                print(f"     {line}")
            print(f"     ─── ${tracker.total_cost:.4f} spent ───\n")
        except Exception as e:
            print(f"  ❌ {e}")

    print(f"\n{tracker.summary()}")


# ═══════════════════════════════════════════════════════════════════════════════
# RESEARCH — Fan query to all models
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_research(args):
    """Send a research query to multiple models."""
    print(BANNER)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("❌ OPENROUTER_API_KEY not set"); sys.exit(1)

    tracker = get_tracker(BudgetConfig(max_budget_usd=args.budget))
    model_names = [m.strip() for m in args.models.split(",")]

    print(f"🔬 Multi-Agent Research")
    print(f"   Query: {args.query}")
    print(f"   Models: {', '.join(model_names)}")
    print(f"{'─' * 60}\n")

    results = multi_agent_research(args.query, model_names)

    for name, response in results.items():
        print(f"\n{'═' * 60}")
        print(f"  🤖 {name.upper()} ({MODELS.get(name, name)})")
        print(f"{'─' * 60}")
        for line in response.split("\n"):
            print(f"  {line}")

    print(f"\n{tracker.summary()}")


# ═══════════════════════════════════════════════════════════════════════════════
# CONSENSUS — All models + synthesis
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_consensus(args):
    """Ask all models, then synthesize into one answer."""
    print(BANNER)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("❌ OPENROUTER_API_KEY not set"); sys.exit(1)

    tracker = get_tracker(BudgetConfig(max_budget_usd=args.budget))
    model_names = [m.strip() for m in args.models.split(",")]

    print(f"🧠 Multi-Agent Consensus")
    print(f"   Query: {args.query}")
    print(f"   Models: {', '.join(model_names)}")
    print(f"{'─' * 60}")

    result = consensus(args.query, model_names)

    for name, response in result["individual"].items():
        print(f"\n  ── {name.upper()} ──")
        for line in response.split("\n")[:8]:
            print(f"     {line}")
        if response.count("\n") > 8:
            print(f"     ... ({response.count(chr(10)) - 8} more lines)")

    print(f"\n{'═' * 60}")
    print(f"  🏆 SYNTHESIZED CONSENSUS")
    print(f"{'─' * 60}")
    for line in result["consensus"].split("\n"):
        print(f"  {line}")

    print(f"\n  💰 Research cost: ${result['cost_usd']:.4f}")
    print(f"\n{tracker.summary()}")


# ═══════════════════════════════════════════════════════════════════════════════
# BUDGET — Show cost report
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_budget(args):
    """Show cost and token report."""
    tracker = get_tracker()
    if tracker.total_tokens == 0:
        print("No API calls recorded in this session.")
    else:
        print(tracker.summary())


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD — Agent Factory
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_build(args):
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    result = factory_build(
        description=args.description,
        artifact_type=args.type,
        models=models,
        extra_context=getattr(args, "context", ""),
        score=not getattr(args, "no_score", False),
    )
    if result.winner:
        print(f"\n  Winner source saved to: {result.output_path}")
        print(f"\n  --- WINNER CODE (truncated) ---")
        src = result.winner.source
        lines = src.split("\n")
        print("\n".join(lines[:60]))
        if len(lines) > 60:
            print(f"  ... ({len(lines)-60} more lines) — see {result.output_path}")

    # Print budget summary
    tracker = get_tracker()
    print(f"\n  💰 Factory cost: ${result.total_cost:.4f} | Tokens: {result.total_tokens:,}")
    print(f"  💼 Session total: ${tracker.total_cost:.4f} remaining ${tracker.remaining_budget:.2f}")


# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATE — Master LLM Planner -> Local SLM Solver
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_orchestrate(args):
    """Use a Master LLM to design a challenge, then let local SLMs solve it."""
    print(BANNER)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("❌ OPENROUTER_API_KEY not set for orchestrator")
        sys.exit(1)

    print(f"🧠 Master Orchestrator: Planning challenge...")
    print(f"   Task: {args.task}")
    print(f"   Generating challenge via Claude...")

    prompt = (
        "You are the Master Orchestrator for The Forge. "
        "The user will describe a coding task to solve in the Vitalis `.sl` language. "
        "You must output ONLY valid JSON representing a `Challenge` object. "
        "Do NOT output markdown fences, ONLY JSON.\n\n"
        "Format:\n"
        "{\n"
        '  "name": "Challenge Name",\n'
        '  "description": "Full problem description",\n'
        '  "function_signature": "fn solve(...) -> ...",\n'
        '  "difficulty": 5,\n'
        '  "test_cases": [{"call": "solve(...)", "expected": "result"}]\n'
        "}\n\n"
        f"USER TASK: {args.task}"
    )

    try:
        content, _ = call_openrouter(
            MODELS["claude"],
            [{"role": "user", "content": prompt}],
            max_tokens=1500,
            purpose="orchestrate"
        )
        content = content.replace("```json", "").replace("```", "").strip()
        data = json.loads(content)
        # Verify JSON
        challenge = Challenge(
            name=data.get("name", "Generated Challenge"),
            description=data.get("description", ""),
            function_signature=data.get("function_signature", "fn main() -> i64"),
            test_cases=data.get("test_cases", []),
            difficulty=data.get("difficulty", 5)
        )
        print(f"✅ Challenge created: {challenge.name}")
        print(f"   Running Arena with local SLMs...\n")
    except Exception as e:
        print(f"❌ Failed to orchestrate challenge: {e}")
        return

    arena = Arena(config=ArenaConfig(
        max_generations=args.generations,
        population_size=args.population,
        verbose=True,
    ))

    # Master orchestrator deploys SLM workers (default: qwen-local)
    model_names = [m.strip() for m in args.models.split(",")]
    for name in model_names:
        provider_fn = ollama_provider(name) if name.endswith("-local") else openrouter_provider(name)
        arena.register_provider(name, provider_fn)

    tournament = arena.run(challenge)
    _save_results(tournament, challenge)


# ═══════════════════════════════════════════════════════════════════════════════
# MARKET — Marketplace
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_market(args):
    mp = get_marketplace()
    cmd = getattr(args, "market_cmd", None)

    if cmd == "search" or cmd is None:
        query = getattr(args, "query", "")
        kind = getattr(args, "kind", "")
        results = mp.search(query=query, kind=kind)
        if not results:
            print("  No items found.")
            return
        print(f"\n  Marketplace — {len(results)} result(s):\n")
        for e in results:
            fitness_str = f"fitness:{e.fitness:.1f}" if e.fitness > 0 else ""
            print(f"  [{e.kind:8s}] {e.id:20s} | {e.name:30s} | {fitness_str:12s} | {e.description[:50]}")

    elif cmd == "list":
        items = mp.list_all()
        print(f"\n  Marketplace — {len(items)} items:\n")
        for e in items:
            print(f"  [{e.kind:8s}] {e.id:20s}  {e.name}")
        print(f"\n  {mp.summary()}")

    elif cmd == "compare":
        print(mp.compare(args.id_a, args.id_b))

    else:
        print("  Usage: forge market [search|list|compare] ...")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(prog="forge", description="The Forge — Multi-Agent Code Evolution Platform")
    sub = parser.add_subparsers(dest="command", help="Commands")

    # arena (real LLMs)
    p_arena = sub.add_parser("arena", help="Run tournament with REAL LLMs via OpenRouter")
    p_arena.add_argument("--models", "-m", default="claude,gpt,gemini,deepseek",
                         help="Comma-separated model names")
    p_arena.add_argument("--challenge", "-c", default=None, help="Path to challenge JSON")
    p_arena.add_argument("--generations", "-g", type=int, default=5, help="Max generations")
    p_arena.add_argument("--population", "-p", type=int, default=8, help="Population size")

    # chat (interactive)
    p_chat = sub.add_parser("chat", help="Interactive multi-agent chat")
    p_chat.add_argument("--model", "-m", default="gemini", help="Model (default: gemini — cheapest)")
    p_chat.add_argument("--budget", "-b", type=float, default=1.00, help="Budget cap in USD")

    # research (fan-out)
    p_research = sub.add_parser("research", help="Fan query to all models")
    p_research.add_argument("query", help="Research question")
    p_research.add_argument("--models", "-m", default="claude,gpt,gemini,deepseek")
    p_research.add_argument("--budget", "-b", type=float, default=2.00)

    # consensus (synthesis)
    p_consensus = sub.add_parser("consensus", help="All models answer + synthesize")
    p_consensus.add_argument("query", help="Question to reach consensus on")
    p_consensus.add_argument("--models", "-m", default="claude,gpt,gemini,deepseek")
    p_consensus.add_argument("--budget", "-b", type=float, default=2.00)

    # budget report
    sub.add_parser("budget", help="Show cost and token report")

    # demo (mock)
    p_demo = sub.add_parser("demo", help="Run demo with mock providers (offline)")
    p_demo.add_argument("--generations", "-g", type=int, default=3, help="Max generations")
    p_demo.add_argument("--population", "-p", type=int, default=6, help="Population size")

    # test-providers
    p_test = sub.add_parser("test-providers", help="Smoke-test LLM providers")
    p_test.add_argument("--models", "-m", default="deepseek")

    # compile
    p_compile = sub.add_parser("compile", help="Compile and run .sl code")
    p_compile.add_argument("source", help="Source code or path to .sl file")

    # check
    p_check = sub.add_parser("check", help="Type-check .sl code")
    p_check.add_argument("source", help="Source code or path to .sl file")

    # build (Agent Factory)
    p_build = sub.add_parser("build", help="🏭 Agent Factory: build any artifact from description")
    p_build.add_argument("description", help="What to build (natural language)")
    p_build.add_argument("--type", "-t", default="auto",
                         choices=list(ARTIFACT_TYPES.keys()) + ["auto"],
                         help="Artifact type (default: auto-detect)")
    p_build.add_argument("--models", "-m", default="claude,gpt,gemini,deepseek",
                         help="Comma-separated model list")
    p_build.add_argument("--context", "-c", default="",
                         help="Additional context or constraints")
    p_build.add_argument("--no-score", action="store_true",
                         help="Skip LLM scoring (faster, cheaper)")

    # orchestrate
    p_orchestrate = sub.add_parser("orchestrate", help="Master Planner dictates challenge, SLMs solve")
    p_orchestrate.add_argument("task", help="Natural language description of the coding task")
    p_orchestrate.add_argument("--models", "-m", default="qwen-local", help="Worker models (comma-separated)")
    p_orchestrate.add_argument("--generations", "-g", type=int, default=5, help="Max generations")
    p_orchestrate.add_argument("--population", "-p", type=int, default=8, help="Population size")

    # market
    p_market = sub.add_parser("market", help="🏪 Skill & Artifact Marketplace")
    market_sub = p_market.add_subparsers(dest="market_cmd")
    ms_search = market_sub.add_parser("search", help="Search marketplace")
    ms_search.add_argument("query", nargs="?", default="", help="Search term")
    ms_search.add_argument("--kind", choices=["skill", "artifact", ""], default="")
    market_sub.add_parser("list", help="List all marketplace items")
    ms_compare = market_sub.add_parser("compare", help="Compare two items")
    ms_compare.add_argument("id_a")
    ms_compare.add_argument("id_b")

    args = parser.parse_args()
    dispatch = {
        "arena": cmd_arena, "chat": cmd_chat, "research": cmd_research,
        "consensus": cmd_consensus, "budget": cmd_budget, "demo": cmd_demo,
        "test-providers": cmd_test_providers, "compile": cmd_compile,
        "check": cmd_check, "build": cmd_build, "market": cmd_market,
        "orchestrate": cmd_orchestrate,
    }
    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
