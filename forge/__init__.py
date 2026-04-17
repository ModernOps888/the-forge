"""The Forge — Enterprise Multi-Agent Agent Factory & Code Evolution Platform."""

from .models import (
    Challenge, Submission, Generation, Tournament,
    Provider, FitnessScore, BenchmarkResult, CompilationResult,
)
from .arena import Arena, ArenaConfig
from .compiler import compile_and_run, type_check, lex, vitalis_version
from .benchmark import compute_benchmark, welch_t_test, adaptive_fitness_score, compute_pareto_front
from .evolution import EvolutionConfig, evolve_generation
from .budget import CostTracker, BudgetConfig, get_tracker
from .governance import ProvenanceChain, PolicyEngine, get_provenance, get_policy_engine
from .providers import openrouter_provider, chat, multi_agent_research, consensus, auto_route, MODELS
from .replay import ReplayConfig, save_replay, load_replay
from .skills import SkillDNA, certify_from_compilation
from .marketplace import Marketplace, get_marketplace
from .factory import build as factory_build, FactoryResult, ARTIFACT_TYPES

__version__ = "2.0.0"
__all__ = [
    # Core
    "Arena", "ArenaConfig",
    "Challenge", "Submission", "Generation", "Tournament",
    "Provider", "FitnessScore", "BenchmarkResult", "CompilationResult",
    # Compiler
    "compile_and_run", "type_check", "lex", "vitalis_version",
    # Benchmark
    "compute_benchmark", "welch_t_test", "adaptive_fitness_score", "compute_pareto_front",
    # Evolution
    "EvolutionConfig", "evolve_generation",
    # Budget & Cost
    "CostTracker", "BudgetConfig", "get_tracker",
    # Governance
    "ProvenanceChain", "PolicyEngine", "get_provenance", "get_policy_engine",
    # Providers
    "openrouter_provider", "chat", "multi_agent_research", "consensus", "auto_route", "MODELS",
    # Replay
    "ReplayConfig", "save_replay", "load_replay",
    # Skills & Marketplace
    "SkillDNA", "certify_from_compilation",
    "Marketplace", "get_marketplace",
    # Factory
    "factory_build", "FactoryResult", "ARTIFACT_TYPES",
]
