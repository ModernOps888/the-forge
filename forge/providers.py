"""
The Forge — Real LLM Providers via OpenRouter + Cost Tracking

Calls Claude, GPT-4, Gemini, and DeepSeek through OpenRouter's unified API.
Every request is traced for cost, token usage, and latency via the BudgetTracker.

Modes:
  - Code generation: providers for the evolution arena
  - Chat: multi-agent research/conversation
  - Auto-route: cheapest model that can handle the task
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from typing import Optional

from .budget import RequestTrace, get_tracker, estimate_tokens

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

def _get_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "")


# ── Model Registry ───────────────────────────────────────────────────────────

MODELS = {
    # Premium tier
    "claude":     "anthropic/claude-sonnet-4",
    "gpt":        "openai/gpt-4.1",
    "gemini":     "google/gemini-2.5-flash",
    "deepseek":   "deepseek/deepseek-chat-v3-0324",
    # Economy aliases
    "gpt-mini":   "openai/gpt-4.1-mini",
    "gpt-nano":   "openai/gpt-4.1-nano",
    "gemini-lite": "google/gemini-2.5-flash-lite",
}

MODEL_TIERS = {
    "cheap":   ["gemini", "deepseek", "gpt-nano", "gemini-lite"],
    "mid":     ["gpt-mini"],
    "premium": ["claude", "gpt"],
}


# ── System Prompts ────────────────────────────────────────────────────────────

CODE_SYSTEM = """You are a code generator for the Vitalis .sl language.
Vitalis is a statically-typed language with Rust-like syntax that compiles to native code via Cranelift JIT.

Key syntax rules:
- Functions: fn name(param: Type) -> ReturnType { body }
- Types: i64, f64, bool, [i64] (arrays), str
- Variables: let x: i64 = 5;  or  let mut x: i64 = 5;
- The entry point is fn main() -> i64 { ... }
- Return is implicit (last expression) or explicit with 'return'
- Comments: // single line
- Loops: while condition { body }
- Conditionals: if cond { } else { }
- Array ops: arr.len(), arr.get(i), arr.push(val), arr.slice(start, end)

CRITICAL RULES:
1. Return ONLY the function source code. No explanation, no markdown, no backticks.
2. Every function must be syntactically complete with matching braces.
3. Use only the types and operations listed above.
4. Include a main() function that calls your solution with a test case and returns the result.
"""

CHAT_SYSTEM = """You are a brilliant AI assistant in The Forge — a multi-agent code evolution platform.
You can research topics, analyze code, compare approaches, and provide expert-level answers.
Be concise, technically precise, and provide concrete examples when useful.
If asked about code, prefer Vitalis .sl syntax (Rust-like, JIT compiled) but also understand all major languages.
"""

RESEARCH_SYSTEM = """You are a research agent in The Forge platform.
Your job is to deeply research a topic and provide a comprehensive, well-structured analysis.
Include: key concepts, trade-offs, best practices, and concrete recommendations.
Be thorough but organized with clear sections.
"""


# ── Core API Call (with full telemetry) ──────────────────────────────────────

def call_openrouter(
    model_id: str,
    messages: list[dict],
    max_tokens: int = 1024,
    temperature: float = 0.7,
    provider_name: str = "",
    purpose: str = "code_gen",
    generation: int = -1,
    submission_id: str = "",
) -> tuple[str, dict]:
    """
    Call OpenRouter API with full cost/token tracing.
    
    Returns: (response_text, usage_dict)
    """
    key = _get_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    tracker = get_tracker()

    # Check budget
    if tracker.is_exhausted:
        raise RuntimeError(f"Budget exhausted (${tracker.total_cost:.4f} / ${tracker.budget.max_budget_usd:.2f})")

    payload = json.dumps({
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/ModernOps888/the-forge",
            "X-Title": "The Forge",
        },
        method="POST",
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latency_ms = (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter {e.code}: {body[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenRouter network error: {e.reason}")

    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})

    # Build trace
    prompt_text = " ".join(m.get("content", "") for m in messages)
    trace = RequestTrace(
        timestamp=time.time(),
        model_id=model_id,
        provider_name=provider_name or model_id.split("/")[0],
        purpose=purpose,
        input_tokens=usage.get("prompt_tokens", estimate_tokens(prompt_text)),
        output_tokens=usage.get("completion_tokens", estimate_tokens(content)),
        total_tokens=usage.get("total_tokens", 0),
        cost_usd=0,   # calculated by tracker
        api_latency_ms=latency_ms,
        generation=generation,
        submission_id=submission_id,
        prompt_chars=len(prompt_text),
        response_chars=len(content),
    )
    trace.total_tokens = trace.input_tokens + trace.output_tokens

    # Record in tracker (calculates cost, checks budget, detects spikes)
    tracker.record(trace)

    return content, {
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "cost_usd": trace.cost_usd,
        "latency_ms": latency_ms,
    }


# ── Code Provider Factory ────────────────────────────────────────────────────

def openrouter_provider(model_name: str):
    """Create a Forge-compatible provider for the evolution arena."""
    model_id = MODELS.get(model_name, model_name)

    def generate(description: str, function_signature: str) -> str:
        prompt = (
            f"Write a Vitalis .sl function.\n\n"
            f"TASK: {description}\n\n"
            f"SIGNATURE: {function_signature}\n\n"
            f"Write the complete function implementation. "
            f"Then add a main() function that tests it and returns an i64 result.\n"
            f"Return ONLY the code, no explanation."
        )
        messages = [
            {"role": "system", "content": CODE_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        content, usage = call_openrouter(
            model_id, messages,
            max_tokens=1024, temperature=0.7,
            provider_name=model_name, purpose="code_gen",
        )
        return _clean_code(content)

    return generate


def make_all_providers() -> dict[str, object]:
    return {name: openrouter_provider(name) for name in MODELS}


# ── Multi-Agent Chat ──────────────────────────────────────────────────────────

def chat(query: str, model_name: str = "gemini", history: list[dict] | None = None) -> str:
    """
    Send a chat message to a single model. Returns the response.
    Cheapest model by default for cost efficiency.
    """
    model_id = MODELS.get(model_name, model_name)
    messages = [{"role": "system", "content": CHAT_SYSTEM}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": query})

    content, usage = call_openrouter(
        model_id, messages,
        max_tokens=2048, temperature=0.5,
        provider_name=model_name, purpose="chat",
    )
    return content


def multi_agent_research(query: str, models: list[str] | None = None) -> dict[str, str]:
    """
    Send the same research query to multiple models and collect all responses.
    Returns {model_name: response_text}.
    """
    models = models or list(MODELS.keys())
    results = {}

    for name in models:
        model_id = MODELS.get(name, name)
        messages = [
            {"role": "system", "content": RESEARCH_SYSTEM},
            {"role": "user", "content": query},
        ]
        try:
            content, usage = call_openrouter(
                model_id, messages,
                max_tokens=2048, temperature=0.4,
                provider_name=name, purpose="research",
            )
            results[name] = content
        except Exception as e:
            results[name] = f"[ERROR: {e}]"

    return results


def consensus(query: str, models: list[str] | None = None) -> dict:
    """
    Ask all models the same question, then ask a synthesizer to merge their answers.
    Returns {"individual": {model: answer}, "consensus": merged_answer, "cost": total_cost}.
    """
    tracker = get_tracker()
    cost_before = tracker.total_cost

    # Phase 1: ask each model
    individual = multi_agent_research(query, models)

    # Phase 2: synthesize
    synthesis_prompt = f"Original question: {query}\n\n"
    for name, answer in individual.items():
        synthesis_prompt += f"=== {name.upper()} ===\n{answer[:1500]}\n\n"
    synthesis_prompt += (
        "Synthesize the above into a single, comprehensive answer. "
        "Note where models agree and where they diverge. Be precise."
    )

    synth_model = MODELS.get("gemini", "google/gemini-2.5-flash")  # cheapest for synthesis
    messages = [
        {"role": "system", "content": "You are a synthesis agent. Merge multiple AI responses into one authoritative answer."},
        {"role": "user", "content": synthesis_prompt},
    ]
    merged, _ = call_openrouter(
        synth_model, messages,
        max_tokens=2048, temperature=0.3,
        provider_name="gemini", purpose="synthesis",
    )

    return {
        "individual": individual,
        "consensus": merged,
        "cost_usd": round(tracker.total_cost - cost_before, 6),
    }


# ── Smart Router ──────────────────────────────────────────────────────────────

def auto_route(query: str, complexity: str = "auto") -> str:
    """
    Automatically pick the cheapest model that can handle the task.
    complexity: "simple" / "medium" / "hard" / "auto"
    """
    if complexity == "auto":
        # Heuristic: longer prompts or code-heavy = harder
        if len(query) > 500 or any(kw in query.lower() for kw in ("architect", "design", "complex", "optimize")):
            complexity = "hard"
        elif len(query) > 200:
            complexity = "medium"
        else:
            complexity = "simple"

    if complexity == "simple":
        model = "gemini"      # $0.15/M input — cheapest
    elif complexity == "medium":
        model = "gpt"         # $0.40/M input — mid-tier
    else:
        model = "claude"      # $3.00/M input — premium

    return chat(query, model_name=model)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_code(text: str) -> str:
    """Strip markdown fences and explanation from LLM code responses."""
    text = text.strip()
    for lang in ("sl", "rust", "vitalis", ""):
        fence = f"```{lang}"
        if fence in text:
            start = text.index(fence) + len(fence)
            end = text.rindex("```") if text.count("```") >= 2 else len(text)
            text = text[start:end].strip()
            break
    if "fn " in text and not text.startswith("fn ") and not text.startswith("//"):
        fn_idx = text.index("fn ")
        text = text[fn_idx:]
    return text


def test_provider(model_name: str = "deepseek") -> str:
    """Smoke test a single provider."""
    provider = openrouter_provider(model_name)
    return provider("Add two integers", "fn add(a: i64, b: i64) -> i64")
