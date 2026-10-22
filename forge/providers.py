"""
The Forge — Multi-Vendor LLM Providers with Smart Routing

Smart routing architecture:
  1. If a direct vendor API key is set (e.g. ANTHROPIC_API_KEY), call the vendor directly.
  2. If not, fall back to OpenRouter as a universal relay.
  3. Local models always route to Ollama.

Supported frontier families (October 2026):
  Claude Sonnet 5.5, Claude Opus 5.5, Claude Fable 5.1, Claude Mythos 5,
  GPT-6 Astra, GPT-6.1 Sol, o3 / o3-mini, Gemini 4 Argon, Gemini 3.8 Pro/Flash,
  DeepSeek (V3 / R1), Grok 4.7, Llama 3.3 70B, and local Ollama models.

Every request is traced for cost, token usage, and latency via the BudgetTracker.

Modes:
  - Code generation: providers for the evolution arena
  - Chat: multi-agent research/conversation
  - Auto-route: cheapest model that can handle the task
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .budget import RequestTrace, get_tracker, estimate_tokens


# ── Automatic .env Loader (Zero-Dependency) ──────────────────────────────────

def _load_dotenv() -> None:
    """Load key=value pairs from <repo_root>/.env into os.environ if not already set."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
                val = val[1:-1]
            elif " #" in val:
                val = val.split(" #", 1)[0].strip()
            if key and key not in os.environ and val:
                os.environ[key] = val
    except Exception:
        pass


_load_dotenv()


# ── Vendor Endpoints ─────────────────────────────────────────────────────────

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_URL  = "https://api.anthropic.com/v1/messages"
OPENAI_URL     = "https://api.openai.com/v1/chat/completions"
DEEPSEEK_URL   = "https://api.deepseek.com/chat/completions"
GOOGLE_AI_URL  = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
XAI_URL        = "https://api.x.ai/v1/chat/completions"


def _get_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "")


def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


# ── Vendor Key Resolution ────────────────────────────────────────────────────

# Maps model_id prefix → (env var name, direct call function name)
_VENDOR_MAP = {
    "anthropic/": ("ANTHROPIC_API_KEY", "anthropic"),
    "openai/":    ("OPENAI_API_KEY",    "openai"),
    "google/":    ("GOOGLE_AI_API_KEY", "google"),
    "deepseek/":  ("DEEPSEEK_API_KEY",  "deepseek"),
    "x-ai/":      ("XAI_API_KEY",       "xai"),
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


def _pick_available_model(preferred: str, tier: str = "simple") -> str:
    """Pick `preferred` if its API key (or OpenRouter) is available, else fallback to an active vendor."""
    if _get_key():
        return preferred

    model_id = MODELS.get(preferred, preferred)
    for prefix, (env_var, _) in _VENDOR_MAP.items():
        if model_id.startswith(prefix) and os.environ.get(env_var, ""):
            return preferred

    # Check which direct vendor keys are configured
    tier_fallbacks = {
        "simple": [
            ("GOOGLE_AI_API_KEY", "gemini-lite"),
            ("DEEPSEEK_API_KEY", "deepseek"),
            ("OPENAI_API_KEY", "gpt-mini"),
            ("ANTHROPIC_API_KEY", "claude-haiku"),
            ("XAI_API_KEY", "grok"),
        ],
        "medium": [
            ("OPENAI_API_KEY", "gpt-mini"),
            ("GOOGLE_AI_API_KEY", "gemini-pro"),
            ("DEEPSEEK_API_KEY", "deepseek"),
            ("ANTHROPIC_API_KEY", "claude"),
            ("XAI_API_KEY", "grok"),
        ],
        "hard": [
            ("ANTHROPIC_API_KEY", "claude"),
            ("OPENAI_API_KEY", "gpt"),
            ("GOOGLE_AI_API_KEY", "gemini"),
            ("DEEPSEEK_API_KEY", "deepseek-r1"),
            ("XAI_API_KEY", "grok"),
        ],
    }
    for env_var, candidate in tier_fallbacks.get(tier, tier_fallbacks["simple"]):
        if os.environ.get(env_var, ""):
            return candidate
    return preferred


def get_vendor_status() -> dict[str, dict]:
    """Return connection status for every vendor — used by /api/vendor-health."""
    vendors = {
        "openrouter":  {"key_env": "OPENROUTER_API_KEY",  "endpoint": OPENROUTER_URL},
        "anthropic":   {"key_env": "ANTHROPIC_API_KEY",   "endpoint": ANTHROPIC_URL},
        "openai":      {"key_env": "OPENAI_API_KEY",      "endpoint": OPENAI_URL},
        "google":      {"key_env": "GOOGLE_AI_API_KEY",   "endpoint": GOOGLE_AI_URL},
        "deepseek":    {"key_env": "DEEPSEEK_API_KEY",    "endpoint": DEEPSEEK_URL},
        "xai":         {"key_env": "XAI_API_KEY",         "endpoint": XAI_URL},
        "ollama":      {"key_env": None,                  "endpoint": f"{_ollama_host()}/api/chat"},
    }
    result = {}
    for name, info in vendors.items():
        has_key = bool(os.environ.get(info["key_env"], "")) if info["key_env"] else True
        result[name] = {"active": has_key, "endpoint": info["endpoint"]}
    result["_compute"] = detect_compute_hardware()
    return result


# ── Hardware Detection & Compute Mode ────────────────────────────────────────

_STATIC_HW_CACHE: Optional[dict] = None


def _get_compute_mode() -> str:
    """Get configured compute mode.
    
    Values:
      auto  — detect GPU, fallback to CPU (default)
      gpu   — force GPU (all layers)
      cpu   — force CPU only (num_gpu=0)
      split — split across GPU+CPU (num_gpu=half)
    """
    return os.environ.get("FORGE_COMPUTE", "auto").lower()


def _detect_windows_ram_gb() -> float:
    """Query total physical memory on Windows via kernel32 GlobalMemoryStatusEx."""
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return round(stat.ullTotalPhys / (1024 ** 3), 1)
    except Exception:
        pass
    return 0.0


def detect_compute_hardware() -> dict:
    """Detect available compute hardware for local inference.
    
    Returns a dict with GPU info, CPU info, and recommended compute mode.
    Caches static hardware queries (RAM, GPUs) so repeated calls are sub-millisecond.
    """
    global _STATIC_HW_CACHE
    if _STATIC_HW_CACHE is None:
        gpus = []
        ram_gb = 0.0

        # Detect RAM
        try:
            import psutil
            ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        except ImportError:
            if os.name == "nt":
                ram_gb = _detect_windows_ram_gb()

        # Detect NVIDIA GPUs
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"],
                shell=False, text=True, timeout=3, stderr=subprocess.DEVNULL,
            )
            for i, line in enumerate(out.strip().split("\n")):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    gpus.append({
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
        if not gpus:
            try:
                out = subprocess.check_output(
                    ["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--csv"],
                    shell=False, text=True, timeout=3, stderr=subprocess.DEVNULL,
                )
                if "GPU" in out:
                    gpus.append({"index": 0, "name": "AMD ROCm GPU", "vendor": "amd"})
            except Exception:
                pass

        # Detect Apple Silicon (macOS unified memory)
        if not gpus and sys.platform == "darwin":
            try:
                out = subprocess.check_output(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    shell=False, text=True, timeout=3, stderr=subprocess.DEVNULL,
                )
                if "Apple" in out:
                    gpus.append({
                        "index": 0,
                        "name": out.strip(),
                        "vram_total_mb": int(ram_gb * 1024),
                        "vendor": "apple",
                    })
            except Exception:
                pass

        _STATIC_HW_CACHE = {
            "gpus": gpus,
            "cpu_cores": os.cpu_count() or 1,
            "ram_gb": ram_gb,
            "platform": sys.platform,
        }

    hw = {
        "gpus": list(_STATIC_HW_CACHE["gpus"]),
        "cpu_cores": _STATIC_HW_CACHE["cpu_cores"],
        "ram_gb": _STATIC_HW_CACHE["ram_gb"],
        "compute_mode": _get_compute_mode(),
        "platform": _STATIC_HW_CACHE["platform"],
    }

    # Auto-resolve compute mode
    if hw["compute_mode"] == "auto":
        if hw["gpus"]:
            hw["resolved_mode"] = "gpu"
        else:
            hw["resolved_mode"] = "cpu"
    else:
        hw["resolved_mode"] = hw["compute_mode"]

    # Compute num_gpu for Ollama
    if hw["resolved_mode"] == "cpu":
        hw["ollama_num_gpu"] = 0
    elif hw["resolved_mode"] == "split":
        hw["ollama_num_gpu"] = 999 // 2
    else:
        hw["ollama_num_gpu"] = 999

    return hw


# ── Model Registry (October 2026 Frontier Horizon) ───────────────────────────

MODELS = {
    # Ultra / Elite tier
    "claude-fable":  "anthropic/claude-fable-5-1",
    "claude-mythos": "anthropic/claude-mythos-5",
    "claude-opus":   "anthropic/claude-opus-5-5",
    # Premium tier
    "claude":        "anthropic/claude-sonnet-5-5",
    "gpt":           "openai/gpt-6-astra",
    "gpt-sol":       "openai/gpt-6.1-sol",
    "gemini":        "google/gemini-4-argon",
    "gemini-pro":    "google/gemini-3.8-pro",
    "grok":          "x-ai/grok-4.7",
    "deepseek":      "deepseek/deepseek-chat",
    "deepseek-r1":   "deepseek/deepseek-r1",
    "o3":            "openai/o3",
    "o3-mini":       "openai/o3-mini",
    # Fast / Economy tier
    "claude-haiku":  "anthropic/claude-haiku-5-5",
    "gpt-mini":      "openai/gpt-5",
    "gpt-nano":      "openai/o3-mini",
    "gemini-lite":   "google/gemini-3.8-flash",
    "llama-3.3":     "meta-llama/llama-3.3-70b-instruct",
    # Local models (run via Ollama — GPU, CPU, or Apple Silicon)
    "qwen-local":    "qwen2.5-coder:7b",
    "llama-local":   "llama3.2:latest",
    "codellama":     "codellama:7b",
    "mistral-local": "mistral:latest",
    "phi-local":     "phi3:mini",
}

PRIMARY_CLOUD_MODELS = ["claude", "gpt", "gemini", "deepseek"]

MODEL_TIERS = {
    "ultra":   ["claude-fable", "claude-mythos", "claude-opus"],
    "elite":   ["claude-fable", "claude-mythos", "claude-opus"],
    "premium": ["claude", "gpt", "gpt-sol", "gemini", "gemini-pro", "grok", "deepseek-r1", "o3", "o3-mini"],
    "mid":     ["gpt-mini", "llama-3.3", "claude-haiku"],
    "fast":    ["claude-haiku", "gemini-lite", "deepseek", "gpt-mini", "gpt-nano", "llama-3.3"],
    "cheap":   ["gemini-lite", "deepseek", "gpt-nano",
                "qwen-local", "llama-local", "codellama", "mistral-local", "phi-local"],
    "local":   ["qwen-local", "llama-local", "codellama", "mistral-local", "phi-local"],
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
3. If you write code that should be pinned to the Workspace Canvas, output it in standard ` ```python ` blocks as usual.
4. Run commands sequentially if needed. I will keep feeding you the `stdout` until you provide your final conversational answer without an `<execute_powershell>` tag.
5. NEVER attempt to launch interactive shells, browsers, VS Code, or isolated `antigravity.cmd` scripts. 
6. To send a message, instruction, or data payload natively to the Antigravity system, you MUST output your message wrapped in exactly `<send_antigravity>...</send_antigravity>`. I will intercept this and drop it directly into the native JSON inbox directory that Antigravity is actively monitoring.
"""


# ── Core API Call (with full telemetry) ──────────────────────────────────────

def _is_local_model(provider_name: str) -> bool:
    """Check if a model name refers to a local Ollama model."""
    if not provider_name:
        return False
    if provider_name.endswith("-local") or provider_name == "codellama":
        return True
    if ":" in provider_name and "/" not in provider_name:
        return True
    model_id = MODELS.get(provider_name, "")
    return ":" in model_id and "/" not in model_id


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
    except urllib.error.URLError as e:
        if "Connection refused" in str(e) or "10061" in str(e):
            raise RuntimeError(
                f"Ollama is not running. Start it with: ollama serve\n"
                f"Then pull the model: ollama pull {model_id}"
            )
        raise RuntimeError(f"Ollama connection error ({model_id}): {e}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code == 404 or "not found" in body.lower():
            raise RuntimeError(
                f"Model '{model_id}' not found in Ollama. Pull it with:\n"
                f"  ollama pull {model_id}"
            )
        raise RuntimeError(f"Ollama HTTP {e.code} ({model_id}): {body[:200]}")
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

    # Claude 5-family / Opus 4.7+ reject sampling params (temperature/top_p → 400),
    # so the direct Anthropic path never sends them; steering is prompt-only.
    body = {
        "model": bare_model,
        "max_tokens": max_tokens,
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
    def _do_request():
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = _retry_with_backoff(_do_request)
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


# ── Retry Helper ─────────────────────────────────────────────────────────

_RETRYABLE_CODES = {429, 500, 502, 503}


def _retry_with_backoff(fn, max_attempts: int = 3, initial_delay: float = 1.0):
    """Execute `fn()` with exponential backoff on transient HTTP errors."""
    delay = initial_delay
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code not in _RETRYABLE_CODES or attempt == max_attempts:
                raise
            last_err = e
            time.sleep(delay)
            delay *= 2
        except urllib.error.URLError:
            if attempt == max_attempts:
                raise
            time.sleep(delay)
            delay *= 2
    raise last_err  # pragma: no cover


# ── Common OpenAI-Compatible Vendor Caller ───────────────────────────────

def _call_openai_compatible(
    endpoint: str,
    model_id: str,
    messages: list[dict],
    api_key: str,
    vendor_name: str,
    headers: dict | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    provider_name: str = "",
    purpose: str = "code_gen",
    generation: int = -1,
    submission_id: str = "",
    timeout: int = 90,
) -> tuple[str, dict]:
    """Shared caller for OpenAI-compatible APIs (OpenAI, Google, DeepSeek, xAI)."""
    tracker = get_tracker()
    if tracker.is_exhausted:
        raise RuntimeError(f"Budget exhausted (${tracker.total_cost:.4f} / ${tracker.budget.max_budget_usd:.2f})")

    bare_model = model_id.split("/", 1)[-1] if "/" in model_id else model_id

    # Reasoning models (o1, o3, o4 families) require max_completion_tokens and reject temperature
    if bare_model.startswith(("o1", "o3", "o4")):
        body = {
            "model": bare_model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
    else:
        body = {
            "model": bare_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    payload = json.dumps(body).encode("utf-8")

    req_headers = {
        "Content-Type": "application/json",
    }
    if "key=" not in endpoint and api_key:
        req_headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        for k, v in headers.items():
            if v is None:
                req_headers.pop(k, None)
            else:
                req_headers[k] = v

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers=req_headers,
        method="POST",
    )

    t0 = time.time()
    def _do_request():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = _retry_with_backoff(_do_request)
        latency_ms = (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{vendor_name} {e.code}: {body_text[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"{vendor_name} network error: {e.reason}")

    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})

    prompt_text = " ".join(m.get("content", "") for m in messages)
    trace = RequestTrace(
        timestamp=time.time(),
        model_id=model_id,
        provider_name=provider_name or vendor_name,
        vendor=vendor_name,
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
        "vendor": vendor_name,
    }


# ── Direct Vendor Callers (thin wrappers) ────────────────────────────────

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
    return _call_openai_compatible(
        OPENAI_URL, model_id, messages, api_key, "openai",
        max_tokens=max_tokens, temperature=temperature,
        provider_name=provider_name or "gpt", purpose=purpose,
        generation=generation, submission_id=submission_id,
    )


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
    """Call Google AI Gemini via their OpenAI-compatible endpoint using Bearer auth."""
    return _call_openai_compatible(
        GOOGLE_AI_URL, model_id, messages, api_key, "google",
        max_tokens=max_tokens, temperature=temperature,
        provider_name=provider_name or "gemini", purpose=purpose,
        generation=generation, submission_id=submission_id,
    )


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
    return _call_openai_compatible(
        DEEPSEEK_URL, model_id, messages, api_key, "deepseek",
        max_tokens=max_tokens, temperature=temperature,
        provider_name=provider_name or "deepseek", purpose=purpose,
        generation=generation, submission_id=submission_id,
    )


def call_xai_direct(
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
    """Call xAI Grok Chat API directly."""
    return _call_openai_compatible(
        XAI_URL, model_id, messages, api_key, "xai",
        max_tokens=max_tokens, temperature=temperature,
        provider_name=provider_name or "grok", purpose=purpose,
        generation=generation, submission_id=submission_id,
    )


# ── Smart Router Dispatch ────────────────────────────────────────────────────

_VENDOR_CALLERS = {
    "anthropic": call_anthropic,
    "openai":    call_openai_direct,
    "google":    call_google_direct,
    "deepseek":  call_deepseek_direct,
    "xai":       call_xai_direct,
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
    if _is_local_model(provider_name) or _is_local_model(model_id):
        resolved_local = MODELS.get(model_id, model_id)
        return call_ollama_chat(
            resolved_local, messages,
            max_tokens=max_tokens, temperature=temperature,
            provider_name=provider_name or model_id, purpose=purpose,
        )

    model_id = MODELS.get(model_id, model_id)
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
    def _do_request():
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = _retry_with_backoff(_do_request)
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
    """Create a Forge-compatible provider for the evolution arena (uses smart routing)."""
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
        content, usage = call_model(
            model_id, messages,
            max_tokens=1024, temperature=0.7,
            provider_name=model_name, purpose="code_gen",
        )
        return _clean_code(content)

    return generate


def ollama_provider(model_name: str):
    """Create a Forge-compatible local provider hitting Ollama API with telemetry & OLLAMA_HOST."""
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
        content, _ = call_ollama_chat(
            model_id, messages,
            max_tokens=2048, temperature=0.7,
            provider_name=model_name, purpose="arena_generation",
        )
        return _clean_code(content)

    return generate


def make_all_providers() -> dict[str, object]:
    providers = {}
    for name in MODELS:
        if _is_local_model(name):
            providers[name] = ollama_provider(name)
        else:
            providers[name] = openrouter_provider(name)
    return providers


# ── Multi-Agent Chat ──────────────────────────────────────────────────────────

def chat(query: str, model_name: str = "gemini-lite", history: list[dict] | None = None) -> str:
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
    Defaults to the 4 primary cloud models (claude, gpt, gemini, deepseek).
    Returns {model_name: response_text}.
    """
    models = models or list(PRIMARY_CLOUD_MODELS)
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
    Ask multiple models the same question, then ask a synthesizer to merge their answers.
    Returns {"individual": {model: answer}, "consensus": merged_answer, "cost_usd": total_cost}.
    """
    tracker = get_tracker()
    cost_before = tracker.total_cost

    # Phase 1: ask each model (defaults to the 4 primary cloud models)
    individual = multi_agent_research(query, models)

    # Phase 2: synthesize using an available model
    synthesis_prompt = f"Original question: {query}\n\n"
    for name, answer in individual.items():
        synthesis_prompt += f"=== {name.upper()} ===\n{answer[:1500]}\n\n"
    synthesis_prompt += (
        "Synthesize the above into a single, comprehensive answer. "
        "Note where models agree and where they diverge. Be precise."
    )

    synth_name = _pick_available_model("gemini-lite", tier="simple")
    synth_model = MODELS.get(synth_name, "google/gemini-3.8-flash")
    messages = [
        {"role": "system", "content": "You are a synthesis agent. Merge multiple AI responses into one authoritative answer."},
        {"role": "user", "content": synthesis_prompt},
    ]
    merged, _ = call_model(
        synth_model, messages,
        max_tokens=2048, temperature=0.3,
        provider_name=synth_name, purpose="synthesis",
    )

    cost_delta = round(tracker.total_cost - cost_before, 6)
    return {
        "individual": individual,
        "consensus": merged,
        "cost_usd": cost_delta,
        "cost": cost_delta,
    }


# ── Smart Router ──────────────────────────────────────────────────────────────

def auto_route(query: str, complexity: str = "auto") -> str:
    """
    Automatically pick the cheapest available model that can handle the task.
    complexity: "simple" / "medium" / "hard" / "auto"
    """
    if complexity == "auto":
        if len(query) > 500 or any(kw in query.lower() for kw in ("architect", "design", "complex", "optimize")):
            complexity = "hard"
        elif len(query) > 200:
            complexity = "medium"
        else:
            complexity = "simple"

    if complexity == "simple":
        preferred = "gemini-lite"  # cheapest cloud model
    elif complexity == "medium":
        preferred = "gpt-mini"     # mid-tier
    else:
        preferred = "claude"       # premium

    model = _pick_available_model(preferred, tier=complexity)
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
