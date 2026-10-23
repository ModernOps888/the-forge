"""The Forge — Enterprise Multi-Agent Agent Factory & Code Evolution Platform."""

from .models import (
    Challenge, Submission, Generation, Tournament,
    Provider, CompilationGate, SubmissionStatus,
    FitnessScore, BenchmarkResult, CompilationResult,
)
from .arena import Arena, ArenaConfig
from .compiler import compile_and_run, type_check, lex, parse_ast, vitalis_version, validate_safety
from .benchmark import (
    compute_benchmark, compute_ci_95, welford_stats, welch_t_test,
    adaptive_fitness_score, compute_pareto_front, kls_spectral_diagnostics,
)
from .evolution import (
    EvolutionConfig, StochasticLocalizationState,
    stochastic_localization_select, evolve_generation,
)
from .budget import CostTracker, BudgetTracker, BudgetConfig, get_tracker
from .governance import ProvenanceChain, PolicyEngine, CircuitBreaker, get_provenance, get_policy_engine
from .providers import openrouter_provider, chat, multi_agent_research, consensus, auto_route, MODELS
from .replay import (
    ReplayConfig, ABTestResult, save_replay, load_replay,
    create_replay_config, run_replay, run_ab_test,
)
from .skills import SkillDNA, certify_from_compilation
from .marketplace import Marketplace, get_marketplace
from .factory import build as factory_build, FactoryResult, Artifact, ARTIFACT_TYPES
from .deployer import deploy, deploy as factory_deploy, DeploymentResult
from .fitness_engine import score as score_code, score as fitness_score, VitalisFitnessReport
from .memory import MemoryEngine, get_memory
from .tracer import ExecutionTracer, get_tracer

__version__ = "2.5.0"
__all__ = [
    # Core
    "Arena", "ArenaConfig",
    "Challenge", "Submission", "Generation", "Tournament",
    "Provider", "CompilationGate", "SubmissionStatus",
    "FitnessScore", "BenchmarkResult", "CompilationResult",
    # Compiler
    "compile_and_run", "type_check", "lex", "parse_ast", "vitalis_version", "validate_safety",
    # Benchmark & KLS Spectral Diagnostics
    "compute_benchmark", "compute_ci_95", "welford_stats", "welch_t_test",
    "adaptive_fitness_score", "compute_pareto_front", "kls_spectral_diagnostics",
    # Evolution & Stochastic Localization
    "EvolutionConfig", "StochasticLocalizationState", "stochastic_localization_select", "evolve_generation",
    # Budget & Cost
    "CostTracker", "BudgetTracker", "BudgetConfig", "get_tracker",
    # Governance
    "ProvenanceChain", "PolicyEngine", "CircuitBreaker", "get_provenance", "get_policy_engine",
    # Providers
    "openrouter_provider", "chat", "multi_agent_research", "consensus", "auto_route", "MODELS",
    # Replay
    "ReplayConfig", "ABTestResult", "save_replay", "load_replay",
    "create_replay_config", "run_replay", "run_ab_test",
    # Skills & Marketplace
    "SkillDNA", "certify_from_compilation",
    "Marketplace", "get_marketplace",
    # Factory & Deployer & Polyglot Fitness
    "factory_build", "FactoryResult", "Artifact", "ARTIFACT_TYPES",
    "deploy", "factory_deploy", "DeploymentResult",
    "score_code", "fitness_score", "VitalisFitnessReport",
    # Memory & Tracer
    "MemoryEngine", "get_memory", "ExecutionTracer", "get_tracer",
]
