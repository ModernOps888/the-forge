"""
The Forge — Data Models

Core data structures for the multi-agent code evolution platform.
Every entity is immutable and timestamped for full auditability.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Provider(str, Enum):
    """LLM providers that compete in the arena."""
    CLAUDE = "claude"
    GPT = "gpt"
    GEMINI = "gemini"
    LOCAL = "local"          # locally-generated (evolution engine)
    HUMAN = "human"          # human-submitted


class CompilationGate(str, Enum):
    """The seven gates a submission must pass."""
    LEX = "lex"
    PARSE = "parse"
    TYPE_CHECK = "type_check"
    LINT = "lint"
    CAPABILITY = "capability"
    COMPILE = "compile"
    EXECUTE = "execute"


class SubmissionStatus(str, Enum):
    """Lifecycle of a submission."""
    PENDING = "pending"
    COMPILING = "compiling"
    COMPILED = "compiled"
    BENCHMARKING = "benchmarking"
    SCORED = "scored"
    PROMOTED = "promoted"
    ELIMINATED = "eliminated"
    FAILED = "failed"


@dataclass(frozen=True)
class Challenge:
    """A coding challenge that agents compete to solve."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    description: str = ""
    function_signature: str = ""           # e.g. "fn sort(arr: [i64]) -> [i64]"
    test_cases: list[dict] = field(default_factory=list)  # [{"input": ..., "expected": ...}]
    constraints: dict = field(default_factory=dict)        # {"time_ms": 50, "memory_kb": 1024}
    difficulty: int = 1                    # 1-10
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class CompilationResult:
    """Result of passing code through the Vitalis compiler gates."""
    success: bool = False
    gate_reached: CompilationGate = CompilationGate.LEX
    output: str = ""
    error: str = ""
    compile_time_ms: float = 0.0
    token_count: int = 0
    ast_node_count: int = 0
    ir_instruction_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FitnessScore:
    """Multi-dimensional fitness evaluation."""
    correctness: float = 0.0       # 0-1: test cases passed
    performance: float = 0.0       # 0-1: relative speed rank
    code_quality: float = 0.0      # 0-1: linter score
    robustness: float = 0.0        # 0-1: fuzz survival rate
    efficiency: float = 0.0        # 0-1: memory/allocation efficiency
    novelty: float = 0.0           # 0-1: code distance from existing solutions

    # Configurable weights
    WEIGHTS: dict = field(default_factory=lambda: {
        "correctness": 0.30,
        "performance": 0.25,
        "code_quality": 0.15,
        "robustness": 0.15,
        "efficiency": 0.10,
        "novelty": 0.05,
    })

    @property
    def total(self) -> float:
        """Weighted composite fitness score (0-100)."""
        w = self.WEIGHTS
        return (
            self.correctness * w["correctness"]
            + self.performance * w["performance"]
            + self.code_quality * w["code_quality"]
            + self.robustness * w["robustness"]
            + self.efficiency * w["efficiency"]
            + self.novelty * w["novelty"]
        ) * 100


@dataclass(frozen=True)
class BenchmarkResult:
    """Statistical benchmark results from N runs."""
    runs: int = 0
    mean_ms: float = 0.0
    stddev_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    peak_memory_kb: float = 0.0
    confidence_interval_95: tuple[float, float] = (0.0, 0.0)


@dataclass
class Submission:
    """A single code submission in a tournament."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    challenge_id: str = ""
    generation: int = 0
    provider: Provider = Provider.LOCAL
    source_code: str = ""
    parent_a_id: Optional[str] = None     # for crossover offspring
    parent_b_id: Optional[str] = None
    mutations: list[str] = field(default_factory=list)  # mutation descriptions
    status: SubmissionStatus = SubmissionStatus.PENDING
    compilation: Optional[CompilationResult] = None
    benchmark: Optional[BenchmarkResult] = None
    fitness: Optional[FitnessScore] = None
    created_at: float = field(default_factory=time.time)

    @property
    def is_offspring(self) -> bool:
        return self.parent_a_id is not None


@dataclass
class Generation:
    """A single generation in the evolutionary tournament."""
    number: int = 0
    challenge_id: str = ""
    submissions: list[Submission] = field(default_factory=list)
    champion_id: Optional[str] = None
    champion_fitness: float = 0.0
    created_at: float = field(default_factory=time.time)

    @property
    def ranked(self) -> list[Submission]:
        """Submissions sorted by fitness (descending)."""
        scored = [s for s in self.submissions if s.fitness is not None]
        return sorted(scored, key=lambda s: s.fitness.total, reverse=True)


@dataclass
class Tournament:
    """A complete evolutionary tournament for one challenge."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    challenge: Challenge = field(default_factory=Challenge)
    generations: list[Generation] = field(default_factory=list)
    providers: list[Provider] = field(default_factory=list)
    max_generations: int = 100
    population_size: int = 50
    elite_count: int = 10
    status: str = "pending"  # pending, running, converged, halted
    created_at: float = field(default_factory=time.time)

    @property
    def current_generation(self) -> int:
        return len(self.generations)

    @property
    def champion(self) -> Optional[Submission]:
        if not self.generations:
            return None
        last = self.generations[-1]
        ranked = last.ranked
        return ranked[0] if ranked else None
