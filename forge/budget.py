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

import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from . import compiler as _c     # hotpath EMA for trend tracking


# ── OpenRouter Pricing ($ per 1M tokens) ─────────────────────────────────────

PRICING = {
    # Elite tier (April 2026)
    "anthropic/claude-opus-4.7":    {"input": 15.00, "output": 75.00},
    # Premium tier (April 2026)
    "anthropic/claude-sonnet-4.6":  {"input": 3.00, "output": 15.00},
    "openai/gpt-5.4":               {"input": 2.50, "output": 20.00},
    "google/gemini-2.5-pro":        {"input": 1.25, "output": 10.00},
    "deepseek/deepseek-chat-v3-0324":{"input": 0.27, "output": 1.10},
    # Economy tier
    "openai/gpt-5.4-mini":          {"input": 0.40, "output": 1.60},
    "openai/gpt-5.4-nano":          {"input": 0.10, "output": 0.40},
    "google/gemini-2.5-flash":      {"input": 0.30, "output": 2.50},
    # Local SLM (free — runs on local GPU via Ollama)
    "qwen2.5-coder:7b":             {"input": 0.00, "output": 0.00},
}


@dataclass
class RequestTrace:
    """Single API request trace with full telemetry."""
    timestamp: float = 0.0
    model_id: str = ""
    provider_name: str = ""
    vendor: str = ""            # "anthropic", "openai", "google", "deepseek", "openrouter", "ollama"
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
        self.budget = budget or BudgetConfig()
        self.traces: list[RequestTrace] = []
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
        # Calculate cost
        pricing = PRICING.get(trace.model_id, {"input": 1.0, "output": 3.0})
        trace.cost_usd = (
            (trace.input_tokens / 1_000_000) * pricing["input"]
            + (trace.output_tokens / 1_000_000) * pricing["output"]
        )

        # Compression ratio
        if trace.output_tokens > 0:
            trace.compression_ratio = trace.response_chars / trace.output_tokens

        self.traces.append(trace)
        self._total_cost += trace.cost_usd
        self._total_input_tokens += trace.input_tokens
        self._total_output_tokens += trace.output_tokens
        self._request_count += 1

        # Per-provider tracking
        p = trace.provider_name
        self._provider_costs[p] = self._provider_costs.get(p, 0) + trace.cost_usd
        self._provider_tokens[p] = self._provider_tokens.get(p, 0) + trace.total_tokens

        # EMA cost trend
        self._cost_ema = _c.hotpath_ema_update(self._cost_ema, trace.cost_usd, 0.3)

        # Cost spike detection
        if (self.budget.alert_on_cost_spike
            and self._request_count > 5
            and trace.cost_usd > self._cost_ema * self.budget.cost_spike_threshold):
            self._alert(f"⚠️  COST SPIKE: ${trace.cost_usd:.4f} "
                       f"(EMA: ${self._cost_ema:.4f}) on {trace.model_id}")

        # Budget check
        if self._total_cost >= self.budget.max_budget_usd:
            self._budget_exhausted = True
            self._alert(f"🛑 BUDGET EXHAUSTED: ${self._total_cost:.4f} "
                       f">= ${self.budget.max_budget_usd:.2f}")

        elif self._total_cost >= self.budget.max_budget_usd * self.budget.warning_threshold_pct:
            self._alert(f"⚠️  BUDGET WARNING: ${self._total_cost:.4f} "
                       f"({self.pct_used:.0%} of ${self.budget.max_budget_usd:.2f})")

    # ── Budget Queries ────────────────────────────────────────────────────

    @property
    def is_exhausted(self) -> bool:
        return self._budget_exhausted

    @property
    def total_cost(self) -> float:
        return self._total_cost

    @property
    def remaining_budget(self) -> float:
        return max(0, self.budget.max_budget_usd - self._total_cost)

    @property
    def pct_used(self) -> float:
        return self._total_cost / self.budget.max_budget_usd if self.budget.max_budget_usd > 0 else 0

    @property
    def total_tokens(self) -> int:
        return self._total_input_tokens + self._total_output_tokens

    # ── Analytics ──────────────────────────────────────────────────────────

    def cost_per_provider(self) -> dict[str, dict]:
        """Cost breakdown per provider."""
        result = {}
        for p in self._provider_costs:
            calls = sum(1 for t in self.traces if t.provider_name == p)
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
        gens: dict[int, float] = {}
        for t in self.traces:
            if t.generation >= 0:
                gens[t.generation] = gens.get(t.generation, 0) + t.cost_usd
        return {k: round(v, 6) for k, v in sorted(gens.items())}

    def cheapest_provider(self) -> str:
        """Provider with lowest cost per token."""
        best = ""
        best_cpt = float("inf")
        for p in self._provider_costs:
            tokens = self._provider_tokens.get(p, 1)
            cpt = self._provider_costs[p] / tokens
            if cpt < best_cpt:
                best_cpt = cpt
                best = p
        return best

    def optimization_hints(self) -> list[str]:
        """Return actionable cost optimization suggestions."""
        hints = []

        # Check if we're using expensive models for simple tasks
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

        # Check output token waste
        for t in self.traces[-10:]:
            if t.output_tokens > 500 and t.response_chars < 200:
                hints.append(
                    f"Model {t.provider_name} returned {t.output_tokens} tokens "
                    f"but only {t.response_chars} useful chars — consider stricter max_tokens"
                )

        # Prompt size optimization
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

        # Per-provider breakdown
        for p, data in self.cost_per_provider().items():
            lines.append(
                f"  {p:12s} │ ${data['total_cost']:.4f} │ "
                f"{data['total_tokens']:>6,} tok │ {data['calls']} calls"
            )

        # Optimization hints
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


# ── Token Estimation ──────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Code-aware token estimator.
    
    Plain English averages ~4 chars/token with most BPE tokenizers.
    Source code (Rust, Python, Vitalis .sl) averages ~3 chars/token because
    symbols like { } [ ] < > ; : are individual tokens.
    We detect symbol density and adjust dynamically.
    """
    if not text:
        return 1
    symbol_count = sum(1 for c in text if c in '{}[]<>;:()=+*/\\|&!@#$%^~`"\'')
    density = symbol_count / max(len(text), 1)
    chars_per_token = 3 if density > 0.05 else 4
    return max(1, len(text) // chars_per_token)


def estimate_cost_preview(text: str, model_id: str, direction: str = "input") -> float:
    """Pre-flight cost estimate before making an API call.
    
    Returns estimated cost in USD for the given text at the given model's pricing.
    Useful for budget guard checks before expensive operations.
    """
    tokens = estimate_tokens(text)
    pricing = PRICING.get(model_id, {"input": 1.0, "output": 3.0})
    rate = pricing.get(direction, pricing.get("input", 1.0))
    return (tokens / 1_000_000) * rate


# ── Global Singleton ──────────────────────────────────────────────────────────

_tracker: Optional[CostTracker] = None

def get_tracker(budget: Optional[BudgetConfig] = None) -> CostTracker:
    """Get or create the global cost tracker."""
    global _tracker
    if _tracker is None:
        _tracker = CostTracker(budget)
    return _tracker
