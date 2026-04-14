#!/usr/bin/env python3
"""
The Forge CLI — Multi-Agent Code Evolution Platform

Usage:
    python forge_cli.py challenge "sort integers" --providers claude,gpt,gemini --generations 50
    python forge_cli.py compile "fn main() -> i64 { 42 }"
    python forge_cli.py demo
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from forge import (
    Arena, ArenaConfig, Challenge,
    compile_and_run, type_check, lex,
)
from forge.arena import mock_provider


BANNER = r"""
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║   ██████╗ ██╗  ██╗███████╗    ███████╗ ██████╗ ██████╗  ██╗ ║
║   ╚═══██╗██║  ██║██╔════╝    ██╔════╝██╔═══██╗██╔══██╗██╔╝ ║
║    ╔══██║███████║█████╗      █████╗  ██║   ██║██████╔╝██║  ║
║    ║  ██║██╔══██║██╔══╝      ██╔══╝  ██║   ██║██╔══██╗██║  ║
║    ╚████║██║  ██║███████╗    ██║     ╚██████╔╝██║  ██║╚██╗ ║
║     ╚═══╝╚═╝  ╚═╝╚══════╝    ╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═╝║
║                                                              ║
║   Multi-Agent Code Evolution Platform                        ║
║   Powered by Vitalis JIT Compiler                            ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""


def cmd_demo(args):
    """Run a demo tournament with mock providers."""
    print(BANNER)
    print("🔥 Running demo tournament with mock LLM providers...\n")

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

    # Register mock providers
    for provider_name in ["claude", "gpt", "gemini"]:
        arena.register_provider(provider_name, mock_provider(provider_name))

    start = time.time()
    tournament = arena.run(challenge)
    elapsed = time.time() - start

    print(f"\n⏱️  Total time: {elapsed:.1f}s")
    print(f"📊 Generations: {len(tournament.generations)}")
    print(f"🏆 Status: {tournament.status}")

    if tournament.champion:
        print(f"\n💾 Champion saved to: tools/champion_{challenge.name.lower().replace(' ', '_')}.sl")
        # Save champion
        tools_dir = Path("tools")
        tools_dir.mkdir(exist_ok=True)
        champ_path = tools_dir / f"champion_{challenge.name.lower().replace(' ', '_')}.sl"
        champ_path.write_text(tournament.champion.source_code, encoding="utf-8")


def cmd_compile(args):
    """Compile and run a .sl snippet or file."""
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

    if result.warnings:
        for w in result.warnings:
            print(f"   ⚠️  {w}")


def cmd_check(args):
    """Type-check a .sl snippet without executing."""
    if Path(args.source).exists():
        source = Path(args.source).read_text(encoding="utf-8")
    else:
        source = args.source

    result = type_check(source)
    status = "✅ Types OK" if result.success else "❌ Type Error"
    print(f"{status} ({result.compile_time_ms:.0f}ms)")
    if result.error:
        print(f"   {result.error}")


def cmd_challenge(args):
    """Run a tournament from a challenge JSON file."""
    print(BANNER)

    challenge_path = Path(args.file)
    if not challenge_path.exists():
        print(f"❌ Challenge file not found: {challenge_path}")
        sys.exit(1)

    with open(challenge_path, encoding="utf-8") as f:
        data = json.load(f)

    challenge = Challenge(
        name=data["name"],
        description=data["description"],
        function_signature=data["function_signature"],
        test_cases=data.get("test_cases", []),
        constraints=data.get("constraints", {}),
        difficulty=data.get("difficulty", 5),
    )

    arena = Arena(config=ArenaConfig(
        max_generations=args.generations,
        population_size=args.population,
        verbose=True,
    ))

    # Register providers
    for name in args.providers.split(","):
        name = name.strip()
        # TODO: replace mock_provider with real LLM API calls
        arena.register_provider(name, mock_provider(name))

    tournament = arena.run(challenge)

    # Save results
    results_path = Path("tools") / f"tournament_{challenge.name.lower().replace(' ', '_')}.json"
    results_path.parent.mkdir(exist_ok=True)
    
    summary = {
        "challenge": challenge.name,
        "generations": len(tournament.generations),
        "status": tournament.status,
        "champion_fitness": tournament.champion.fitness.total if tournament.champion else 0,
        "champion_provider": tournament.champion.provider.value if tournament.champion else None,
    }
    results_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n💾 Results saved to: {results_path}")


def main():
    parser = argparse.ArgumentParser(
        prog="forge",
        description="The Forge — Multi-Agent Code Evolution Platform",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # demo
    p_demo = sub.add_parser("demo", help="Run a demo tournament with mock LLM providers")
    p_demo.add_argument("--generations", "-g", type=int, default=5, help="Max generations (default: 5)")
    p_demo.add_argument("--population", "-p", type=int, default=10, help="Population size (default: 10)")

    # compile
    p_compile = sub.add_parser("compile", help="Compile and run a .sl snippet")
    p_compile.add_argument("source", help="Source code string or path to .sl file")

    # check
    p_check = sub.add_parser("check", help="Type-check a .sl snippet")
    p_check.add_argument("source", help="Source code string or path to .sl file")

    # challenge
    p_challenge = sub.add_parser("challenge", help="Run a tournament from a challenge file")
    p_challenge.add_argument("file", help="Path to challenge JSON file")
    p_challenge.add_argument("--providers", default="claude,gpt,gemini", help="Comma-separated provider names")
    p_challenge.add_argument("--generations", "-g", type=int, default=20, help="Max generations")
    p_challenge.add_argument("--population", "-p", type=int, default=20, help="Population size")

    args = parser.parse_args()

    if args.command == "demo":
        cmd_demo(args)
    elif args.command == "compile":
        cmd_compile(args)
    elif args.command == "check":
        cmd_check(args)
    elif args.command == "challenge":
        cmd_challenge(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
