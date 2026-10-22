"""
The Forge — Budget, Cost & Token Observability

MCPlex/AgentLens-inspired cost tracking, token optimization, and budget governance.

Tracks:
  - Token usage (input/output) per request
  - Cost per model per generation
  - Budget limits with auto-halt
  - Cost-per-fitness-point efficiency metrics
  - Provider cost comparison
  - Token compression / prompt optimization hints
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import compiler as _c     # hotpath EMA for trend tracking


# ── OpenRouter Pricing ($ per 1M tokens) ─────────────────────────────────────

PRICING = {
    # Ultra / Elite tier (October 2026)
    "anthropic/claude-fable-5-1":   {"input": 10.00, "output": 50.00},
    "anthropic/claude-fable-5":     {"input": 10.00, "output": 50.00},
    "anthropic/claude-mythos-5":    {"input": 15.00, "output": 75.00},
    "anthropic/claude-opus-5-5":    {"input": 5.00, "output": 25.00},
    "anthropic/claude-opus-4-8":    {"input": 5.00, "output": 25.00},
    # Premium tier
    "anthropic/claude-sonnet-5-5":  {"input": 3.00, "output": 15.00},
    "anthropic/claude-sonnet-5":    {"input": 3.00, "output": 15.00},
    "anthropic/claude-haiku-5-5":   {"input": 0.80, "output": 4.00},
    "openai/gpt-6-astra":           {"input": 2.50, "output": 10.00},
    "openai/gpt-6.1-sol":           {"input": 3.00, "output": 24.00},
    "openai/gpt-5.6-sol":           {"input": 3.00, "output": 24.00},
    "openai/gpt-5":                 {"input": 1.25, "output": 5.00},
    "openai/o3":                    {"input": 2.00, "output": 8.00},
    "openai/o3-mini":               {"input": 1.10, "output": 4.40},
    "google/gemini-4-argon":        {"input": 2.00, "output": 12.00},
    "google/gemini-3.8-pro":        {"input": 1.25, "output": 10.00},
    "google/gemini-3.1-pro":        {"input": 2.00, "output": 12.00},
    "google/gemini-3.8-flash":      {"input": 0.15, "output": 0.60},
    "google/gemini-3.5-flash":      {"input": 0.30, "output": 2.50},
    "x-ai/grok-4.7":                {"input": 3.00, "output": 15.00},
    "x-ai/grok-4.5":                {"input": 3.00, "output": 15.00},
    "deepseek/deepseek-chat":       {"input": 0.27, "output": 1.10},
    "deepseek/deepseek-r1":         {"input": 0.55, "output": 2.19},
    "deepseek/deepseek-reasoner":   {"input": 0.55, "output": 2.19},
    # Economy tier
    "openai/gpt-5.6-terra":         {"input": 0.60, "output": 2.40},
    "openai/gpt-5.6-luna":          {"input": 0.15, "output": 0.60},
    "meta-llama/llama-3.3-70b-instruct": {"input": 0.40, "output": 0.40},
    # Local SLM (free — runs on local GPU/CPU via Ollama)
    "qwen2.5-coder:7b":             {"input": 0.00, "output": 0.00},
    "llama3.2:latest":              {"input": 0.00, "output": 0.00},
    "codellama:7b":                 {"input": 0.00, "output": 0.00},
    "mistral:latest":               {"input": 0.00, "output": 0.00},
    "phi3:mini":                    {"input": 0.00, "output": 0.00},
}


@dataclass
class RequestTrace:
    """Single API request trace with full telemetry."""
    timestamp: float = 0.0
    model_id: str = ""
    provider_name: str = ""
    vendor: str = ""            # "anthropic", "openai", "google", "deepseek", "xai", "openrouter", "ollama"
    purpose: str = ""           # "code_gen", "chat", "research"
    
    # Token metrics
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    
    # Cost
    cost_usd: float = 0.0
    
    # Timing
    api_latency_ms: float = 0.0
    ttft_ms: float = 0.0     # time to first token (if streaming)
    
    # Context
    generation: int = -1
    submission_id: str = ""
    
    # Optimization
    prompt_chars: int = 0
    response_chars: int = 0
    compression_ratio: float = 0.0   # response_chars / output_tokens


@dataclass
class BudgetConfig:
    """Budget limits and alerting thresholds."""
    max_budget_usd: float = 5.00          # hard cap
    warning_threshold_pct: float = 0.80   # warn at 80%
    max_tokens_per_request: int = 1024
    max_requests_per_minute: int = 60
    alert_on_cost_spike: bool = True
    cost_spike_threshold: float = 3.0     # 3× the running average = spike


class CostTracker:
    """
    Full cost and token observability — tracks every API call,
    computes running costs, enforces budgets, and optimizes spend.
    """

    def __init__(self, budget: Optional[BudgetConfig] = None):
        if budget is None:
            default_max = 5.00
            try:
                default_max = float(os.environ.get("FORGE_BUDGET_USD", "5.00"))
            except ValueError:
                default_max = 5.00
            budget = BudgetConfig(max_budget_usd=default_max)
        self.budget = budget
        self.traces: list[RequestTrace] = []
        self._lock = threading.RLock()
        self._total_cost: float = 0.0
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._cost_ema: float = 0.0
        self._provider_costs: dict[str, float] = {}
        self._provider_tokens: dict[str, int] = {}
        self._request_count: int = 0
        self._budget_exhausted: bool = False

    # ── Record Trace ──────────────────────────────────────────────────────

    def record(self, trace: RequestTrace) -> None:
        """Record a request trace and update running metrics."""
        if not trace.timestamp:
            trace.timestamp = time.time()
        if trace.total_tokens <= 0:
            trace.total_tokens = trace.input_tokens + trace.output_tokens

        # Calculate cost (local Ollama models are always free)
        if trace.vendor == "ollama" or (":" in trace.model_id and "/" not in trace.model_id):
            pricing = {"input": 0.0, "output": 0.0}
        else:
            pricing = PRICING.get(trace.model_id, {"input": 1.0, "output": 3.0})
        trace.cost_usd = (
            (trace.input_tokens / 1_000_000) * pricing["input"]
            + (trace.output_tokens / 1_000_000) * pricing["output"]
        )

        # Compression ratio
        if trace.output_tokens > 0:
            trace.compression_ratio = trace.response_chars / trace.output_tokens

        with self._lock:
            self.traces.append(trace)
            self._total_cost += trace.cost_usd
            self._total_input_tokens += trace.input_tokens
            self._total_output_tokens += trace.output_tokens
            self._request_count += 1

            # Per-provider tracking
            p = trace.provider_name or trace.vendor or "unknown"
            self._provider_costs[p] = self._provider_costs.get(p, 0) + trace.cost_usd
            self._provider_tokens[p] = self._provider_tokens.get(p, 0) + trace.total_tokens

            # Cost spike detection BEFORE updating EMA with the spike itself
            if (
                self.budget.alert_on_cost_spike
                and self._request_count > 5
                and self._cost_ema > 0
                and trace.cost_usd > self._cost_ema * self.budget.cost_spike_threshold
            ):
                self._alert(
                    f"⚠️  COST SPIKE: ${trace.cost_usd:.4f} "
                    f"(EMA: ${self._cost_ema:.4f}) on {trace.model_id}"
                )

            # EMA cost trend
            if self._request_count == 1:
                self._cost_ema = trace.cost_usd
            else:
                self._cost_ema = _c.hotpath_ema_update(self._cost_ema, trace.cost_usd, 0.3)

            # Budget check
            if self._total_cost >= self.budget.max_budget_usd:
                self._budget_exhausted = True
                self._alert(
                    f"🛑 BUDGET EXHAUSTED: ${self._total_cost:.4f} "
                    f">= ${self.budget.max_budget_usd:.2f}"
                )
            elif self._total_cost >= self.budget.max_budget_usd * self.budget.warning_threshold_pct:
                self._alert(
                    f"⚠️  BUDGET WARNING: ${self._total_cost:.4f} "
                    f"({self.pct_used:.0%} of ${self.budget.max_budget_usd:.2f})"
                )

    def reset(self) -> None:
        """Reset tracked traces and costs."""
        with self._lock:
            self.traces.clear()
            self._total_cost = 0.0
            self._total_input_tokens = 0
            self._total_output_tokens = 0
            self._cost_ema = 0.0
            self._provider_costs.clear()
            self._provider_tokens.clear()
            self._request_count = 0
            self._budget_exhausted = False

    # ── Budget Queries ────────────────────────────────────────────────────

    @property
    def is_exhausted(self) -> bool:
        with self._lock:
            return self._budget_exhausted

    @property
    def total_cost(self) -> float:
        with self._lock:
            return self._total_cost

    @property
    def remaining_budget(self) -> float:
        with self._lock:
            return max(0.0, self.budget.max_budget_usd - self._total_cost)

    @property
    def pct_used(self) -> float:
        with self._lock:
            return self._total_cost / self.budget.max_budget_usd if self.budget.max_budget_usd > 0 else 0.0

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return self._total_input_tokens + self._total_output_tokens

    # ── Analytics ──────────────────────────────────────────────────────────

    def cost_per_provider(self) -> dict[str, dict]:
        """Cost breakdown per provider."""
        with self._lock:
            result = {}
            for p in self._provider_costs:
                calls = sum(1 for t in self.traces if (t.provider_name or t.vendor or "unknown") == p)
                result[p] = {
                    "total_cost": round(self._provider_costs[p], 6),
                    "total_tokens": self._provider_tokens[p],
                    "calls": calls,
                    "avg_cost_per_call": round(self._provider_costs[p] / max(calls, 1), 6),
                    "avg_tokens_per_call": self._provider_tokens[p] // max(calls, 1),
                }
            return result

    def cost_per_generation(self) -> dict[int, float]:
        """Cost accumulated per generation."""
        with self._lock:
            gens: dict[int, float] = {}
            for t in self.traces:
                if t.generation >= 0:
                    gens[t.generation] = gens.get(t.generation, 0) + t.cost_usd
            return {k: round(v, 6) for k, v in sorted(gens.items())}

    def cheapest_provider(self) -> str:
        """Provider with lowest cost per token."""
        with self._lock:
            best = ""
            best_cpt = float("inf")
            for p in self._provider_costs:
                tokens = max(self._provider_tokens.get(p, 1), 1)
                cpt = self._provider_costs[p] / tokens
                if cpt < best_cpt:
                    best_cpt = cpt
                    best = p
            return best

    def optimization_hints(self) -> list[str]:
        """Return actionable cost optimization suggestions."""
        with self._lock:
            hints = []

            for p, data in self.cost_per_provider().items():
                pricing = PRICING.get(
                    next((t.model_id for t in self.traces if t.provider_name == p), ""),
                    {"input": 0, "output": 0}
                )
                if pricing["output"] > 5.0 and data["calls"] > 10:
                    hints.append(
                        f"Consider using a cheaper model for '{p}' — "
                        f"${pricing['output']}/M output tokens, {data['calls']} calls"
                    )

            for t in self.traces[-10:]:
                if t.output_tokens > 500 and t.response_chars < 200:
                    hints.append(
                        f"Model {t.provider_name} returned {t.output_tokens} tokens "
                        f"but only {t.response_chars} useful chars — consider stricter max_tokens"
                    )

            avg_input = self._total_input_tokens / max(self._request_count, 1)
            if avg_input > 800:
                hints.append(
                    f"Average input is {avg_input:.0f} tokens — "
                    f"consider shortening system prompt"
                )

            return hints

    # ── Reporting ──────────────────────────────────────────────────────────

    def summary(self) -> str:
        """Human-readable cost summary."""
        with self._lock:
            lines = [
                f"{'═' * 55}",
                f"  💰  COST & TOKEN REPORT",
                f"{'─' * 55}",
                f"  Total cost     : ${self._total_cost:.4f} / ${self.budget.max_budget_usd:.2f} ({self.pct_used:.0%})",
                f"  Remaining      : ${self.remaining_budget:.4f}",
                f"  Total tokens   : {self.total_tokens:,} ({self._total_input_tokens:,} in / {self._total_output_tokens:,} out)",
                f"  Total requests : {self._request_count}",
                f"  Avg cost/req   : ${self._total_cost / max(self._request_count, 1):.6f}",
                f"  Cost EMA       : ${self._cost_ema:.6f}",
                f"{'─' * 55}",
            ]

            for p, data in self.cost_per_provider().items():
                lines.append(
                    f"  {p:12s} │ ${data['total_cost']:.4f} │ "
                    f"{data['total_tokens']:>6,} tok │ {data['calls']} calls"
                )

            hints = self.optimization_hints()
            if hints:
                lines.append(f"{'─' * 55}")
                lines.append(f"  💡  Optimization hints:")
                for h in hints:
                    lines.append(f"     • {h}")

            lines.append(f"{'═' * 55}")
            return "\n".join(lines)

    # ── Internals ──────────────────────────────────────────────────────────

    def _alert(self, message: str) -> None:
        print(f"  [BudgetTracker] {message}")


BudgetTracker = CostTracker


# ── Token Estimation ──────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Code-aware token estimator."""
    if not text:
        return 1
    symbol_count = sum(1 for c in text if c in '{}[]<>;:()=+*/\\|&!@#$%^~`"\'')
    density = symbol_count / max(len(text), 1)
    chars_per_token = 3 if density > 0.05 else 4
    return max(1, len(text) // chars_per_token)


def estimate_cost_preview(text: str, model_id: str, direction: str = "input") -> float:
    """Pre-flight cost estimate before making an API call."""
    tokens = estimate_tokens(text)
    if ":" in model_id and "/" not in model_id:
        pricing = {"input": 0.0, "output": 0.0}
    else:
        pricing = PRICING.get(model_id, {"input": 1.0, "output": 3.0})
    rate = pricing.get(direction, pricing.get("input", 1.0))
    return (tokens / 1_000_000) * rate


# ── Global Singleton ──────────────────────────────────────────────────────────

_tracker: Optional[CostTracker] = None


def get_tracker(budget: Optional[BudgetConfig] = None) -> CostTracker:
    """Get or create the global cost tracker. Updates budget config if explicitly passed."""
    global _tracker
    if _tracker is None:
        _tracker = CostTracker(budget)
    elif budget is not None:
        with _tracker._lock:
            _tracker.budget = budget
            _tracker._budget_exhausted = _tracker._total_cost >= budget.max_budget_usd
    return _tracker
