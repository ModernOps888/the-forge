<div align="center">

# 🔥 The Forge

### Multi-Agent Code Evolution Platform

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Vitalis](https://img.shields.io/badge/Powered_by-Vitalis_JIT-b7410e?style=for-the-badge&logo=rust&logoColor=white)](https://github.com/ModernOps888/vitalis)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)

**Multiple LLMs compete to solve coding challenges.<br>
The Vitalis compiler is the impartial judge.<br>
Evolution breeds the winners into code no single model could write.**

<br>

> **`Claude's elegance × GPT's brute force × Gemini's lateral thinking → native compiled code`**

</div>

---

## 💡 The Idea

Every AI coding agent generates code. **None of them evolve it.**

The Forge takes solutions from multiple LLMs, compiles them through a real JIT compiler (Vitalis), benchmarks them with statistical rigor, and then **breeds the winners together** — crossover, mutation, selection — for hundreds of generations.

The result: code that's better than any single model wrote.

```
4 LLMs × 1 challenge × 100 generations = code no single model could write
```

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  7. GOVERNANCE LAYER                        │
│  Safety policies · Capability checks · Kill switch          │
├─────────────────────────────────────────────────────────────┤
│                  6. OBSERVABILITY LAYER                     │
│  Traces · Flame graphs · Drift detection                    │
├─────────────────────────────────────────────────────────────┤
│                  5. EVOLUTION LAYER                         │
│  Crossover · Mutation · Tournament selection · Elitism      │
├─────────────────────────────────────────────────────────────┤
│                  4. BENCHMARK LAYER                         │
│  Welford stats · Confidence intervals · Welch's t-test      │
├─────────────────────────────────────────────────────────────┤
│                  3. COMPILER LAYER (Vitalis)                │
│  Lex → Parse → Type-check → JIT → Execute (7 gates)        │
├─────────────────────────────────────────────────────────────┤
│                  2. ORCHESTRATION LAYER                     │
│  Multi-LLM dispatch · Challenge distribution                │
├─────────────────────────────────────────────────────────────┤
│                  1. DATA LAYER                              │
│  Evolution ledger · Tool registry · Benchmark archive       │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| **Python** | 3.12+ | Orchestration engine |
| **Rust** | nightly / 1.85+ | Vitalis compiler |
| **Vitalis** | Cloned at `C:\Vitalis-V60` | JIT compilation backend |

### Install & Run

```bash
# Clone The Forge
git clone https://github.com/ModernOps888/the-forge.git
cd the-forge

# Run a demo tournament (mock providers, no API keys needed)
python forge_cli.py demo --generations 5 --population 10

# Compile a .sl snippet through Vitalis
python forge_cli.py compile "fn main() -> i64 { 42 }"

# Type-check without executing
python forge_cli.py check "fn main() -> i64 { 42 }"

# Run a full tournament from a challenge file
python forge_cli.py challenge challenges/sort_integers.json --generations 20
```

### Demo Output

```
╔══════════════════════════════════════════════════════════════╗
║   THE FORGE — Multi-Agent Code Evolution Platform            ║
║   Powered by Vitalis JIT Compiler                            ║
╚══════════════════════════════════════════════════════════════╝

⚔️  THE FORGE — Tournament Starting
   Challenge: Sort Integers
   Providers: claude, gpt, gemini
   Max Generations: 5
   Population Size: 10
────────────────────────────────────────────────────────────

   Gen   0 │ Scored:   3 │ Failed:   0 │ Champion:  87.20 (claude)
   Gen   1 │ Scored:  10 │ Failed:   0 │ Champion:  89.15 [crossover_uniform]
   Gen   2 │ Scored:  10 │ Failed:   1 │ Champion:  91.73 [crossover, insert_guard]
   Gen   3 │ Scored:  10 │ Failed:   0 │ Champion:  92.40 [elite_passthrough]
   Gen   4 │ Scored:  10 │ Failed:   2 │ Champion:  93.11 [crossover, swap_lines]

════════════════════════════════════════════════════════════
🏆 TOURNAMENT COMPLETE — CONVERGED
   Champion Fitness: 93.11
   Ancestry: Claude (gen 0) × GPT (gen 0) → evolved gen 4
════════════════════════════════════════════════════════════
```

---

## 📁 Project Structure

```
TheForge/
├── forge/
│   ├── __init__.py           # Package exports
│   ├── models.py             # Data models (Challenge, Submission, Score, etc.)
│   ├── compiler.py           # Vitalis JIT integration (7-gate pipeline)
│   ├── benchmark.py          # Welford stats, t-tests, outlier detection
│   ├── evolution.py          # Crossover, mutation, selection operators
│   └── arena.py              # Tournament orchestrator
├── challenges/
│   └── sort_integers.json    # Example challenge definition
├── tools/                    # Evolved champion code lands here
├── forge_cli.py              # CLI entry point
├── requirements.txt          # Zero dependencies (stdlib only)
└── README.md
```

---

## 🧬 How Evolution Works

### Generation 0: Seed
Each LLM provider generates a solution to the challenge independently.

### Generations 1–N: Evolve

| Operator | What It Does |
|---|---|
| **Elite selection** | Top N individuals pass through unchanged |
| **Tournament selection** | Pick K random, keep the fittest as parent |
| **Uniform crossover** | For each line, randomly pick from parent A or B |
| **Single-point crossover** | Swap tails at a random body line |
| **Mutate: swap lines** | Swap two adjacent lines in the function body |
| **Mutate: insert guard** | Add an early-return guard clause |
| **Mutate: change constant** | Randomly alter a numeric literal |
| **Novelty scoring** | Reward solutions that are different from the herd |

### The 6 Fitness Dimensions

| Dimension | Weight | Source |
|---|---|---|
| **Correctness** | 30% | Test case pass rate |
| **Performance** | 25% | Relative execution speed |
| **Code Quality** | 15% | Complexity, structure, comments |
| **Robustness** | 15% | Fuzz testing survival |
| **Efficiency** | 10% | Memory/allocation metrics |
| **Novelty** | 5% | Code distance from population |

---

## 🔒 The 7 Compiler Gates

Every submission passes through 7 gates before being scored:

```
Source → LEX → PARSE → TYPE CHECK → LINT → CAPABILITY → COMPILE → EXECUTE
          ↓      ↓         ↓          ↓        ↓          ↓         ↓
        Reject  Reject   Reject    Score    Reject     Reject    Score
```

If any gate fails, the submission is eliminated. The compiler is the impartial judge — it doesn't care which LLM wrote the code.

---

## 🐍 Python SDK

```python
from forge import Arena, ArenaConfig, Challenge

# Define a challenge
challenge = Challenge(
    name="Fibonacci",
    description="Compute the nth Fibonacci number",
    function_signature="fn fib(n: i64) -> i64",
)

# Create arena
arena = Arena(config=ArenaConfig(max_generations=50))
arena.register_provider("claude", my_claude_api)
arena.register_provider("gpt", my_gpt_api)

# Run tournament
tournament = arena.run(challenge)

# Get the champion
print(f"Winner: {tournament.champion.fitness.total}")
print(tournament.champion.source_code)
```

---

## 🔧 Wiring Real LLM Providers

Replace mock providers with real API calls:

```python
import anthropic

client = anthropic.Anthropic()

def claude_provider(description: str, signature: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"Write a Vitalis .sl function. {description}\n"
                       f"Signature: {signature}\n"
                       f"Return ONLY the function code, no explanation."
        }]
    )
    return response.content[0].text

arena.register_provider("claude", claude_provider)
```

---

## 📊 Statistical Rigor

The Forge doesn't use vibes. Every comparison uses proper statistics:

- **Welford's algorithm** for numerically stable online mean/variance
- **95% confidence intervals** via Student's t distribution
- **Welch's t-test** for comparing two candidates
- **Cohen's d** effect size measurement
- **MAD-based outlier detection** (modified Z-score)
- **Minimum 30 runs** before declaring significance

---

## 🛡️ Safety

| Rule | Enforcement |
|---|---|
| No file system access | Compile-time capability check |
| No network access | Compile-time capability check |
| No infinite loops | Execution timeout (configurable) |
| No regression | Champion must beat current best by >2σ |
| Kill switch | `tournament.status = "halted"` freezes everything |

---

## 🗺 Roadmap

- [x] Core engine (compiler, benchmark, evolution, arena)
- [x] CLI with demo mode
- [x] Vitalis JIT integration
- [ ] Real LLM provider adapters (Claude, GPT, Gemini)
- [ ] SQLite evolution ledger
- [ ] Web dashboard (real-time tournament visualization)
- [ ] MCP server (agents discover evolved tools)
- [ ] AgentLens integration (full observability)
- [ ] AST-level crossover (not line-level)
- [ ] Property-based fuzz testing via Vitalis

---

<div align="center">

**Built with 🔥 by [ModernOps888](https://github.com/ModernOps888)**

*Where code is forged through competition, refined by evolution, and hardened by compilation.*

</div>
