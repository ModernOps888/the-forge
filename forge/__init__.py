"""The Forge — Multi-Agent Code Evolution Platform powered by Vitalis."""

from .models import (
    Challenge, Submission, Generation, Tournament,
    Provider, FitnessScore, BenchmarkResult, CompilationResult,
)
from .arena import Arena, ArenaConfig
from .compiler import compile_and_run, type_check, lex
from .benchmark import compute_benchmark, welch_t_test
from .evolution import EvolutionConfig, evolve_generation

__version__ = "0.1.0"
__all__ = [
    "Arena", "ArenaConfig",
    "Challenge", "Submission", "Generation", "Tournament",
    "Provider", "FitnessScore", "BenchmarkResult", "CompilationResult",
    "compile_and_run", "type_check", "lex",
    "compute_benchmark", "welch_t_test",
    "EvolutionConfig", "evolve_generation",
]
