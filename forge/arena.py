"""
The Forge — Arena Orchestrator (Elite Edition)

The full tournament loop — everything wired together:
  - Multi-LLM seeding via pluggable provider functions
  - Vitalis 7-gate compiler as impartial judge
  - Multi-objective fitness: correctness, speed, quality, robustness, novelty
  - Adaptive fitness weights that shift per generation (native Rust or Python math)
  - Pareto front selection for multi-objective optimization
  - Boltzmann + Bayesian UCB + quantum-annealed evolution
  - Lévy-flight mutations for heavy-tailed exploration
  - Shannon diversity monitoring (convergence detection)
  - Dual-track: Forge evolution engine + Vitalis native evo engine
  - EMA fitness trend tracking
  - Full provenance chain on every submission
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .models import (
    Challenge, Submission, Generation, Tournament,
    Provider, SubmissionStatus, FitnessScore,
    CompilationGate, CompilationResult,
)
from . import compiler as _c
from .benchmark import (
    compute_benchmark, compute_fitness_performance, adaptive_fitness_score,
    native_code_quality, compute_pareto_front, measure_diversity,
    FitnessTrend, kls_spectral_diagnostics,
)
from .evolution import (
    EvolutionConfig, StochasticLocalizationState, evolve_generation,
    code_novelty, code_fingerprint, _ngram_vector,
)
from .governance import get_provenance

ProviderFn = Callable[[str, str], str]
DirectorHookFn = Callable[[Generation, dict], Optional[list[str]]]


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

    # KLS Stochastic Localization & Multi-Trajectory Pruning (MSR Song & Zhang 2026)
    enable_trajectory_pruning: bool = True
    trajectory_prune_patience: int = 2
    trajectory_prune_gap: float = 12.0   # fitness gap below champion to flag dead-end lineage
    director_hook: Optional[DirectorHookFn] = None

    verbose: bool = True
    use_vitalis_native_evo: bool = True   # dual-track with slang_evo_* engine


    def weights_dict(self) -> dict[str, float]:
        return {
            "correctness": self.weight_correctness,
            "performance": self.weight_performance,
            "code_quality": self.weight_quality,
            "robustness": self.weight_robustness,
            "efficiency": self.weight_efficiency,
            "novelty": self.weight_novelty,
        }


# ── Provider Registry Helpers ─────────────────────────────────────────────────

def mock_provider(name: str) -> ProviderFn:
    """
    Mock LLM provider — returns structurally valid, self-contained .sl code
    with different algorithmic styles per provider. Adapts to array or scalar signatures.
    """
    def _parse_sig(sig: str) -> tuple[str, list[tuple[str, str]], str]:
        m = re.search(r"\bfn\s+([a-zA-Z_]\w*)\s*\(([^)]*)\)\s*(?:->\s*([^{]+))?", sig)
        if not m:
            return "solve", [("n", "i64")], "i64"
        fn_name = m.group(1)
        raw_params = m.group(2).strip()
        ret_type = (m.group(3) or "i64").strip()
        params: list[tuple[str, str]] = []
        if raw_params:
            for p in raw_params.split(","):
                p = p.strip()
                if not p:
                    continue
                if ":" in p:
                    pn, pt = p.split(":", 1)
                    params.append((pn.replace("mut ", "").strip(), pt.strip()))
                else:
                    params.append((p.replace("mut ", "").strip(), "i64"))
        return fn_name, params, ret_type

    def _generate(description: str, signature: str) -> str:
        sig = signature.strip().rstrip("{").strip()
        fn_name, params, ret_type = _parse_sig(sig)
        first_param = params[0][0] if params else "arr"
        first_type = params[0][1] if params else "[i64]"
        desc_lower = (description or "").lower()

        # Array sorting / array processing challenge
        if "[" in first_type or "[" in ret_type:
            if name.startswith("claude"):
                return f"""{sig} {{
    // claude: recursive merge sort
    if {first_param}.len() < 2 {{ return {first_param}; }}
    let mid: i64 = {first_param}.len() / 2;
    let left: [i64] = {fn_name}({first_param}.slice(0, mid));
    let right: [i64] = {fn_name}({first_param}.slice(mid, {first_param}.len()));
    merge_sorted(left, right)
}}"""
            if name.startswith("gpt") or name.startswith("o3"):
                return f"""{sig} {{
    // gpt: iterative in-place selection sort
    let mut result: [i64] = {first_param};
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
            if name.startswith("gemini"):
                return f"""{sig} {{
    // gemini: hybrid pivot-based quicksort
    if {first_param}.len() < 2 {{ return {first_param}; }}
    let pivot: i64 = {first_param}.get({first_param}.len() / 2);
    let mut lo: [i64] = [];
    let mut hi: [i64] = [];
    let mut eq: [i64] = [];
    for x in {first_param} {{
        if x < pivot {{ lo = lo.push(x); }}
        else {{ if x > pivot {{ hi = hi.push(x); }} else {{ eq = eq.push(x); }} }}
    }}
    array_concat(array_concat({fn_name}(lo), eq), {fn_name}(hi))
}}"""
            if name.startswith("deepseek"):
                return f"""{sig} {{
    // deepseek: insertion sort
    insertion_sort({first_param})
}}"""
            return f"""{sig} {{
    insertion_sort({first_param})
}}"""

        # Fibonacci challenge
        if "fib" in fn_name.lower() or "fibonacci" in desc_lower:
            return f"""{sig} {{
    if {first_param} <= 0 {{ return 0; }}
    if {first_param} == 1 {{ return 1; }}
    let mut a: i64 = 0;
    let mut b: i64 = 1;
    let mut i: i64 = 2;
    while i <= {first_param} {{
        let next: i64 = a + b;
        a = b;
        b = next;
        i = i + 1;
    }}
    b
}}"""

        # Two-parameter scalar challenge (e.g. fn add(a: i64, b: i64) -> i64)
        if len(params) >= 2:
            p0, p1 = params[0][0], params[1][0]
            return f"""{sig} {{
    {p0} + {p1}
}}"""

        # Default single-parameter scalar challenge
        return f"""{sig} {{
    if {first_param} < 0 {{ return 0; }}
    {first_param}
}}"""

    return _generate


# ── The Arena ─────────────────────────────────────────────────────────────────

class Arena:
    """
    The Forge tournament engine.
    """

    def __init__(self, config: Optional[ArenaConfig] = None):
        self.config = config or ArenaConfig()
        self.providers: dict[str, ProviderFn] = {}
        self._trial_counts: dict[str, int] = {}
        self._trend = FitnessTrend(alpha=0.3)
        self._seen_fingerprints: set[str] = set()
        self._localization_state = StochasticLocalizationState(dim=64)
        self._lineage_stagnation: dict[str, int] = {}
        self.pruned_trajectories: int = 0
        self.kls_history: list[dict] = []

        # Sync evo config from arena config
        self.config.evo.population_size = self.config.population_size
        self.config.evo.elite_count = self.config.elite_count

    def register_provider(self, name: str, fn: ProviderFn) -> None:
        self.providers[name] = fn

    def _compute_generation_kls(self, gen: Generation) -> dict:
        """Compute KLS isoperimetric & Poincaré spectral diagnostics for a generation."""
        scored = [s for s in gen.submissions if s.fitness is not None]
        if len(scored) < 2:
            diag = kls_spectral_diagnostics([], [])
        else:
            vecs = [_ngram_vector(s.source_code, dim=64) for s in scored]
            fits = [s.fitness.total for s in scored]
            diag = kls_spectral_diagnostics(vecs, fits)
        self.kls_history.append(diag)
        return diag

    def _prune_dead_end_trajectories(
        self,
        gen: Generation,
        kls_diag: dict,
        tournament_id: str = "",
    ) -> list[Submission]:
        """
        MSR Song-Zhang (2026) Multi-Trajectory Dead-End Pruning + Human Director Steering:
        - Prunes lineages that lag the generation champion by >= trajectory_prune_gap
          for >= trajectory_prune_patience consecutive generations (always preserving elites).
        - Applies optional director_hook(gen, kls_diag) to allow human/external steering.
        """
        scored = [s for s in gen.submissions if s.fitness is not None]
        if len(scored) <= max(2, self.config.elite_count):
            return gen.submissions

        # Optional human-director steering hook (returns allowed submission IDs if steering)
        if self.config.director_hook is not None:
            try:
                keep_ids = self.config.director_hook(gen, kls_diag)
                if keep_ids:
                    keep_set = set(keep_ids)
                    steered = [s for s in gen.submissions if s.id in keep_set]
                    if steered:
                        self.pruned_trajectories += len(gen.submissions) - len(steered)
                        return steered
            except Exception:
                pass

        if not self.config.enable_trajectory_pruning:
            return gen.submissions

        champ_fit = gen.champion_fitness
        ranked = sorted(scored, key=lambda s: s.fitness.total, reverse=True)
        elite_ids = {s.id for s in ranked[: max(1, self.config.elite_count)]}

        surviving: list[Submission] = []
        pruned_ids: list[str] = []

        for s in gen.submissions:
            if s.fitness is None:
                pruned_ids.append(s.id)
                continue
            if s.id in elite_ids:
                self._lineage_stagnation[s.id] = 0
                surviving.append(s)
                continue

            parent_key = s.parent_a_id or s.id
            gap = champ_fit - s.fitness.total
            if gap >= self.config.trajectory_prune_gap:
                stag = self._lineage_stagnation.get(parent_key, 0) + 1
                self._lineage_stagnation[s.id] = stag
                if stag >= self.config.trajectory_prune_patience:
                    pruned_ids.append(s.id)
                    continue
            else:
                self._lineage_stagnation[s.id] = 0
            surviving.append(s)

        if not surviving:
            return ranked[: max(1, self.config.elite_count)]

        if pruned_ids:
            self.pruned_trajectories += len(pruned_ids)
            if tournament_id:
                get_provenance().record(
                    "trajectory_pruned",
                    tournament_id,
                    "arena",
                    {
                        "generation": gen.number,
                        "pruned_count": len(pruned_ids),
                        "surviving_count": len(surviving),
                        "poincare_ratio": kls_diag.get("poincare_ratio", 0.0),
                    },
                )
        return surviving

    def run(self, challenge: Challenge) -> Tournament:
        """Execute a full evolutionary tournament. Returns complete Tournament."""
        _c._ensure_ffi()   # warm up DLL if available
        vitalis_ver = _c.vitalis_version()
        prov_chain = get_provenance()

        tournament = Tournament(
            challenge=challenge,
            providers=[Provider.from_name(n) for n in self.providers],
            max_generations=self.config.max_generations,
            population_size=self.config.population_size,
            elite_count=self.config.elite_count,
            status="running",
        )

        prov_chain.record(
            "tournament_start",
            tournament.id,
            "arena",
            {
                "challenge_id": challenge.id,
                "challenge_name": challenge.name,
                "providers": list(self.providers.keys()),
                "max_generations": self.config.max_generations,
            },
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
        gen_0 = self._seed_generation(challenge, tournament.id)
        tournament.generations.append(gen_0)
        kls_0 = self._compute_generation_kls(gen_0)
        self._log_gen(gen_0, [])

        if not gen_0.submissions:
            tournament.status = "failed_no_submissions"
            return tournament

        self._vitalis_evo_register(challenge, gen_0)

        # ── Generations 1–N: Evolve ────────────────────────────────────
        stale = 0
        best_fitness = gen_0.champion_fitness
        active_parents = self._prune_dead_end_trajectories(gen_0, kls_0, tournament.id)

        for gen_num in range(1, self.config.max_generations + 1):
            prev = tournament.generations[-1]
            all_sources = [s.source_code for s in prev.submissions if s.fitness]

            offspring = evolve_generation(
                parents=active_parents,
                challenge_id=challenge.id,
                generation_number=gen_num,
                config=self.config.evo,
                trial_counts=self._trial_counts,
                localization_state=self._localization_state,
            )

            gen = self._evaluate_generation(offspring, challenge, gen_num, all_sources, tournament.id)
            tournament.generations.append(gen)
            kls_diag = self._compute_generation_kls(gen)
            active_parents = self._prune_dead_end_trajectories(gen, kls_diag, tournament.id)

            ema = self._trend.update(gen.champion_fitness)
            diversity = measure_diversity([s.fitness.total for s in gen.submissions if s.fitness])
            self._log_gen(gen, all_sources, diversity=diversity, ema=ema)

            self._vitalis_evo_register(challenge, gen)


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

        if tournament.champion and tournament.champion.fitness:
            prov_chain.record(
                "promotion",
                tournament.id,
                "arena",
                {
                    "champion_id": tournament.champion.id,
                    "provider": tournament.champion.provider.value,
                    "fitness": round(tournament.champion.fitness.total, 2),
                    "generations": len(tournament.generations),
                    "status": tournament.status,
                },
            )

        if self.config.verbose:
            self._log_final(tournament)

        return tournament

    # ── Private: Seed ──────────────────────────────────────────────────────

    def _seed_generation(self, challenge: Challenge, tournament_id: str = "") -> Generation:
        """Generate initial submissions from all registered LLM providers with Agentic Reflection."""
        submissions: list[Submission] = []
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

                    result = _c.compile_and_run(source, 5.0)
                    if result.success:
                        if self.config.verbose and attempt > 0:
                            print(f"    ↳ ✅ Fixed via reflection after {attempt} retries.")
                        break
                    else:
                        feedback = f"Failed at gate: {result.gate_reached.value}\nError: {result.error}"
                        if self.config.verbose and attempt < max_reflections:
                            print(f"    ↳ ❌ Failed gate {result.gate_reached.value}. Reflecting...")

                sub = Submission(
                    challenge_id=challenge.id,
                    generation=0,
                    provider=Provider.from_name(pname),
                    source_code=source,
                    status=SubmissionStatus.PENDING,
                )
                submissions.append(sub)
            except Exception as e:
                if self.config.verbose:
                    print(f"  ⚠️  {pname} error: {e}")

        return self._evaluate_generation(submissions, challenge, 0, [], tournament_id)

    # ── Private: Evaluate ──────────────────────────────────────────────────

    def _evaluate_generation(
        self,
        submissions: list[Submission],
        challenge: Challenge,
        gen_num: int,
        all_sources_prev: list[str],
        tournament_id: str = "",
    ) -> Generation:
        """Compile, benchmark, and score every submission."""
        all_sources = list(all_sources_prev)
        weights_map = self.config.weights_dict()

        for sub in submissions:
            fp = code_fingerprint(sub.source_code)
            is_duplicate = fp in self._seen_fingerprints
            self._seen_fingerprints.add(fp)

            # Gate 0: capability safety check
            safe, violations = _c.validate_safety(sub.source_code)
            if not safe:
                sub.compilation = CompilationResult(
                    success=False,
                    gate_reached=CompilationGate.CAPABILITY,
                    error="; ".join(violations),
                )
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

            # Novelty vs current population (elites keep small floor; non-elite duplicates penalized)
            raw_novelty = code_novelty(sub.source_code, all_sources)
            if is_duplicate and "elite" not in sub.mutations:
                novelty = min(raw_novelty, 0.05)
            else:
                novelty = raw_novelty
            all_sources.append(sub.source_code)

            speed_norm = min(1.0, 5.0 / max(result.compile_time_ms, 5.0))

            adaptive = adaptive_fitness_score(
                speed=speed_norm,
                correctness=correctness,
                complexity=quality,
                security=0.9,
                generation=gen_num,
            )

            sub.fitness = FitnessScore(
                correctness=correctness,
                performance=speed_norm,
                code_quality=quality,
                robustness=max(correctness, adaptive),
                efficiency=speed_norm,
                novelty=novelty,
                WEIGHTS=weights_map,
            )
            sub.status = SubmissionStatus.SCORED

        # Relative performance scoring & benchmark attachment for top candidates
        scored = [s for s in submissions if s.fitness is not None]
        if scored:
            comp_times = [
                s.compilation.compile_time_ms
                for s in scored if s.compilation is not None
            ]
            for s in scored:
                if s.compilation is not None:
                    perf = compute_fitness_performance(s.compilation.compile_time_ms, comp_times)
                    s.fitness = FitnessScore(
                        correctness=s.fitness.correctness,
                        performance=perf,
                        code_quality=s.fitness.code_quality,
                        robustness=s.fitness.robustness,
                        efficiency=s.fitness.efficiency,
                        novelty=s.fitness.novelty,
                        WEIGHTS=weights_map,
                    )

        # Pareto front computation (multi-objective)
        objective_vecs = [
            (s.id, [s.fitness.correctness, s.fitness.performance,
                    s.fitness.code_quality, s.fitness.novelty])
            for s in scored if s.fitness is not None
        ]
        pareto_ids = set(compute_pareto_front(objective_vecs)) if objective_vecs else set()

        # Identify champion (highest composite fitness)
        ranked = sorted(scored, key=lambda s: s.fitness.total, reverse=True)
        champion = ranked[0] if ranked else None
        if champion is not None and champion.id in pareto_ids:
            champion.status = SubmissionStatus.PROMOTED
            if champion.compilation is not None and champion.benchmark is None:
                champion.benchmark = compute_benchmark([champion.compilation.compile_time_ms] * max(2, min(5, self.config.benchmark_runs)))

        if tournament_id and champion is not None and champion.fitness is not None:
            get_provenance().record(
                "compilation",
                tournament_id,
                champion.provider.value,
                {
                    "generation": gen_num,
                    "champion_id": champion.id,
                    "fitness": round(champion.fitness.total, 2),
                    "pareto_front_size": len(pareto_ids),
                },
            )

        return Generation(
            number=gen_num,
            challenge_id=challenge.id,
            submissions=submissions,
            champion_id=champion.id if champion else None,
            champion_fitness=champion.fitness.total if (champion and champion.fitness) else 0.0,
        )

    # ── Private: Correctness ───────────────────────────────────────────────

    @staticmethod
    def _strip_existing_main(source: str) -> str:
        """Remove any existing `fn main() ... { ... }` block so test wrapper doesn't collide."""
        m = re.search(r"\bfn\s+main\s*\([^)]*\)\s*(?:->\s*[^{]+)?\s*\{", source)
        if not m:
            return source
        start = m.start()
        depth = 1
        i = m.end()
        while i < len(source) and depth > 0:
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
            i += 1
        return (source[:start] + source[i:]).strip()

    @staticmethod
    def _infer_return_type(signature: str) -> str:
        m = re.search(r"->\s*([^{]+)", signature or "")
        return m.group(1).strip() if m else "i64"

    @staticmethod
    def _infer_fn_name(signature: str) -> str:
        m = re.search(r"\bfn\s+([a-zA-Z_]\w*)", signature or "")
        return m.group(1) if m else "solve"

    def _check_correctness(self, source: str, challenge: Challenge) -> float:
        """Run test cases against compiled output. Returns pass rate [0.0, 1.0]."""
        if not challenge.test_cases:
            return 1.0

        clean_source = self._strip_existing_main(source)
        ret_type = self._infer_return_type(challenge.function_signature)
        fn_name = self._infer_fn_name(challenge.function_signature)

        passed = 0
        valid_cases = 0
        for tc in challenge.test_cases:
            call = tc.get("call", "")
            if not call and "input" in tc:
                inp = tc["input"]
                call = f"{fn_name}({inp})"
            expected = str(tc.get("expected", "")).strip()
            if not call:
                continue
            valid_cases += 1

            wrapper = f"{clean_source}\n\nfn main() -> {ret_type} {{\n    {call}\n}}"
            result = _c.compile_and_run(wrapper, timeout_seconds=5.0)

            if result.success:
                actual = result.output.strip()
                norm_actual = actual.replace(" ", "")
                norm_expected = expected.replace(" ", "")
                if norm_actual == norm_expected or actual.splitlines()[-1:].count(expected) > 0:
                    passed += 1

        return passed / valid_cases if valid_cases > 0 else 1.0

    # ── Private: Vitalis Native Evo Dual-Track ─────────────────────────────

    def _vitalis_evo_register(self, challenge: Challenge, gen: Generation) -> None:
        if not self.config.use_vitalis_native_evo:
            return
        champion = next((s for s in gen.submissions if s.id == gen.champion_id), None)
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
            pass

    # ── Logging ────────────────────────────────────────────────────────────

    def _log_gen(
        self,
        gen: Generation,
        prev_sources: list[str],
        diversity: float = 0.0,
        ema: float = 0.0,
    ) -> None:
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
        print(
            f"  {gen.number:>4}  {len(scored):>7}  {len(failed):>7}  "
            f"{gen.champion_fitness:>10.2f}  {diversity_str:>10}  {notes}"
        )

    def _log_final(self, tournament: Tournament) -> None:
        print(f"\n{'═' * 65}")
        print(f"  🏆  TOURNAMENT {tournament.status.upper()}")
        print(f"  📊  Total generations : {len(tournament.generations)}")

        champion = tournament.champion
        if champion and champion.fitness:
            print(f"  🎯  Champion fitness  : {champion.fitness.total:.2f}")
            print(f"  👤  Origin           : {champion.provider.value}")
            if champion.parent_a_id:
                print(f"  🧬  Ancestry         : evolved (gen {champion.generation})")
            if champion.mutations:
                print(f"  🔬  Last mutations   : {', '.join(champion.mutations[:3])}")
            print(f"\n  Fitness breakdown:")
            print(f"    Correctness : {champion.fitness.correctness:.2f}")
            print(f"    Performance : {champion.fitness.performance:.2f}")
            print(f"    Qual/Style  : {champion.fitness.code_quality:.2f}")
            print(f"    Novelty     : {champion.fitness.novelty:.2f}")

            if champion.compilation:
                print(f"\n  Compiler stats:")
                print(f"    Compile time : {champion.compilation.compile_time_ms:.1f}ms")
                print(f"    Gate reached : {champion.compilation.gate_reached.value}")

            try:
                ast_str = _c.parse_ast(champion.source_code)
                if ast_str and not ast_str.startswith("(AST"):
                    print(f"\n  AST (champion): {ast_str[:120]}...")
            except Exception:
                pass

            print(f"\n  Champion source:")
            for line in champion.source_code.split("\n"):
                print(f"    {line}")

        gen0 = tournament.generations[0] if tournament.generations else None
        if gen0:
            print(f"\n  Seed provider results (Gen 0):")
            for s in gen0.submissions:
                crown = "👑" if s.id == gen0.champion_id else "  "
                fit = f"{s.fitness.total:.1f}" if s.fitness else "FAIL"
                print(f"    {crown} {s.provider.value:12s} │ fitness: {fit}")

        print(f"{'═' * 65}\n")
