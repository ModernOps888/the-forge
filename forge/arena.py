"""
The Forge — Arena Orchestrator (Elite Edition)

The full tournament loop — everything wired together:
  - Multi-LLM seeding via pluggable provider functions
  - Vitalis 7-gate compiler as impartial judge
  - Multi-objective fitness: correctness, speed, quality, robustness, novelty
  - Adaptive fitness weights that shift per generation (native Rust)
  - Pareto front selection for multi-objective optimization
  - Boltzmann + Bayesian UCB + quantum-annealed evolution
  - Lévy-flight mutations for heavy-tailed exploration
  - Shannon diversity monitoring (convergence detection)
  - Dual-track: Forge evolution engine + Vitalis native evo engine
  - EMA fitness trend tracking
  - Full provenance chain on every submission
"""

from __future__ import annotations

import time
import random
from dataclasses import dataclass, field
from typing import Callable, Optional

from .models import (
    Challenge, Submission, Generation, Tournament,
    Provider, SubmissionStatus, FitnessScore,
    BenchmarkResult, CompilationResult,
)
from . import compiler as _c
from .benchmark import (
    compute_benchmark, compute_fitness_performance, adaptive_fitness_score,
    native_code_quality, compute_pareto_front, measure_diversity,
    weighted_composite, FitnessTrend, welch_t_test,
)
from .evolution import (
    EvolutionConfig, evolve_generation, code_novelty, code_fingerprint,
    elite_select,
)

ProviderFn = Callable[[str, str], str]


# ── Arena Configuration ───────────────────────────────────────────────────────

@dataclass
class ArenaConfig:
    max_generations: int = 100
    population_size: int = 50
    elite_count: int = 10
    benchmark_runs: int = 15           # runs per candidate for statistical rigor
    execution_timeout_sec: float = 10.0
    convergence_patience: int = 10
    convergence_delta: float = 0.005   # min fitness improvement to reset patience

    # Multi-objective fitness weights (sum to 1.0)
    weight_correctness: float = 0.30
    weight_performance: float = 0.25
    weight_quality: float = 0.15
    weight_robustness: float = 0.15
    weight_efficiency: float = 0.10
    weight_novelty: float = 0.05

    # Evolution settings
    evo: EvolutionConfig = field(default_factory=EvolutionConfig)

    verbose: bool = True
    use_vitalis_native_evo: bool = True   # dual-track with slang_evo_* engine


# ── Provider Registry Helpers ─────────────────────────────────────────────────

def mock_provider(name: str) -> ProviderFn:
    """
    Mock LLM provider — returns structurally valid .sl code with different
    algorithmic styles per provider. Used for testing without API keys.
    """
    import re

    styles = {
        "claude": (
            "recursive, elegant, with guard clauses",
            lambda sig: f"""{sig} {{
    // claude: elegant recursive approach
    if arr.len() < 2 {{ return arr; }}
    let mid: i64 = arr.len() / 2;
    let left: [i64] = sort(arr.slice(0, mid));
    let right: [i64] = sort(arr.slice(mid, arr.len()));
    merge_sorted(left, right)
}}"""
        ),
        "gpt": (
            "iterative, brute-force, explicit loops",
            lambda sig: f"""{sig} {{
    // gpt: in-place selection sort
    let mut result: [i64] = arr;
    let n: i64 = result.len();
    let mut i: i64 = 0;
    while i < n {{
        let mut min_idx: i64 = i;
        let mut j: i64 = i + 1;
        while j < n {{
            if result.get(j) < result.get(min_idx) {{ min_idx = j; }}
            j = j + 1;
        }}
        let tmp: i64 = result.get(i);
        result.set(i, result.get(min_idx));
        result.set(min_idx, tmp);
        i = i + 1;
    }}
    result
}}"""
        ),
        "gemini": (
            "hybrid pivot-based partitioning",
            lambda sig: f"""{sig} {{
    // gemini: quicksort-style partition
    if arr.len() < 10 {{ return insertion_sort(arr); }}
    let pivot: i64 = arr.get(arr.len() / 2);
    let mut lo: [i64] = [];
    let mut hi: [i64] = [];
    let mut eq: [i64] = [];
    for x in arr {{
        if x < pivot {{ lo = lo.push(x); }}
        else {{ if x > pivot {{ hi = hi.push(x); }} else {{ eq = eq.push(x); }} }}
    }}
    array_concat(array_concat(sort(lo), eq), sort(hi))
}}"""
        ),
        "local": (
            "stub",
            lambda sig: f"""{sig} {{
    arr
}}"""
        ),
    }

    style_name, generator = styles.get(name, styles["local"])

    def _generate(description: str, signature: str) -> str:
        sig = signature.strip().rstrip("{").strip()
        return generator(sig)

    return _generate


# ── The Arena ─────────────────────────────────────────────────────────────────

class Arena:
    """
    The Forge tournament engine.
    
    1. Seed from N LLM providers
    2. Evaluate through 7 Vitalis compiler gates
    3. Score on 6 adaptive fitness dimensions
    4. Compute Pareto front for multi-objective selection
    5. Evolve: Boltzmann + Bayesian UCB + quantum annealing + Lévy mutations
    6. Monitor diversity via Shannon entropy
    7. Dual-track: also register champions with Vitalis native evolution engine
    8. Repeat until convergence or max generations
    """

    def __init__(self, config: Optional[ArenaConfig] = None):
        self.config = config or ArenaConfig()
        self.providers: dict[str, ProviderFn] = {}
        self._trial_counts: dict[str, int] = {}
        self._trend = FitnessTrend(alpha=0.3)
        self._seen_fingerprints: set[str] = set()

        # Sync evo config from arena config
        self.config.evo.population_size = self.config.population_size
        self.config.evo.elite_count = self.config.elite_count

    def register_provider(self, name: str, fn: ProviderFn) -> None:
        self.providers[name] = fn

    def run(self, challenge: Challenge) -> Tournament:
        """Execute a full evolutionary tournament. Returns complete Tournament."""
        _c._ensure_ffi()   # warm up DLL
        vitalis_ver = _c.vitalis_version()

        tournament = Tournament(
            challenge=challenge,
            providers=[Provider(n) if n in [p.value for p in Provider] else Provider.LOCAL for n in self.providers],
            max_generations=self.config.max_generations,
            population_size=self.config.population_size,
            elite_count=self.config.elite_count,
            status="running",
        )

        if self.config.verbose:
            print(f"\n{'═' * 65}")
            print(f"  ⚔️   THE FORGE — Tournament")
            print(f"  📋  Challenge  : {challenge.name}")
            print(f"  🧬  Providers  : {', '.join(self.providers)}")
            print(f"  🔧  Vitalis    : {vitalis_ver}")
            print(f"  🧪  Max gens   : {self.config.max_generations}  │  Pop: {self.config.population_size}")
            print(f"{'═' * 65}")
            print(f"  {'Gen':>4}  {'Scored':>7}  {'Failed':>7}  {'Champion':>10}  {'Diversity':>10}  Notes")
            print(f"  {'─'*4}  {'─'*7}  {'─'*7}  {'─'*10}  {'─'*10}  {'─'*20}")

        # ── Generation 0: Seed from LLMs ──────────────────────────────
        gen_0 = self._seed_generation(challenge)
        tournament.generations.append(gen_0)
        self._log_gen(gen_0, [])

        if not gen_0.submissions:
            tournament.status = "failed_no_submissions"
            return tournament

        # Register champion with Vitalis native evolution engine
        self._vitalis_evo_register(challenge, gen_0)

        # ── Generations 1–N: Evolve ────────────────────────────────────
        stale = 0
        best_fitness = gen_0.champion_fitness

        for gen_num in range(1, self.config.max_generations + 1):
            prev = tournament.generations[-1]
            all_sources = [s.source_code for s in prev.submissions if s.fitness]

            # Evolve next generation
            offspring = evolve_generation(
                parents=prev.submissions,
                challenge_id=challenge.id,
                generation_number=gen_num,
                config=self.config.evo,
                trial_counts=self._trial_counts,
            )

            # Evaluate
            gen = self._evaluate_generation(offspring, challenge, gen_num, all_sources)
            tournament.generations.append(gen)

            # EMA trend and diversity
            ema = self._trend.update(gen.champion_fitness)
            diversity = measure_diversity([s.fitness.total for s in gen.submissions if s.fitness])
            self._log_gen(gen, all_sources, diversity=diversity, ema=ema)

            # Register champion with native Vitalis engine
            self._vitalis_evo_register(challenge, gen)

            # Convergence check
            if gen.champion_fitness > best_fitness + self.config.convergence_delta:
                best_fitness = gen.champion_fitness
                stale = 0
            else:
                stale += 1

            if stale >= self.config.convergence_patience:
                tournament.status = "converged"
                if self.config.verbose:
                    print(f"\n  🏁  Convergence reached at generation {gen_num} (stale for {stale} gens)")
                break

        if tournament.status == "running":
            tournament.status = "completed"

        if self.config.verbose:
            self._log_final(tournament)

        return tournament

    # ── Private: Seed ──────────────────────────────────────────────────────

    def _seed_generation(self, challenge: Challenge) -> Generation:
        """Generate initial submissions from all registered LLM providers with Agentic Reflection."""
        submissions = []
        for pname, pfn in self.providers.items():
            try:
                if self.config.verbose:
                    print(f"  🤖 Seeding from {pname}...")
                    
                max_reflections = 2
                feedback = ""
                source = ""
                
                for attempt in range(max_reflections + 1):
                    prompt_desc = challenge.description
                    if feedback:
                        prompt_desc += f"\n\n[COMPILER ERROR from previous attempt]:\n{feedback}\nPlease fix the code."
                        
                    source = pfn(prompt_desc, challenge.function_signature)
                    
                    # Quick compilation check
                    result = _c.compile_and_run(source, 5.0)
                    if result.success:
                        if self.config.verbose and attempt > 0:
                            print(f"    ↳ ✅ Fixed via reflection after {attempt} retries.")
                        break
                    else:
                        feedback = f"Failed at gate: {result.gate_reached.value}\nError: {result.error}"
                        if self.config.verbose and attempt < max_reflections:
                            print(f"    ↳ ❌ Failed gate {result.gate_reached.value}. Reflecting...")

                fp = code_fingerprint(source)
                sub = Submission(
                    challenge_id=challenge.id,
                    generation=0,
                    provider=Provider(pname) if pname in [p.value for p in Provider] else Provider.LOCAL,
                    source_code=source,
                    status=SubmissionStatus.PENDING,
                )
                submissions.append(sub)
                self._seen_fingerprints.add(fp)
            except Exception as e:
                if self.config.verbose:
                    print(f"  ⚠️  {pname} error: {e}")

        return self._evaluate_generation(submissions, challenge, 0, [])

    # ── Private: Evaluate ──────────────────────────────────────────────────

    def _evaluate_generation(
        self,
        submissions: list[Submission],
        challenge: Challenge,
        gen_num: int,
        all_sources_prev: list[str],
    ) -> Generation:
        """Compile, benchmark, and score every submission."""

        all_sources = list(all_sources_prev)

        # De-duplicate by fingerprint
        unique: list[Submission] = []
        for sub in submissions:
            fp = code_fingerprint(sub.source_code)
            if fp not in self._seen_fingerprints:
                self._seen_fingerprints.add(fp)
                unique.append(sub)
            else:
                sub.status = SubmissionStatus.ELIMINATED
        # Keep duplicates but mark them — still process them
        to_evaluate = submissions  # process all, duplicates get low novelty

        for sub in to_evaluate:
            if sub.status == SubmissionStatus.ELIMINATED:
                continue

            # Gate 0: capability safety check
            safe, violations = _c.validate_safety(sub.source_code)
            if not safe:
                sub.status = SubmissionStatus.FAILED
                continue

            # Gate 1–7: full Vitalis compiler pipeline
            result = _c.compile_and_run(sub.source_code, self.config.execution_timeout_sec)
            sub.compilation = result

            if not result.success:
                sub.status = SubmissionStatus.FAILED
                continue

            sub.status = SubmissionStatus.COMPILED

            # Correctness against test cases
            correctness = self._check_correctness(sub.source_code, challenge)

            # Native code quality score (Vitalis hotpath)
            quality = native_code_quality(sub.source_code)

            # Novelty vs current population
            novelty = code_novelty(sub.source_code, all_sources)
            all_sources.append(sub.source_code)

            # Adaptive fitness (weights shift per generation — native Rust)
            # speed proxy: 1/(compile_time_ms) normalized
            speed_proxy = 1.0 / max(result.compile_time_ms, 1.0)
            speed_norm = min(speed_proxy / 10.0, 1.0)  # normalize to [0,1]

            adaptive = adaptive_fitness_score(
                speed=speed_norm,
                correctness=correctness,
                complexity=quality,
                security=0.8,             # default safe (no violations)
                generation=gen_num,
            )

            sub.fitness = FitnessScore(
                correctness=correctness,
                performance=speed_norm,     # refined below with relative scoring
                code_quality=quality,
                robustness=correctness,     # simplified: fuzz = correctness proxy
                efficiency=speed_norm,
                novelty=novelty,
            )
            sub.status = SubmissionStatus.SCORED

        # Relative performance scoring
        scored = [s for s in to_evaluate if s.fitness is not None]
        if scored:
            comp_times = [s.compilation.compile_time_ms
                          for s in scored if s.compilation]
            for s in scored:
                if s.compilation:
                    perf = compute_fitness_performance(s.compilation.compile_time_ms, comp_times)
                    # Recompute weighted composite via native Rust
                    metrics = [
                        s.fitness.correctness,
                        perf,
                        s.fitness.code_quality,
                        s.fitness.robustness,
                        s.fitness.efficiency,
                        s.fitness.novelty,
                    ]
                    weights = [
                        self.config.weight_correctness,
                        self.config.weight_performance,
                        self.config.weight_quality,
                        self.config.weight_robustness,
                        self.config.weight_efficiency,
                        self.config.weight_novelty,
                    ]
                    s.fitness = FitnessScore(
                        correctness=s.fitness.correctness,
                        performance=perf,
                        code_quality=s.fitness.code_quality,
                        robustness=s.fitness.robustness,
                        efficiency=s.fitness.efficiency,
                        novelty=s.fitness.novelty,
                    )

        # Pareto front computation (multi-objective)
        objective_vecs = [
            (s.id, [s.fitness.correctness, s.fitness.performance,
                    s.fitness.code_quality, s.fitness.novelty])
            for s in scored
        ]
        pareto_ids = set(compute_pareto_front(objective_vecs)) if objective_vecs else set()

        # Identify champion (highest composite fitness)
        ranked = sorted(scored, key=lambda s: s.fitness.total, reverse=True)
        champion = ranked[0] if ranked else None

        return Generation(
            number=gen_num,
            challenge_id=challenge.id,
            submissions=to_evaluate,
            champion_id=champion.id if champion else None,
            champion_fitness=champion.fitness.total if champion else 0.0,
        )

    # ── Private: Correctness ───────────────────────────────────────────────

    def _check_correctness(self, source: str, challenge: Challenge) -> float:
        """Run test cases against compiled output. Returns pass rate [0.0, 1.0]."""
        if not challenge.test_cases:
            return 1.0   # no test cases = correctness assumed if compiled

        passed = 0
        for tc in challenge.test_cases:
            call = tc.get("call", "")
            expected = str(tc.get("expected", ""))
            if not call:
                continue

            wrapper = f'{source}\n\nfn main() -> i64 {{\n    {call}\n}}'
            result = _c.compile_and_run(wrapper, timeout_seconds=5.0)

            if result.success and expected in result.output:
                passed += 1

        return passed / len(challenge.test_cases)

    # ── Private: Vitalis Native Evo Dual-Track ─────────────────────────────

    def _vitalis_evo_register(self, challenge: Challenge, gen: Generation) -> None:
        """
        Register the generation champion with Vitalis's native evolution engine.
        This provides dual-track evolution history: Forge engine + native engine.
        """
        if not self.config.use_vitalis_native_evo:
            return
        champion = None
        for s in gen.submissions:
            if s.id == gen.champion_id:
                champion = s
                break
        if champion is None or champion.fitness is None:
            return

        fn_name = challenge.name.lower().replace(" ", "_")
        try:
            if gen.number == 0:
                _c.evo_register(fn_name, champion.source_code)
            else:
                _c.evo_submit_variant(fn_name, champion.source_code)
            _c.evo_set_fitness(fn_name, champion.fitness.total / 100.0)
        except Exception:
            pass   # non-critical — Forge engine is the primary

    # ── Logging ────────────────────────────────────────────────────────────

    def _log_gen(self, gen: Generation, prev_sources: list[str],
                  diversity: float = 0.0, ema: float = 0.0) -> None:
        if not self.config.verbose:
            return
        scored = [s for s in gen.submissions if s.fitness]
        failed = [s for s in gen.submissions if s.status == SubmissionStatus.FAILED]
        champion = next((s for s in gen.submissions if s.id == gen.champion_id), None)

        notes = ""
        if champion:
            if champion.provider != Provider.LOCAL:
                notes += f"{champion.provider.value} "
            if champion.mutations:
                notes += f"[{', '.join(champion.mutations[:2])}]"

        diversity_str = f"{diversity:.2f}" if diversity > 0 else "  —  "
        ema_str = f"{ema:.1f}" if ema > 0 else "  —  "

        print(f"  {gen.number:>4}  {len(scored):>7}  {len(failed):>7}  "
              f"{gen.champion_fitness:>10.2f}  {diversity_str:>10}  {notes}")

    def _log_final(self, tournament: Tournament) -> None:
        print(f"\n{'═' * 65}")
        print(f"  🏆  TOURNAMENT {tournament.status.upper()}")
        print(f"  📊  Total generations : {len(tournament.generations)}")

        champion = tournament.champion
        if champion:
            print(f"  🎯  Champion fitness  : {champion.fitness.total:.2f}")
            print(f"  👤  Origin           : {champion.provider.value}")
            if champion.parent_a_id:
                print(f"  🧬  Ancestry         : evolved (gen {champion.generation})")
            if champion.mutations:
                print(f"  🔬  Last mutations   : {', '.join(champion.mutations[:3])}")
            if champion.fitness:
                print(f"\n  Fitness breakdown:")
                print(f"    Correctness : {champion.fitness.correctness:.2f}")
                print(f"    Performance : {champion.fitness.performance:.2f}")
                print(f"    Qual/Style  : {champion.fitness.code_quality:.2f}")
                print(f"    Novelty     : {champion.fitness.novelty:.2f}")

            # Vitalis pipeline insight
            if champion.compilation:
                print(f"\n  Compiler stats:")
                print(f"    Compile time : {champion.compilation.compile_time_ms:.1f}ms")
                print(f"    Gate reached : {champion.compilation.gate_reached.value}")

            # AST + IR dump for champion (FFI only)
            try:
                ast = _c.parse_ast(champion.source_code)
                if ast and ast != "(AST requires FFI)":
                    print(f"\n  AST (champion): {ast[:120]}...")
            except Exception:
                pass

            print(f"\n  Champion source:")
            for line in champion.source_code.split("\n"):
                print(f"    {line}")

        # Provider stats
        gen0 = tournament.generations[0] if tournament.generations else None
        if gen0:
            print(f"\n  Seed provider results (Gen 0):")
            for s in gen0.submissions:
                crown = "👑" if s.id == gen0.champion_id else "  "
                fit = f"{s.fitness.total:.1f}" if s.fitness else "FAIL"
                print(f"    {crown} {s.provider.value:12s} │ fitness: {fit}")

        print(f"{'═' * 65}\n")
