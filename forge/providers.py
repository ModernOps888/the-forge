"""
The Forge — Multi-Vendor LLM Providers with Smart Routing

Smart routing architecture:
  1. If a direct vendor API key is set (e.g. ANTHROPIC_API_KEY), call the vendor directly.
  2. If not, fall back to OpenRouter as a universal relay.
  3. Local models always route to Ollama.

Every request is traced for cost, token usage, and latency via the BudgetTracker.

Modes:
  - Code generation: providers for the evolution arena
  - Chat: multi-agent research/conversation
  - Auto-route: cheapest model that can handle the task
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Optional

from .budget import RequestTrace, get_tracker, estimate_tokens

# ── Vendor Endpoints ─────────────────────────────────────────────────────────

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_URL  = "https://api.anthropic.com/v1/messages"
OPENAI_URL     = "https://api.openai.com/v1/chat/completions"
DEEPSEEK_URL   = "https://api.deepseek.com/chat/completions"
GOOGLE_AI_URL  = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

def _get_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "")

def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


# ── Vendor Key Resolution ────────────────────────────────────────────────────

# Maps model_id prefix → (env var name, direct call function name)
_VENDOR_MAP = {
    "anthropic/": ("ANTHROPIC_API_KEY", "anthropic"),
    "openai/":    ("OPENAI_API_KEY",    "openai"),
    "google/":    ("GOOGLE_AI_API_KEY",  "google"),
    "deepseek/":  ("DEEPSEEK_API_KEY",   "deepseek"),
}

def _resolve_vendor(model_id: str) -> tuple[str, str]:
    """Determine which vendor to call and with which API key.

    Returns (vendor_name, api_key).
    Prefers direct vendor key; falls back to OpenRouter.
    """
    for prefix, (env_var, vendor_name) in _VENDOR_MAP.items():
        if model_id.startswith(prefix):
            direct_key = os.environ.get(env_var, "")
            if direct_key:
                return vendor_name, direct_key
            break  # no direct key → fall through to OpenRouter

    or_key = _get_key()
    if or_key:
        return "openrouter", or_key

    raise RuntimeError(
        f"No API key for {model_id}. Set a direct vendor key or OPENROUTER_API_KEY. "
        f"See .env.example for details."
    )

def get_vendor_status() -> dict[str, dict]:
    """Return connection status for every vendor — used by /api/vendor-health."""
    vendors = {
        "openrouter":  {"key_env": "OPENROUTER_API_KEY",  "endpoint": OPENROUTER_URL},
        "anthropic":   {"key_env": "ANTHROPIC_API_KEY",   "endpoint": ANTHROPIC_URL},
        "openai":      {"key_env": "OPENAI_API_KEY",      "endpoint": OPENAI_URL},
        "google":      {"key_env": "GOOGLE_AI_API_KEY",   "endpoint": GOOGLE_AI_URL},
        "deepseek":    {"key_env": "DEEPSEEK_API_KEY",    "endpoint": DEEPSEEK_URL},
        "ollama":      {"key_env": None,                   "endpoint": f"{_ollama_host()}/api/chat"},
    }
    result = {}
    for name, info in vendors.items():
        has_key = bool(os.environ.get(info["key_env"], "")) if info["key_env"] else True
        result[name] = {"active": has_key, "endpoint": info["endpoint"]}
    # Attach hardware info
    result["_compute"] = detect_compute_hardware()
    return result


# ── Hardware Detection & Compute Mode ────────────────────────────────────────

def _get_compute_mode() -> str:
    """Get configured compute mode.
    
    Values:
      auto  — detect GPU, fallback to CPU (default)
      gpu   — force GPU (all layers)
      cpu   — force CPU only (num_gpu=0)
      split — split across GPU+CPU (num_gpu=half)
    """
    return os.environ.get("FORGE_COMPUTE", "auto").lower()


def detect_compute_hardware() -> dict:
    """Detect available compute hardware for local inference.
    
    Returns a dict with GPU info, CPU info, and recommended compute mode.
    Works on Windows, Linux, and macOS.
    """
    import subprocess
    hw = {
        "gpus": [],
        "cpu_cores": os.cpu_count() or 1,
        "ram_gb": 0,
        "compute_mode": _get_compute_mode(),
        "platform": sys.platform,
    }
    
    # Detect RAM
    try:
        import psutil
        hw["ram_gb"] = round(psutil.virtual_memory().total / (1024**3), 1)
    except ImportError:
        # Fallback for systems without psutil
        try:
            if os.name == "nt":
                out = subprocess.check_output(
                    'wmic computersystem get totalphysicalmemory /value',
                    shell=True, text=True, timeout=3
                )
                for line in out.strip().split('\n'):
                    if 'TotalPhysicalMemory' in line:
                        hw["ram_gb"] = round(int(line.split('=')[1].strip()) / (1024**3), 1)
        except Exception:
            pass
    
    # Detect NVIDIA GPUs
    try:
        out = subprocess.check_output(
            'nvidia-smi --query-gpu=name,memory.total,memory.free,driver_version --format=csv,noheader,nounits',
            shell=True, text=True, timeout=3
        )
        for i, line in enumerate(out.strip().split('\n')):
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 4:
                hw["gpus"].append({
                    "index": i,
                    "name": parts[0],
                    "vram_total_mb": int(parts[1]),
                    "vram_free_mb": int(parts[2]),
                    "driver": parts[3],
                    "vendor": "nvidia",
                })
    except Exception:
        pass
    
    # Detect AMD GPUs (ROCm)
    if not hw["gpus"]:
        try:
            out = subprocess.check_output(
                'rocm-smi --showproductname --showmeminfo vram --csv',
                shell=True, text=True, timeout=3
            )
            if 'GPU' in out:
                hw["gpus"].append({"index": 0, "name": "AMD ROCm GPU", "vendor": "amd"})
        except Exception:
            pass
    
    # Detect Apple Silicon (macOS unified memory)
    if not hw["gpus"] and os.name != "nt":
        try:
            out = subprocess.check_output(
                'sysctl -n machdep.cpu.brand_string',
                shell=True, text=True, timeout=3
            )
            if 'Apple' in out:
                hw["gpus"].append({
                    "index": 0,
                    "name": out.strip(),
                    "vram_total_mb": int(hw["ram_gb"] * 1024),  # unified memory
                    "vendor": "apple",
                })
        except Exception:
            pass
    
    # Auto-resolve compute mode
    if hw["compute_mode"] == "auto":
        if hw["gpus"]:
            hw["resolved_mode"] = "gpu"
        elif hw["ram_gb"] >= 16:
            hw["resolved_mode"] = "cpu"  # enough RAM for CPU inference
        else:
            hw["resolved_mode"] = "cpu"
    else:
        hw["resolved_mode"] = hw["compute_mode"]
    
    # Compute num_gpu for Ollama
    if hw["resolved_mode"] == "cpu":
        hw["ollama_num_gpu"] = 0
    elif hw["resolved_mode"] == "split":
        hw["ollama_num_gpu"] = 999 // 2  # half layers on GPU
    else:
        hw["ollama_num_gpu"] = 999  # all layers on GPU (Ollama default)
    
    return hw



# ── Model Registry ───────────────────────────────────────────────────────────

MODELS = {
    # Elite tier
    "claude-opus": "anthropic/claude-opus-4.7",
    # Premium tier (April 2026)
    "claude":     "anthropic/claude-sonnet-4.6",
    "gpt":        "openai/gpt-5.4",
    "gemini":     "google/gemini-2.5-pro",
    "deepseek":   "deepseek/deepseek-chat-v3-0324",
    # Economy aliases
    "gpt-mini":   "openai/gpt-5.4-mini",
    "gpt-nano":   "openai/gpt-5.4-nano",
    "gemini-lite": "google/gemini-2.5-flash",
    # Local SLM (runs via Ollama — GPU, CPU, or Apple Silicon)
    "qwen-local": "qwen2.5-coder:7b",
}

MODEL_TIERS = {
    "cheap":   ["gemini", "deepseek", "gpt-nano", "gemini-lite", "qwen-local"],
    "mid":     ["gpt-mini"],
    "premium": ["claude", "gpt"],
    "elite":   ["claude-opus"],
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

COWORK_SYSTEM = """You are an elite, autonomous AI Desktop Assistant (a "Claw" agent) wired natively into the user's Windows OS via The Forge Native Bridge.
You are collaborating with the user in a spatial sandbox environment.

CRITICAL CAPABILITY:
You possess the power to actively execute scripts on the host machine to solve the user's request flawlessly.
If you need to search files, open directories, write scripts, or inspect the environment, you MUST output your command wrapped exactly in `<execute_powershell>...</execute_powershell>`.
When you do this, I will intercept the command, execute it natively, and reply to you with the `stdout` terminal output so you can read the results.

EXAMPLE:
User: "Find infinity.png for me"
You: `<execute_powershell>Get-ChildItem -Path C:\\ -Filter infinity.png -Recurse -ErrorAction SilentlyContinue | Select-Object FullName</execute_powershell>`
System: `stdout: C:\\forge-seo\\assets\\infinity.png`
You: "I found your file! It is located at..."

RULES:
1. ONLY use `<execute_powershell>...</execute_powershell>`. Do not use markdown backticks when you actually want to execute it.
2. The user will not see the `<execute_powershell>` blocks; they only see your final conversational text and any markdown artifacts you generate.
3. If you write code that should be pinned to the Workspace Canvas, output it in standard standard ` ```python ` blocks as usual.
4. Run commands sequentially if needed. I will keep feeding you the `stdout` until you provide your final conversational answer without an `<execute_powershell>` tag.
5. NEVER attempt to launch interactive shells, browsers, VS Code, or isolated `antigravity.cmd` scripts. 
6. To send a message, instruction, or data payload natively to the Antigravity system, you MUST output your message wrapped in exactly `<send_antigravity>...</send_antigravity>`. I will intercept this and drop it directly into the native JSON inbox directory that Antigravity is actively monitoring.
"""


# ── Core API Call (with full telemetry) ──────────────────────────────────────

def _is_local_model(provider_name: str) -> bool:
    """Check if a model name refers to a local Ollama model."""
    return provider_name.endswith("-local")


def call_ollama_chat(
    model_id: str,
    messages: list[dict],
    max_tokens: int = 1024,
    temperature: float = 0.7,
    provider_name: str = "",
    purpose: str = "chat",
) -> tuple[str, dict]:
    """
    Call local Ollama API for chat/research with the same trace contract
    as call_openrouter. Cost is always $0 for local models.
    """
    tracker = get_tracker()

    # Resolve compute mode for local inference (GPU / CPU / split)
    hw = detect_compute_hardware()
    num_gpu = hw.get("ollama_num_gpu", 999)

    payload = json.dumps({
        "model": model_id,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": 8192,
            "num_predict": max_tokens,
            "num_gpu": num_gpu,
        },
    }).encode("utf-8")

    ollama_endpoint = f"{_ollama_host()}/api/chat"
    req = urllib.request.Request(
        ollama_endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latency_ms = (time.time() - t0) * 1000
    except Exception as e:
        raise RuntimeError(f"Ollama local error ({model_id} at {ollama_endpoint}): {e}")

    content = data["message"]["content"]

    # Ollama returns eval_count / prompt_eval_count when available
    in_tok = data.get("prompt_eval_count", estimate_tokens(" ".join(m.get("content", "") for m in messages)))
    out_tok = data.get("eval_count", estimate_tokens(content))

    prompt_text = " ".join(m.get("content", "") for m in messages)
    trace = RequestTrace(
        timestamp=time.time(),
        model_id=model_id,
        provider_name=provider_name or "qwen-local",
        vendor="ollama",
        purpose=purpose,
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=in_tok + out_tok,
        cost_usd=0.0,
        api_latency_ms=latency_ms,
        prompt_chars=len(prompt_text),
        response_chars=len(content),
    )
    tracker.record(trace)

    return content, {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_usd": 0.0,
        "latency_ms": latency_ms,
        "vendor": "ollama",
    }


# ── Direct Vendor Callers ────────────────────────────────────────────────────

def call_anthropic(
    model_id: str,
    messages: list[dict],
    api_key: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    provider_name: str = "",
    purpose: str = "code_gen",
    generation: int = -1,
    submission_id: str = "",
) -> tuple[str, dict]:
    """Call Anthropic Messages API directly (no OpenRouter middleman)."""
    tracker = get_tracker()
    if tracker.is_exhausted:
        raise RuntimeError(f"Budget exhausted (${tracker.total_cost:.4f} / ${tracker.budget.max_budget_usd:.2f})")

    # Anthropic requires system as a top-level param, not in messages
    system_text = ""
    api_messages = []
    for m in messages:
        if m["role"] == "system":
            system_text += m.get("content", "") + "\n"
        else:
            api_messages.append({"role": m["role"], "content": m["content"]})

    # Strip the vendor prefix for the Anthropic API
    bare_model = model_id.split("/", 1)[-1] if "/" in model_id else model_id

    body = {
        "model": bare_model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": api_messages,
    }
    if system_text.strip():
        body["system"] = system_text.strip()

    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latency_ms = (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic {e.code}: {body_text[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Anthropic network error: {e.reason}")

    # Anthropic returns content as a list of blocks
    content_blocks = data.get("content", [])
    content = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    usage = data.get("usage", {})

    prompt_text = " ".join(m.get("content", "") for m in messages)
    trace = RequestTrace(
        timestamp=time.time(),
        model_id=model_id,
        provider_name=provider_name or "claude",
        vendor="anthropic",
        purpose=purpose,
        input_tokens=usage.get("input_tokens", estimate_tokens(prompt_text)),
        output_tokens=usage.get("output_tokens", estimate_tokens(content)),
        total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        cost_usd=0,
        api_latency_ms=latency_ms,
        generation=generation,
        submission_id=submission_id,
        prompt_chars=len(prompt_text),
        response_chars=len(content),
    )
    tracker.record(trace)

    return content, {
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "cost_usd": trace.cost_usd,
        "latency_ms": latency_ms,
        "vendor": "anthropic",
    }


def call_openai_direct(
    model_id: str,
    messages: list[dict],
    api_key: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    provider_name: str = "",
    purpose: str = "code_gen",
    generation: int = -1,
    submission_id: str = "",
) -> tuple[str, dict]:
    """Call OpenAI Chat Completions API directly."""
    tracker = get_tracker()
    if tracker.is_exhausted:
        raise RuntimeError(f"Budget exhausted (${tracker.total_cost:.4f} / ${tracker.budget.max_budget_usd:.2f})")

    bare_model = model_id.split("/", 1)[-1] if "/" in model_id else model_id

    payload = json.dumps({
        "model": bare_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENAI_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latency_ms = (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI {e.code}: {body_text[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI network error: {e.reason}")

    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})

    prompt_text = " ".join(m.get("content", "") for m in messages)
    trace = RequestTrace(
        timestamp=time.time(),
        model_id=model_id,
        provider_name=provider_name or "gpt",
        vendor="openai",
        purpose=purpose,
        input_tokens=usage.get("prompt_tokens", estimate_tokens(prompt_text)),
        output_tokens=usage.get("completion_tokens", estimate_tokens(content)),
        total_tokens=usage.get("total_tokens", 0),
        cost_usd=0,
        api_latency_ms=latency_ms,
        generation=generation,
        submission_id=submission_id,
        prompt_chars=len(prompt_text),
        response_chars=len(content),
    )
    trace.total_tokens = trace.input_tokens + trace.output_tokens
    tracker.record(trace)

    return content, {
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "cost_usd": trace.cost_usd,
        "latency_ms": latency_ms,
        "vendor": "openai",
    }


def call_google_direct(
    model_id: str,
    messages: list[dict],
    api_key: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    provider_name: str = "",
    purpose: str = "code_gen",
    generation: int = -1,
    submission_id: str = "",
) -> tuple[str, dict]:
    """Call Google AI Gemini via their OpenAI-compatible endpoint."""
    tracker = get_tracker()
    if tracker.is_exhausted:
        raise RuntimeError(f"Budget exhausted (${tracker.total_cost:.4f} / ${tracker.budget.max_budget_usd:.2f})")

    bare_model = model_id.split("/", 1)[-1] if "/" in model_id else model_id

    payload = json.dumps({
        "model": bare_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    endpoint = f"{GOOGLE_AI_URL}?key={api_key}"
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latency_ms = (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Google AI {e.code}: {body_text[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Google AI network error: {e.reason}")

    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})

    prompt_text = " ".join(m.get("content", "") for m in messages)
    trace = RequestTrace(
        timestamp=time.time(),
        model_id=model_id,
        provider_name=provider_name or "gemini",
        vendor="google",
        purpose=purpose,
        input_tokens=usage.get("prompt_tokens", estimate_tokens(prompt_text)),
        output_tokens=usage.get("completion_tokens", estimate_tokens(content)),
        total_tokens=usage.get("total_tokens", 0),
        cost_usd=0,
        api_latency_ms=latency_ms,
        generation=generation,
        submission_id=submission_id,
        prompt_chars=len(prompt_text),
        response_chars=len(content),
    )
    trace.total_tokens = trace.input_tokens + trace.output_tokens
    tracker.record(trace)

    return content, {
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "cost_usd": trace.cost_usd,
        "latency_ms": latency_ms,
        "vendor": "google",
    }


def call_deepseek_direct(
    model_id: str,
    messages: list[dict],
    api_key: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    provider_name: str = "",
    purpose: str = "code_gen",
    generation: int = -1,
    submission_id: str = "",
) -> tuple[str, dict]:
    """Call DeepSeek Chat API directly."""
    tracker = get_tracker()
    if tracker.is_exhausted:
        raise RuntimeError(f"Budget exhausted (${tracker.total_cost:.4f} / ${tracker.budget.max_budget_usd:.2f})")

    bare_model = model_id.split("/", 1)[-1] if "/" in model_id else model_id

    payload = json.dumps({
        "model": bare_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latency_ms = (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek {e.code}: {body_text[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"DeepSeek network error: {e.reason}")

    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})

    prompt_text = " ".join(m.get("content", "") for m in messages)
    trace = RequestTrace(
        timestamp=time.time(),
        model_id=model_id,
        provider_name=provider_name or "deepseek",
        vendor="deepseek",
        purpose=purpose,
        input_tokens=usage.get("prompt_tokens", estimate_tokens(prompt_text)),
        output_tokens=usage.get("completion_tokens", estimate_tokens(content)),
        total_tokens=usage.get("total_tokens", 0),
        cost_usd=0,
        api_latency_ms=latency_ms,
        generation=generation,
        submission_id=submission_id,
        prompt_chars=len(prompt_text),
        response_chars=len(content),
    )
    trace.total_tokens = trace.input_tokens + trace.output_tokens
    tracker.record(trace)

    return content, {
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "cost_usd": trace.cost_usd,
        "latency_ms": latency_ms,
        "vendor": "deepseek",
    }


# ── Smart Router Dispatch ────────────────────────────────────────────────────

_VENDOR_CALLERS = {
    "anthropic": call_anthropic,
    "openai":    call_openai_direct,
    "google":    call_google_direct,
    "deepseek":  call_deepseek_direct,
}


def call_model(
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
    Unified entry point with SMART ROUTING:
      1. Local models → Ollama
      2. Direct vendor key available → call vendor API directly
      3. Otherwise → OpenRouter fallback
    """
    if _is_local_model(provider_name):
        return call_ollama_chat(
            model_id, messages,
            max_tokens=max_tokens, temperature=temperature,
            provider_name=provider_name, purpose=purpose,
        )

    vendor, api_key = _resolve_vendor(model_id)
    caller = _VENDOR_CALLERS.get(vendor)

    if caller:
        return caller(
            model_id, messages, api_key,
            max_tokens=max_tokens, temperature=temperature,
            provider_name=provider_name, purpose=purpose,
            generation=generation, submission_id=submission_id,
        )

    # Default: OpenRouter
    return call_openrouter(
        model_id, messages,
        max_tokens=max_tokens, temperature=temperature,
        provider_name=provider_name, purpose=purpose,
        generation=generation, submission_id=submission_id,
    )


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

    referer = os.environ.get("FORGE_HTTP_REFERER", "https://github.com/ModernOps888/the-forge")
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": referer,
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
        vendor="openrouter",
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
        "vendor": "openrouter",
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


def ollama_provider(model_name: str):
    """Create a Forge-compatible local provider hitting Ollama API."""
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
        payload = json.dumps({
            "model": model_id,
            "messages": [
                {"role": "system", "content": CODE_SYSTEM},
                {"role": "user", "content": prompt}
            ],
            "stream": False,
            "options": {"temperature": 0.7, "num_ctx": 8192}
        }).encode("utf-8")

        req = urllib.request.Request(
            "http://localhost:11434/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["message"]["content"]
        except Exception as e:
            raise RuntimeError(f"Ollama local error ({model_id}): {e}")

        return _clean_code(content)

    return generate


def make_all_providers() -> dict[str, object]:
    providers = {}
    for name in MODELS:
        if name.endswith("-local"):
            providers[name] = ollama_provider(name)
        else:
            providers[name] = openrouter_provider(name)
    return providers


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

    content, usage = call_model(
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
            content, usage = call_model(
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

    synth_model = MODELS.get("gemini", "google/gemini-2.5-pro")  # used for synthesis
    messages = [
        {"role": "system", "content": "You are a synthesis agent. Merge multiple AI responses into one authoritative answer."},
        {"role": "user", "content": synthesis_prompt},
    ]
    merged, _ = call_model(
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
        model = "gemini"      # cheapest cloud model
    elif complexity == "medium":
        model = "gpt-mini"    # mid-tier
    else:
        model = "claude"      # premium

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
