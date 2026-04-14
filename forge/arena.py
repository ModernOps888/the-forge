"""
The Forge — Arena Orchestrator

The central loop that ties everything together:
1. Distribute challenges to LLM providers
2. Collect .sl submissions
3. Compile, benchmark, and score each one
4. Evolve the next generation
5. Repeat until convergence or max generations
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .models import (
    Challenge, Submission, Generation, Tournament,
    Provider, SubmissionStatus, FitnessScore,
)
from .compiler import compile_and_run, validate_safety
from .benchmark import compute_benchmark, compute_fitness_performance, welch_t_test
from .evolution import (
    EvolutionConfig, evolve_generation, code_distance, code_fingerprint,
    elite_select,
)


# ── Provider Adapters ────────────────────────────────────────────

# Type: a function that takes (challenge_description, function_signature) -> .sl source code
ProviderFn = Callable[[str, str], str]


def mock_provider(name: str) -> ProviderFn:
    """
    Creates a mock provider that returns template solutions.
    Replace with real LLM API calls for production use.
    """
    templates = {
        "claude": '''fn {name}({params}) -> {ret} {{
    // Claude: elegant recursive approach
    if arr.len() < 2 {{ return arr; }}
    let mid: i64 = arr.len() / 2;
    let left: [i64] = {name}(arr.slice(0, mid));
    let right: [i64] = {name}(arr.slice(mid, arr.len()));
    array_merge_sorted(left, right)
}}''',
        "gpt": '''fn {name}({params}) -> {ret} {{
    // GPT: iterative approach with accumulator
    let mut result: [i64] = arr;
    let mut i: i64 = 0;
    while i < result.len() {{
        let mut j: i64 = i + 1;
        while j < result.len() {{
            if result.get(j) < result.get(i) {{
                let tmp: i64 = result.get(i);
                result.set(i, result.get(j));
                result.set(j, tmp);
            }}
            j = j + 1;
        }}
        i = i + 1;
    }}
    result
}}''',
        "gemini": '''fn {name}({params}) -> {ret} {{
    // Gemini: hybrid approach
    if arr.len() < 10 {{ return insertion_sort(arr); }}
    let pivot: i64 = arr.get(arr.len() / 2);
    let mut lo: [i64] = [];
    let mut hi: [i64] = [];
    let mut eq: [i64] = [];
    for x in arr {{
        if x < pivot {{ lo = lo.push(x); }}
        else {{ if x > pivot {{ hi = hi.push(x); }} else {{ eq = eq.push(x); }} }}
    }}
    array_concat(array_concat({name}(lo), eq), {name}(hi))
}}''',
    }

    def generate(description: str, signature: str) -> str:
        # Parse signature to extract function name, params, return type
        import re
        match = re.match(r'fn\s+(\w+)\(([^)]*)\)\s*->\s*(.+)', signature.strip())
        if match:
            fname, params, ret = match.groups()
        else:
            fname, params, ret = "solve", "arr: [i64]", "[i64]"

        template = templates.get(name, templates["claude"])
        return template.format(name=fname, params=params, ret=ret)

    return generate


# ── The Arena ────────────────────────────────────────────────────

@dataclass
class ArenaConfig:
    """Configuration for the arena."""
    max_generations: int = 100
    population_size: int = 50
    elite_count: int = 10
    benchmark_runs: int = 10
    execution_timeout_sec: float = 10.0
    convergence_threshold: float = 0.01  # stop if fitness improves < this for N gens
    convergence_patience: int = 10       # N generations without improvement
    verbose: bool = True


class Arena:
    """
    The multi-agent code evolution arena.
    
    Usage:
        arena = Arena(config=ArenaConfig(max_generations=50))
        arena.register_provider("claude", my_claude_fn)
        arena.register_provider("gpt", my_gpt_fn)
        result = arena.run(challenge)
    """

    def __init__(self, config: Optional[ArenaConfig] = None):
        self.config = config or ArenaConfig()
        self.providers: dict[str, ProviderFn] = {}
        self.evolution_config = EvolutionConfig(
            population_size=self.config.population_size,
            elite_count=self.config.elite_count,
        )

    def register_provider(self, name: str, fn: ProviderFn) -> None:
        """Register an LLM provider function."""
        self.providers[name] = fn

    def run(self, challenge: Challenge) -> Tournament:
        """
        Execute a full evolutionary tournament.
        
        Returns a Tournament object with complete history.
        """
        tournament = Tournament(
            challenge=challenge,
            providers=[Provider(name) for name in self.providers.keys()],
            max_generations=self.config.max_generations,
            population_size=self.config.population_size,
            elite_count=self.config.elite_count,
            status="running",
        )

        if self.config.verbose:
            print(f"\n⚔️  THE FORGE — Tournament Starting")
            print(f"   Challenge: {challenge.name}")
            print(f"   Providers: {', '.join(self.providers.keys())}")
            print(f"   Max Generations: {self.config.max_generations}")
            print(f"   Population Size: {self.config.population_size}")
            print(f"{'─' * 60}")

        # ── Generation 0: Seed from LLM providers ──────────────────
        gen_0 = self._seed_generation(challenge)
        tournament.generations.append(gen_0)
        self._log_generation(gen_0)

        # ── Generations 1-N: Evolve ────────────────────────────────
        stale_count = 0
        best_fitness = gen_0.champion_fitness

        for gen_num in range(1, self.config.max_generations):
            previous = tournament.generations[-1]
            
            # Evolve offspring from previous generation
            offspring = evolve_generation(
                parents=previous.submissions,
                challenge_id=challenge.id,
                generation_number=gen_num,
                config=self.evolution_config,
            )

            # Compile, benchmark, and score all offspring
            generation = self._evaluate_generation(offspring, challenge, gen_num)
            tournament.generations.append(generation)
            self._log_generation(generation)

            # Convergence check
            if generation.champion_fitness > best_fitness + self.config.convergence_threshold:
                best_fitness = generation.champion_fitness
                stale_count = 0
            else:
                stale_count += 1

            if stale_count >= self.config.convergence_patience:
                if self.config.verbose:
                    print(f"\n🏁 Convergence reached after {gen_num} generations")
                tournament.status = "converged"
                break

        if tournament.status != "converged":
            tournament.status = "completed"

        if self.config.verbose:
            self._log_final(tournament)

        return tournament

    def _seed_generation(self, challenge: Challenge) -> Generation:
        """Generate initial submissions from all providers."""
        submissions = []

        for provider_name, provider_fn in self.providers.items():
            try:
                source = provider_fn(challenge.description, challenge.function_signature)
                sub = Submission(
                    challenge_id=challenge.id,
                    generation=0,
                    provider=Provider(provider_name),
                    source_code=source,
                    status=SubmissionStatus.PENDING,
                )
                submissions.append(sub)
            except Exception as e:
                if self.config.verbose:
                    print(f"   ⚠️  {provider_name} failed to generate: {e}")

        return self._evaluate_generation(submissions, challenge, 0)

    def _evaluate_generation(
        self, submissions: list[Submission], challenge: Challenge, gen_num: int,
    ) -> Generation:
        """Compile, benchmark, and score all submissions in a generation."""
        
        for sub in submissions:
            # Safety check
            is_safe, violations = validate_safety(sub.source_code)
            if not is_safe:
                sub.status = SubmissionStatus.FAILED
                continue

            # Compile and run
            result = compile_and_run(sub.source_code, self.config.execution_timeout_sec)
            sub.compilation = result

            if not result.success:
                sub.status = SubmissionStatus.FAILED
                continue

            sub.status = SubmissionStatus.COMPILED

            # Check correctness against test cases
            correctness = self._check_correctness(sub.source_code, challenge)

            # Score code quality (heuristic)
            quality = self._score_quality(sub.source_code)

            # Calculate fitness
            sub.fitness = FitnessScore(
                correctness=correctness,
                performance=0.5,  # relative scoring done below
                code_quality=quality,
                robustness=correctness,  # simplified: robustness ≈ correctness for now
                efficiency=0.5,          # TODO: measure allocations
                novelty=0.5,             # calculated below
            )
            sub.status = SubmissionStatus.SCORED

        # Relative scoring: performance and novelty
        scored = [s for s in submissions if s.fitness is not None]
        if scored:
            # Performance ranking (by compile time as proxy for now)
            compile_times = [s.compilation.compile_time_ms for s in scored if s.compilation]
            for s in scored:
                if s.compilation:
                    perf = compute_fitness_performance(s.compilation.compile_time_ms, compile_times)
                    # Update fitness with relative performance score
                    s.fitness = FitnessScore(
                        correctness=s.fitness.correctness,
                        performance=perf,
                        code_quality=s.fitness.code_quality,
                        robustness=s.fitness.robustness,
                        efficiency=s.fitness.efficiency,
                        novelty=self._score_novelty(s, scored),
                    )

        # Build generation
        ranked = sorted(scored, key=lambda s: s.fitness.total, reverse=True)
        champion = ranked[0] if ranked else None

        return Generation(
            number=gen_num,
            challenge_id=challenge.id,
            submissions=submissions,
            champion_id=champion.id if champion else None,
            champion_fitness=champion.fitness.total if champion else 0.0,
        )

    def _check_correctness(self, source: str, challenge: Challenge) -> float:
        """Check submission against test cases. Returns 0-1 score."""
        if not challenge.test_cases:
            return 1.0  # No test cases = assume correct if it compiles

        passed = 0
        for tc in challenge.test_cases:
            # Wrap the function with a main that calls it with the test input
            test_program = source + f"\n\nfn main() -> i64 {{\n    {tc.get('call', '0')}\n}}"
            result = compile_and_run(test_program, timeout_seconds=5.0)
            
            if result.success:
                expected = str(tc.get("expected", ""))
                if expected in result.output:
                    passed += 1

        return passed / len(challenge.test_cases) if challenge.test_cases else 1.0

    def _score_quality(self, source: str) -> float:
        """Heuristic code quality score (0-1)."""
        lines = source.strip().split("\n")
        body_lines = [l for l in lines if l.strip() and not l.strip().startswith("//")]
        
        score = 1.0

        # Penalize very long functions
        if len(body_lines) > 50:
            score -= 0.2
        elif len(body_lines) > 30:
            score -= 0.1

        # Penalize deeply nested code
        max_indent = max((len(l) - len(l.lstrip()) for l in lines), default=0)
        if max_indent > 20:
            score -= 0.15

        # Reward comments
        comment_lines = sum(1 for l in lines if l.strip().startswith("//"))
        if comment_lines > 0:
            score += 0.05

        return max(0.0, min(1.0, score))

    def _score_novelty(self, candidate: Submission, population: list[Submission]) -> float:
        """Score novelty as average code distance from the rest of the population."""
        if len(population) < 2:
            return 1.0

        distances = [
            code_distance(candidate.source_code, other.source_code)
            for other in population
            if other.id != candidate.id
        ]
        return sum(distances) / len(distances) if distances else 0.5

    def _log_generation(self, gen: Generation) -> None:
        """Log generation results."""
        if not self.config.verbose:
            return

        scored = [s for s in gen.submissions if s.fitness is not None]
        failed = [s for s in gen.submissions if s.status == SubmissionStatus.FAILED]

        champion = None
        for s in gen.submissions:
            if s.id == gen.champion_id:
                champion = s
                break

        print(f"\n   Gen {gen.number:3d} │ "
              f"Scored: {len(scored):3d} │ "
              f"Failed: {len(failed):3d} │ "
              f"Champion: {gen.champion_fitness:6.2f}", end="")

        if champion and champion.provider != Provider.LOCAL:
            print(f" ({champion.provider.value})", end="")
        
        if champion and champion.mutations:
            print(f" [{', '.join(champion.mutations[:2])}]", end="")

        print()

    def _log_final(self, tournament: Tournament) -> None:
        """Log final tournament results."""
        print(f"\n{'═' * 60}")
        print(f"🏆 TOURNAMENT COMPLETE — {tournament.status.upper()}")
        print(f"   Generations: {len(tournament.generations)}")

        champion = tournament.champion
        if champion:
            print(f"   Champion Fitness: {champion.fitness.total:.2f}")
            print(f"   Champion Provider: {champion.provider.value}")
            if champion.parent_a_id:
                print(f"   Ancestry: evolved (parents: {champion.parent_a_id}, {champion.parent_b_id})")
            print(f"\n   Champion Code:")
            for line in champion.source_code.split("\n"):
                print(f"   │ {line}")

        # Provider stats
        print(f"\n   Provider Win Rates (Gen 0 seeding):")
        for gen in tournament.generations:
            if gen.number == 0 and gen.champion_id:
                for s in gen.submissions:
                    status = "👑" if s.id == gen.champion_id else "  "
                    fitness_str = f"{s.fitness.total:.2f}" if s.fitness else "FAIL"
                    print(f"   {status} {s.provider.value:10s} │ {fitness_str}")

        print(f"{'═' * 60}")
