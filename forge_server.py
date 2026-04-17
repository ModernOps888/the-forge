#!/usr/bin/env python3
"""
The Forge — Live API Server

HTTP API that powers the interactive dashboard.
All requests flow through budget tracking, governance, and provenance.

Endpoints:
  POST /api/chat           — Send message to any model
  POST /api/research       — Fan query to selected models  
  POST /api/consensus      — All models + synthesis
  POST /api/compile        — Compile .sl code via Vitalis
  GET  /api/budget         — Cost & token report
  GET  /api/models         — Available models + pricing
  GET  /api/provenance     — Governance audit trail
  GET  /api/health         — System health check
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import traceback
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

# Add forge to path
sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# ── Windows CP1252 fix: reconfigure stdout/stderr to UTF-8 at runtime ─────
# PYTHONIOENCODING only works if set before Python starts.
# This ensures all emoji/unicode print() calls across the entire forge package
# never crash with UnicodeEncodeError on Windows terminals.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("forge")

from forge.providers import (
    call_openrouter, call_model, MODELS, CHAT_SYSTEM, RESEARCH_SYSTEM, COWORK_SYSTEM,
    _clean_code, CODE_SYSTEM, ollama_provider, openrouter_provider,
    _is_local_model, get_vendor_status, _resolve_vendor, detect_compute_hardware,
)
from forge.budget import get_tracker, BudgetConfig, RequestTrace, PRICING
from forge.governance import get_provenance, get_policy_engine, CircuitBreaker
from forge.compiler import compile_and_run, vitalis_version
from forge.fitness_engine import score as fitness_score
from forge.factory import build as factory_build, ARTIFACT_TYPES, LANG_MAP
from forge.marketplace import get_marketplace
from forge.deployer import deploy as factory_deploy
from forge.memory import get_memory
from forge.tracer import get_tracer

# ── Globals ───────────────────────────────────────────────────────────────────

_SERVER_INSTANCE = None
DASHBOARD_DIR = Path(__file__).parent / "dashboard"
TRACKER = get_tracker(BudgetConfig(max_budget_usd=50.0))
PROVENANCE = get_provenance()
BREAKER = CircuitBreaker(failure_threshold=5, cooldown_seconds=30)
CHAT_HISTORIES: dict[str, list[dict]] = {}  # session_id -> messages
MEMORY = get_memory()  # Three-tier memory: working + episodic + semantic skills

# Record server start
PROVENANCE.record("system_start", "forge_server", "system", {
    "vitalis_version": "loading...",
    "models": list(MODELS.keys()),
    "budget_usd": 50.0,
})


# ── Request Handler ───────────────────────────────────────────────────────────

class ForgeHandler(SimpleHTTPRequestHandler):
    """Serve dashboard + API endpoints."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def log_request_custom(self, path: str, status: int, duration_ms: float):
        log.info("%-6s %-40s %d  %.1fms", self.command, path, status, duration_ms)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        t0 = time.monotonic()
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self._handle_api_get(path)
        else:
            # Serve static files from dashboard
            if path == "/":
                self.path = "/index.html"
            super().do_GET()
        self.log_request_custom(path, 200, (time.monotonic() - t0) * 1000)

    def do_POST(self):
        t0 = time.monotonic()
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self._handle_api_post(path)
        else:
            self.send_error(404)
        self.log_request_custom(path, 200, (time.monotonic() - t0) * 1000)

    # ── GET APIs ──────────────────────────────────────────────────────────

    def _handle_api_get(self, path: str):
        try:
            if path == "/api/health":
                valid, _ = PROVENANCE.verify_integrity()
                vendors = get_vendor_status()
                active_vendors = [k for k, v in vendors.items() if v["active"]]
                self._json_response({
                    "status": "ok",
                    "vitalis": _safe_vitalis_version(),
                    "models": len(MODELS),
                    "budget_remaining": round(TRACKER.remaining_budget, 4),
                    "provenance_entries": PROVENANCE.length,
                    "provenance_integrity": valid,
                    "circuit_breaker": BREAKER.state,
                    "uptime_s": round(time.time() - _START_TIME, 1),
                    "artifact_types": len(ARTIFACT_TYPES),
                    "marketplace_items": len(get_marketplace().list_all()),
                    "active_vendors": active_vendors,
                })

            elif path == "/api/models":
                models_out = []
                for name, model_id in MODELS.items():
                    pricing = PRICING.get(model_id, {"input": 0, "output": 0})
                    # Determine vendor routing for this model
                    try:
                        vendor, _ = _resolve_vendor(model_id)
                    except RuntimeError:
                        vendor = "none"
                    models_out.append({
                        "name": name,
                        "model_id": model_id,
                        "input_cost_per_m": pricing.get("input", 0),
                        "output_cost_per_m": pricing.get("output", 0),
                        "vendor": vendor,
                    })
                self._json_response({"models": models_out})

            elif path == "/api/vendor-health":
                self._json_response(get_vendor_status())

            elif path == "/api/ide-targets":
                targets = [t.strip() for t in os.environ.get("FORGE_IDE_TARGETS", "antigravity,clipboard").split(",") if t.strip()]
                self._json_response({"targets": targets})

            elif path == "/api/compute":
                hw = detect_compute_hardware()
                self._json_response(hw)

            elif path == "/api/budget":
                provider_costs = TRACKER.cost_per_provider()
                self._json_response({
                    "total_cost": round(TRACKER.total_cost, 6),
                    "remaining": round(TRACKER.remaining_budget, 4),
                    "budget_max": TRACKER.budget.max_budget_usd,
                    "pct_used": round(TRACKER.pct_used * 100, 2),
                    "total_tokens": TRACKER.total_tokens,
                    "input_tokens": TRACKER._total_input_tokens,
                    "output_tokens": TRACKER._total_output_tokens,
                    "request_count": TRACKER._request_count,
                    "cost_ema": round(TRACKER._cost_ema, 6),
                    "providers": provider_costs,
                    "optimization_hints": TRACKER.optimization_hints(),
                    "is_exhausted": TRACKER.is_exhausted,
                })

            elif path == "/api/provenance":
                entries = []
                for e in PROVENANCE._entries[-50:]:
                    entries.append({
                        "timestamp": e.timestamp,
                        "event_type": e.event_type,
                        "entity_id": e.entity_id,
                        "actor": e.actor,
                        "hash": e.entry_hash[:16],
                    })
                valid, count = PROVENANCE.verify_integrity()
                self._json_response({
                    "entries": entries,
                    "total": PROVENANCE.length,
                    "integrity": valid,
                    "head": PROVENANCE.head[:16],
                })

            elif path == "/api/system":
                self._api_system()

            elif path == "/api/ag-inbox":
                self._api_ag_inbox()

            elif path == "/api/traces":
                traces = get_tracer().get_all_traces()
                # quick serialize
                res = []
                for t in traces:
                    res.append({
                        "id": t.id,
                        "session_id": t.session_id,
                        "task": t.task,
                        "status": t.status,
                        "steps": len(t.steps)
                    })
                self._json_response({"traces": res})

            elif path == "/api/hitl/pending":
                self._json_response({"pending": get_tracer().hitl.get_pending()})

            elif path == "/api/memory":
                from forge.memory import get_memory
                mem = get_memory()
                self._json_response(mem.stats())

            else:
                self._json_response({"error": "Unknown endpoint"}, 404)

        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    # ── POST APIs ─────────────────────────────────────────────────────────

    def _handle_api_post(self, path: str):
        try:
            if path == "/api/transcribe":
                self._api_transcribe()
                return

            body = self._read_body()

            if path == "/api/chat":
                self._api_chat(body)
            elif path == "/api/research":
                self._api_research(body)
            elif path == "/api/consensus":
                self._api_consensus(body)
            elif path == "/api/compile":
                self._api_compile(body)
            elif path == "/api/build":
                self._api_build(body)
            elif path == "/api/deploy":
                self._api_deploy(body)
            elif path == "/api/orchestrate":
                self._api_orchestrate(body)
            elif path == "/api/score":
                self._api_score(body)
            elif path == "/api/system":
                self._api_system()
            elif path == "/api/artifact-types":
                self._json_response({"types": {k: {"description": v, "language": LANG_MAP.get(k, "python")} for k, v in ARTIFACT_TYPES.items()}})
            elif path == "/api/export":
                self._api_export(body)
            elif path == "/api/native":
                self._api_native(body)
            elif path == "/api/cowork":
                self._api_cowork(body)
            elif path == "/api/self-improve":
                self._api_self_improve(body)
            elif path == "/api/quick-chat":
                self._api_quick_chat(body)
            elif path == "/api/analyze-file":
                self._api_analyze_file(body)
            elif path == "/api/hitl/resolve":
                self._api_hitl_resolve(body)
            else:
                self._json_response({"error": "Unknown endpoint"}, 404)

        except Exception as e:
            traceback.print_exc()
            self._json_response({"error": str(e)}, 500)

    def _trigger_reload(self):
        """Threaded hot-swap to drop the active TCP socket and execv a new server process."""
        import threading
        def restarter():
            time.sleep(1)  # Buffer to allow HTTP response to flush
            print("  [System] Initiating hot-swap process replacement...")
            global _SERVER_INSTANCE
            if _SERVER_INSTANCE:
                _SERVER_INSTANCE.shutdown()
                _SERVER_INSTANCE.server_close()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=restarter, daemon=True).start()

    def _api_self_improve(self, body: dict):
        """Self-improvement bridge using LINE-RANGE patching.
        
        Architecture:
          We ask the LLM to return patches as line-range replacements:
            [{"start_line": 10, "end_line": 15, "replacement": "new code here"}]
          
          Line numbers are integers (never break JSON), and the replacement
          content is the only string field — much safer than find/replace
          where both fields contain complex code.
          
          Fallback: if the model returns find/replace format, we handle that too.
          
          Safety layers:
            1. JSON repair (fix unterminated strings, trailing commas)
            2. AST dry-run before overwrite (Python only)
            3. Auto-backup before every write
            4. Hot-swap only when patching self
        """
        instruction = body.get("instruction")
        model = body.get("model", "claude")
        target_file = body.get("file", str(Path(__file__)))
        
        if not instruction:
            return self._json_response({"error": "No instruction provided"}, 400)
            
        target_path = Path(target_file)
        if not target_path.exists():
            return self._json_response({"error": f"Target file not found: {target_file}"}, 404)
            
        current_code = target_path.read_text(encoding="utf-8")
        code_lines = current_code.split("\n")
        total_lines = len(code_lines)
        
        # Build a NUMBERED excerpt so the model can reference line numbers
        # Show first 150 + last 40 lines with line numbers for large files
        if total_lines > 250:
            numbered_top = "\n".join(f"{i+1}: {line}" for i, line in enumerate(code_lines[:150]))
            numbered_bot = "\n".join(f"{i+1}: {line}" for i, line in enumerate(code_lines[-40:], total_lines - 40))
            excerpt = numbered_top + f"\n\n# ... [{total_lines - 190} lines omitted] ...\n\n" + numbered_bot
        else:
            excerpt = "\n".join(f"{i+1}: {line}" for i, line in enumerate(code_lines))
        
        system = (
            "You are the Forge Self-Improvement Engine. You receive a LINE-NUMBERED source file excerpt.\n"
            "Return a JSON array of patches. Each patch uses ONE of these formats:\n\n"
            "FORMAT A (preferred — line-range):\n"
            '  {"start_line": 10, "end_line": 12, "replacement": "new line 10\\nnew line 11\\nnew line 12"}\n\n'
            "FORMAT B (simple find/replace — use SHORT unique strings only):\n"
            '  {"find": "short unique text", "replace": "replacement text"}\n\n'
            "CRITICAL RULES:\n"
            "- Keep find strings SHORT (1-3 lines max) to avoid JSON escaping issues\n"
            "- Prefer FORMAT A with line numbers for multi-line changes\n"
            "- Return ONLY the JSON array. No markdown fences, no explanation.\n"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"File: {target_path.name} ({total_lines} lines)\n\n{excerpt}\n\nInstruction: {instruction}"}
        ]
        
        print(f"  [Self-Improve] Analyzing: {instruction} (model={model}, file={target_path.name})")
        from forge.providers import call_model, MODELS
        actual_model_id = MODELS.get(model, model)
        
        try:
            reply_text, usage = call_model(actual_model_id, messages, max_tokens=4096)
            
            # ── Parse with repair ──
            patches = self._parse_patch_json(reply_text)
            
            if patches is None:
                return self._json_response({
                    "error": "Could not parse model output as valid patches. Try a simpler instruction.",
                    "raw_preview": reply_text[:500],
                }, 400)
            
            # ── Apply patches ──
            patched_lines = list(code_lines)
            applied = 0
            skipped = []
            
            # Sort line-range patches by start_line descending (apply bottom-up to preserve line numbers)
            line_patches = [p for p in patches if "start_line" in p]
            find_patches = [p for p in patches if "find" in p and "start_line" not in p]
            
            # Apply line-range patches (bottom-up)
            for patch in sorted(line_patches, key=lambda p: p.get("start_line", 0), reverse=True):
                start = patch.get("start_line", 0) - 1  # 1-indexed to 0-indexed
                end = patch.get("end_line", start + 1) - 1
                replacement = patch.get("replacement", "")
                
                if start < 0 or end >= len(patched_lines) or start > end:
                    skipped.append(f"Line range {start+1}-{end+1} out of bounds")
                    continue
                
                new_lines = replacement.split("\n")
                patched_lines[start:end+1] = new_lines
                applied += 1
            
            # Apply find/replace patches on the joined result
            patched_code = "\n".join(patched_lines)
            for patch in find_patches:
                find_str = patch.get("find", "")
                replace_str = patch.get("replace", "")
                if not find_str or len(find_str) < 3:
                    skipped.append("Find string too short or empty")
                    continue
                if find_str not in patched_code:
                    skipped.append(f"'{find_str[:60]}...' not found in source")
                    continue
                patched_code = patched_code.replace(find_str, replace_str, 1)
                applied += 1
            
            if not find_patches:
                patched_code = "\n".join(patched_lines)
            
            if applied == 0:
                return self._json_response({
                    "error": "No patches matched. " + ("; ".join(skipped[:3]) if skipped else "Try a more specific instruction."),
                }, 400)
            
            # ── Syntax check (Python only) ──
            if target_path.suffix == ".py":
                try:
                    compile(patched_code, "<string>", "exec")
                except SyntaxError as e:
                    print(f"  [Self-Improve] SyntaxError blocked patch! {e}")
                    return self._json_response({"error": f"Rejected: SyntaxError in patched code: {e}"}, 400)
                
            # ── Downgrade Gateway (Fitness Sandbox) ────────────────────────
            # Enforces THREE rules before a patch can land:
            #   1. No regression: mutant score must not drop below baseline
            #   2. Quality floor: mutant must clear a minimum absolute threshold
            #   3. Tolerance: small fluctuations (<0.5 pts) are treated as neutral
            if target_path.suffix == ".py":
                try:
                    baseline_report = fitness_score(current_code, "python")
                    mutant_report   = fitness_score(patched_code, "python")

                    b_score = baseline_report.total          # 0-100
                    m_score = mutant_report.total

                    TOLERANCE   = 0.5   # ignore sub-0.5 point fluctuations
                    FLOOR_SCORE = 30.0  # absolute minimum quality — catch lobotomised patches

                    delta = m_score - b_score

                    # Rule 1 — no regression beyond tolerance
                    if delta < -TOLERANCE:
                        # Build per-dimension breakdown for transparency
                        dims = {}
                        for dim in ("correctness","performance","code_quality","robustness","efficiency","novelty"):
                            bv = getattr(baseline_report, dim, None)
                            mv = getattr(mutant_report,   dim, None)
                            if bv is not None and mv is not None:
                                diff = round(mv - bv, 2)
                                if diff < 0:
                                    dims[dim] = diff
                        print(f"  [Gate] BLOCKED — regression {b_score:.1f} -> {m_score:.1f} ({delta:+.1f})")
                        return self._json_response({
                            "error": (
                                f"DOWNGRADE BLOCKED: patch degrades quality by {abs(delta):.1f} pts "
                                f"({b_score:.1f} \u2192 {m_score:.1f}). "
                                f"Dropped dimensions: {dims or 'overall'}"
                            ),
                            "baseline": round(b_score, 2),
                            "mutant":   round(m_score, 2),
                            "delta":    round(delta, 2),
                            "dropped_dimensions": dims,
                        }, 400)

                    # Rule 2 — absolute quality floor
                    if m_score < FLOOR_SCORE:
                        print(f"  [Gate] BLOCKED — below quality floor ({m_score:.1f} < {FLOOR_SCORE})")
                        return self._json_response({
                            "error": (
                                f"QUALITY FLOOR BLOCKED: mutant score {m_score:.1f} is below "
                                f"minimum threshold of {FLOOR_SCORE}. Patch rejected."
                            ),
                            "baseline": round(b_score, 2),
                            "mutant":   round(m_score, 2),
                            "floor":    FLOOR_SCORE,
                        }, 400)

                    grade = mutant_report.grade if hasattr(mutant_report, "grade") else "?"
                    print(f"  [Gate] PASSED — {b_score:.1f} -> {m_score:.1f} ({delta:+.1f} pts, grade {grade})")

                except Exception as e:
                    # Non-fatal — log but allow patch through (fitness engine is advisory)
                    print(f"  [Gate] Sandbox evaluation failed (non-fatal): {e}")
            
            # ── Backup + Overwrite ──
            backup_path = target_path.with_suffix(target_path.suffix + ".bak")
            backup_path.write_text(current_code, encoding="utf-8")
            target_path.write_text(patched_code, encoding="utf-8")
            
            PROVENANCE.record("self_improve_patch", str(target_path.name), "system", {
                "instruction": instruction,
                "patches_applied": applied,
                "patches_total": len(patches),
                "skipped": len(skipped),
                "cost_usd": usage.get("cost_usd", 0),
            })
            
            print(f"  [Self-Improve] ✅ Applied {applied}/{len(patches)} patches to {target_path.name}" +
                  (f" ({len(skipped)} skipped)" if skipped else ""))
            
            response = {
                "ok": True,
                "patches_applied": applied,
                "patches_total": len(patches),
                "patches_skipped": len(skipped),
                "file": str(target_path),
                "cost_usd": round(usage.get("cost_usd", 0), 6),
            }
            
            if target_path.resolve() == Path(__file__).resolve():
                response["status"] = "Patch validated! Hot-swapping server process..."
                self._json_response(response)
                self._trigger_reload()
            else:
                response["status"] = f"Patch applied to {target_path.name} (no restart needed)"
                self._json_response(response)
            
        except Exception as e:
            traceback.print_exc()
            self._json_response({"error": str(e)}, 500)

    @staticmethod
    def _parse_patch_json(raw: str):
        """Parse LLM output into patch objects with aggressive repair.
        
        Handles:
          - Markdown code fences (```json ... ```)
          - Trailing commas
          - Unterminated strings (truncation)
          - Single top-level object (wraps in array)
        Returns list[dict] or None on total failure.
        """
        text = raw.strip()
        
        # Strip markdown fences
        if text.startswith("```"):
            first_nl = text.find("\n")
            text = text[first_nl+1:] if first_nl > 0 else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        
        # Attempt 1: direct parse
        try:
            result = json.loads(text)
            return [result] if isinstance(result, dict) else result
        except json.JSONDecodeError:
            pass
        
        # Attempt 2: fix trailing commas
        import re
        fixed = re.sub(r',\s*([}\]])', r'\1', text)
        try:
            result = json.loads(fixed)
            return [result] if isinstance(result, dict) else result
        except json.JSONDecodeError:
            pass
        
        # Attempt 3: truncated output — find the last complete object
        # Look for the last complete }] or } pattern
        last_bracket = text.rfind("}")
        if last_bracket > 0:
            # Try to close the array
            candidate = text[:last_bracket+1]
            if not candidate.rstrip().endswith("]"):
                candidate = candidate + "]"
            # Make sure it starts with [
            if not candidate.lstrip().startswith("["):
                candidate = "[" + candidate
            
            candidate = re.sub(r',\s*([}\]])', r'\1', candidate)
            try:
                result = json.loads(candidate)
                return [result] if isinstance(result, dict) else result
            except json.JSONDecodeError:
                pass
        
        # Attempt 4: extract individual JSON objects via regex
        obj_pattern = re.compile(r'\{[^{}]*\}', re.DOTALL)
        objects = obj_pattern.findall(text)
        parsed = []
        for obj_str in objects:
            try:
                parsed.append(json.loads(obj_str))
            except json.JSONDecodeError:
                continue
        if parsed:
            return parsed
        
        return None

    def _api_quick_chat(self, body: dict):
        """Stateless single-shot chat for desktop integration.
        No session history — just send a message, get a response.
        """
        model = body.get("model", "gemini")
        message = body.get("message", "")
        
        if not message or not message.strip():
            return self._json_response({"error": "message required"}, 400)
        
        if not BREAKER.can_proceed():
            return self._json_response({"error": "Circuit breaker open"}, 503)
        
        model_id = MODELS.get(model, model)
        messages = [
            {"role": "system", "content": CHAT_SYSTEM},
            {"role": "user", "content": message},
        ]
        
        try:
            t0 = time.time()
            content, usage = call_model(
                model_id, messages,
                max_tokens=2048, temperature=0.5,
                provider_name=model, purpose="chat",
            )
            latency = time.time() - t0
            BREAKER.record_success()
            
            self._json_response({
                "response": content,
                "model": model,
                "latency_ms": round(latency * 1000, 1),
                "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                "cost_usd": round(usage.get("cost_usd", 0), 6),
            })
        except Exception as e:
            BREAKER.record_failure()
            self._json_response({"error": str(e)}, 500)

    def _api_analyze_file(self, body: dict):
        """Analyze a local file with AI — used by desktop context menu integration."""
        file_path = body.get("file", "")
        model = body.get("model", "gemini")
        
        if not file_path:
            return self._json_response({"error": "file path required"}, 400)
        
        path = Path(file_path)
        if not path.exists():
            return self._json_response({"error": f"File not found: {file_path}"}, 404)
        
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return self._json_response({"error": f"Cannot read file: {e}"}, 400)
        
        # Truncate very large files
        if len(content) > 30000:
            content = content[:15000] + "\n\n... [TRUNCATED] ...\n\n" + content[-5000:]
        
        analysis_prompt = (
            f"Analyze this file: {path.name} ({path.suffix}, {len(content)} chars)\n\n"
            f"```\n{content}\n```\n\n"
            "Provide:\n"
            "1. What it does (purpose & architecture)\n"
            "2. Code quality score (1-10) with rationale\n"
            "3. Top 3 improvements\n"
            "4. Security concerns (if any)"
        )
        
        model_id = MODELS.get(model, model)
        messages = [
            {"role": "system", "content": "You are a senior code reviewer. Provide concise, actionable analysis."},
            {"role": "user", "content": analysis_prompt},
        ]
        
        try:
            t0 = time.time()
            response, usage = call_model(
                model_id, messages,
                max_tokens=2048, temperature=0.3,
                provider_name=model, purpose="analysis",
            )
            latency = time.time() - t0
            
            self._json_response({
                "file": str(path),
                "analysis": response,
                "model": model,
                "latency_ms": round(latency * 1000, 1),
                "cost_usd": round(usage.get("cost_usd", 0), 6),
            })
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _api_export(self, body: dict):
        """IDE-Agnostic Export Bridge.
        
        Dispatches Forge payloads to all configured IDE targets:
          - antigravity: JSON file → ~/.gemini/antigravity/brain/forge-inbox/
          - vscode:      JSON file → workspace .vscode/forge-export.json
          - cursor:      JSON file → workspace .cursor/forge-context/
          - windsurf:    JSON file → workspace .windsurf/forge-context/
          - clipboard:   PowerShell Set-Clipboard (universal fallback)
        """
        import subprocess
        
        payload_type = body.get("type", "unknown")
        title       = body.get("title", "Untitled")
        content     = body.get("content", {})
        source_model = body.get("model", "")

        # Build Markdown for clipboard
        md = f"**[FORGE NATIVE EXPORT: {payload_type.upper()}]**\n\n"
        md += f"**Title**: {title}\n"
        md += f"**Source Model**: {source_model}\n\n"
        
        raw = content.get("response") or content.get("code") or str(content)
        
        if payload_type == "build":
            md += f"```python\n{raw}\n```\n"
        elif payload_type == "orchestrate":
            md += f"```\n{raw}\n```\n"
        else:
            md += f"{raw}\n"
            
        md += "\n*Sent from The Forge Native Desktop Bridge.*"

        try:
            import json as _json
            from datetime import datetime
            from pathlib import Path

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_title = "".join([c if c.isalnum() else "_" for c in title[:30]])
            fname = f"{ts}_{payload_type}_{source_model}_{safe_title}.json"
            ide_payload = {
                "exported_at": datetime.now().isoformat(),
                "type": payload_type,
                "model": source_model,
                "title": title,
                "content": content
            }
            ide_json = _json.dumps(ide_payload, indent=2)

            targets = [t.strip() for t in os.environ.get("FORGE_IDE_TARGETS", "antigravity,clipboard").split(",") if t.strip()]
            delivered = []

            for target in targets:
                try:
                    if target == "antigravity":
                        inbox_dir = Path.home() / ".gemini" / "antigravity" / "brain" / "forge-inbox"
                        inbox_dir.mkdir(parents=True, exist_ok=True)
                        (inbox_dir / fname).write_text(ide_json, encoding="utf-8")
                        delivered.append("antigravity")

                    elif target == "vscode":
                        vsc_dir = Path.cwd() / ".vscode"
                        vsc_dir.mkdir(parents=True, exist_ok=True)
                        (vsc_dir / "forge-export.json").write_text(ide_json, encoding="utf-8")
                        delivered.append("vscode")

                    elif target == "cursor":
                        cur_dir = Path.cwd() / ".cursor" / "forge-context"
                        cur_dir.mkdir(parents=True, exist_ok=True)
                        (cur_dir / fname).write_text(ide_json, encoding="utf-8")
                        delivered.append("cursor")

                    elif target == "windsurf":
                        ws_dir = Path.cwd() / ".windsurf" / "forge-context"
                        ws_dir.mkdir(parents=True, exist_ok=True)
                        (ws_dir / fname).write_text(ide_json, encoding="utf-8")
                        delivered.append("windsurf")

                    elif target == "clipboard":
                        subprocess.run(
                            ["powershell", "-command", "Set-Clipboard", "-Value", "$input"],
                            input=md.encode('utf-8'), timeout=5,
                        )
                        delivered.append("clipboard")

                except Exception as e:
                    print(f"  [Export] Target '{target}' failed: {e}")

            PROVENANCE.record("export_ide_bridge", title, f"type:{payload_type}", {
                "targets": delivered, "file": fname,
            })
            self._json_response({"ok": True, "title": title, "file": fname, "delivered_to": delivered})

        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _api_native(self, body: dict):
        """Native OS Bridge for File Explorer, IDEs, and Terminals."""
        import os
        import subprocess
        from pathlib import Path
        
        action = body.get("action")
        target = body.get("target")
        
        if not action or not target:
            return self._json_response({"error": "action and target required"}, 400)
            
        try:
            target_path = Path(target)
            if not target_path.exists() and action in ["explorer", "ide"]:
                return self._json_response({"error": f"Path not found: {target}"}, 404)
                
            if action == "explorer":
                # Open Windows File Explorer focusing the directory or file
                os.startfile(target_path if target_path.is_dir() else target_path.parent)
                
            elif action == "ide":
                # Open in Visual Studio Code
                subprocess.run(["code", str(target_path)], shell=True)
                
            elif action == "terminal":
                # Open native PowerShell un-attached and run the target script immediately
                if target_path.is_file():
                    cmd = f'cd "{target_path.parent}"; python "{target_path.name}"'
                    subprocess.Popen(['start', 'powershell', '-NoExit', '-Command', cmd], shell=True)
                else:
                    cmd = f'cd "{target_path}"'
                    subprocess.Popen(['start', 'powershell', '-NoExit', '-Command', cmd], shell=True)
                    
            else:
                return self._json_response({"error": "unknown action"}, 400)
                
            PROVENANCE.record("native_bridge", action, "desktop", {"target": target})
            self._json_response({"ok": True, "action": action})
            
        except Exception as e:
            self._json_response({"error": f"Native execution failed: {str(e)}"}, 500)

    def _api_ag_inbox(self):
        """Read the contents of the Antigravity inbox."""
        inbox_dir = Path.home() / ".gemini" / "antigravity" / "brain" / "forge-inbox"
        if not inbox_dir.exists():
            return self._json_response({"files": []})
        
        files = []
        for file_path in sorted(inbox_dir.glob("*.json"), key=os.path.getmtime, reverse=True)[:50]:
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
                files.append(data)
            except Exception as e:
                print(f"Error reading inbox file {file_path}: {e}")
        self._json_response({"files": files})

    def _api_transcribe(self):
        """Dynamic JIT Voice-to-Text inference to safely share GPU with LLMs."""
        length = int(self.headers.get("Content-Length", 0))
        audio_data = self.rfile.read(length)
        
        import tempfile
        import os
        import torch
        import gc
        import traceback
        try:
            import faster_whisper
        except ImportError:
            return self._json_response({"error": "faster_whisper not installed"}, 500)

        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
            tmp.write(audio_data)
            temp_path = tmp.name

        text = ""
        try:
            print("  [STT] Loading faster-whisper JIT to GPU...")
            model = faster_whisper.WhisperModel("medium.en", device="cuda", compute_type="float16")
            
            segments, _ = model.transcribe(
                temp_path,
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
            )
            text = "".join([segment.text for segment in segments])
            
            # Aggressive VRAM flush
            del model
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            print(f"  [STT] Transcription complete. Output: {text.strip()}")
            
        except Exception as e:
            traceback.print_exc()
            text = f"[STT Error: {str(e)}]"
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
                
        self._json_response({"text": text.strip()})

    def _api_system(self):
        """Monitor full system telemetry: CPU, RAM, and NVIDIA GPU (for local SLMs)."""
        import psutil
        sys_data = {
            "cpu_pct": int(psutil.cpu_percent()),
            "ram_pct": int(psutil.virtual_memory().percent),
            "ram_used_gb": round(psutil.virtual_memory().used / (1024**3), 1),
            "ram_total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
            "gpu_available": False
        }
        try:
            out = subprocess.check_output(
                "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits",
                shell=True, timeout=1, text=True
            ).strip().split('\n')[0]
            parts = [p.strip() for p in out.split(",")]
            if len(parts) >= 5:
                vram_pct = int(round((float(parts[1]) / float(parts[2])) * 100)) if float(parts[2]) > 0 else 0
                sys_data.update({
                    "gpu_available": True,
                    "gpu_util_pct": int(parts[0]),
                    "vram_used_mb": int(parts[1]),
                    "vram_total_mb": int(parts[2]),
                    "vram_pct": vram_pct,
                    "gpu_temp_c": int(parts[3]),
                    "gpu_power_w": float(parts[4])
                })
        except Exception:
            pass
        self._json_response(sys_data)

    def _inject_url_context(self, message: str) -> str:
        """Finds URLs in the message, scrapes them, and appends the text as context."""
        import re
        import urllib.request
        from bs4 import BeautifulSoup

        # Match explicit http(s) URLs and standalone domains (e.g., infinitytechstack.uk)
        raw_urls = re.findall(r'(?:https?://[^\s<>"]+|(?:\w+\.)+[a-zA-Z]{2,}(?:/[^\s<>"]*)?)', message)
        
        urls = set()
        for u in raw_urls:
            u = u.rstrip('.,!?;:)')
            # Filter common abbreviations that look like domains
            if u.lower() in ('e.g.', 'i.e.', 'etc.', 'vs.', 'py.', 'js.', 'ts.', 'com.', 'uk.'):
                continue
            urls.add(u)

        if not urls:
            return message
            
        context = ""
        for url in urls:
            target_url = url if url.startswith("http") else f"https://{url}"
            # Use Jina Reader API for flawless Markdown rendering, JS execution, and ad-stripping
            jina_endpoint = f"https://r.jina.ai/{target_url}"
            try:
                req = urllib.request.Request(
                    jina_endpoint, 
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36', 'Accept': 'text/event-stream'}
                )
                with urllib.request.urlopen(req, timeout=15) as response:
                    text = response.read().decode('utf-8')
                    # Keep it bounded (e.g. max 25,000 chars for clean markdown)
                    if len(text) > 25000:
                        text = text[:25000] + "\n... (truncated for context limits)"
                    context += f"\n\n=== Content from {url} ===\n{text}\n"
            except Exception as e:
                context += f"\n\n--- Content from {url} ---\n[Failed to scrape: {str(e)}]\n"
                
        if context:
            return message + "\n\nBACKGROUND INTERNET CONTEXT CAPTURED FROM URLs:\n" + context
        return message

    @staticmethod
    def _estimate_message_tokens(messages: list[dict]) -> int:
        """Fast token estimator aware of code density (symbols inflate tokenizer)."""
        total = 0
        for m in messages:
            text = m.get("content", "")
            # Code-aware heuristic: symbols like {}[]<>;: inflate BPE tokenisation
            symbol_density = sum(1 for c in text if c in '{}[]<>;:()=+*/\\|&!@#$%^~`') / max(len(text), 1)
            chars_per_token = 3 if symbol_density > 0.06 else 4
            total += max(1, len(text) // chars_per_token) + 4  # +4 for role/name overhead
        return total

    @staticmethod
    def _trim_history_to_budget(history: list[dict], max_tokens: int = 12000) -> list[dict]:
        """Sliding-window token guard: drop oldest message pairs until within budget."""
        trimmed = list(history)
        while trimmed:
            est = sum(
                max(1, len(m.get('content', '')) // 4) + 4 for m in trimmed
            )
            if est <= max_tokens:
                break
            # Drop the oldest pair (user + assistant)
            if len(trimmed) >= 2:
                trimmed = trimmed[2:]
            else:
                trimmed = trimmed[1:]
        return trimmed

    def _api_chat(self, body: dict):
        """Chat with a selected model. Maintains session history.
        
        Architecture:
          - URL context is injected EPHEMERALLY into the current API call only.
          - Chat history stores only the raw user message (no scraped HTML).
          - A sliding-window token guard trims oldest messages to stay within budget.
        """
        model = body.get("model", "gemini")
        raw_message = body.get("message", "")
        session = body.get("session_id", "default")

        if not raw_message or not raw_message.strip():
            return self._json_response({"error": "message required"}, 400)

        if len(raw_message) > 50000:
            return self._json_response({"error": "Message too long (max 50,000 chars)"}, 400)

        # Enrich with URL context (transient — NOT stored in history)
        enriched_message = self._inject_url_context(raw_message)

        if not BREAKER.can_proceed():
            return self._json_response({"error": "Circuit breaker open — too many API failures"}, 503)

        # Get or create session history
        if session not in CHAT_HISTORIES:
            CHAT_HISTORIES[session] = []
        history = CHAT_HISTORIES[session]

        # Token-aware sliding window: trim history to stay within context budget
        trimmed_history = self._trim_history_to_budget(history, max_tokens=12000)

        # Build memory context
        memory_ctx = MEMORY.build_context(query=raw_message, session=session, scope="chat")
        sys_prompt = f"{CHAT_SYSTEM}\n\n{memory_ctx}" if memory_ctx else CHAT_SYSTEM

        # Build messages — enriched message is ephemeral, history is lean
        messages = [{"role": "system", "content": sys_prompt}]
        messages.extend(trimmed_history)
        messages.append({"role": "user", "content": enriched_message})

        model_id = MODELS.get(model, model)

        try:
            t0 = time.time()
            content, usage = call_model(
                model_id, messages,
                max_tokens=2048, temperature=0.5,
                provider_name=model, purpose="chat",
            )
            latency = time.time() - t0

            # Store in Working Memory & Episodic (disk)
            MEMORY.remember("user", raw_message, session=session, persist=True)
            MEMORY.remember("assistant", content, session=session, persist=True)

            # Store ONLY the raw message in history (no URL bloat)
            history.append({"role": "user", "content": raw_message})
            history.append({"role": "assistant", "content": content})

            # Record provenance
            PROVENANCE.record("chat_message", session, f"provider:{model}", {
                "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                "cost": usage.get("cost_usd", 0),
                "history_depth": len(trimmed_history),
            })

            BREAKER.record_success()

            self._json_response({
                "response": content,
                "model": model,
                "model_id": model_id,
                "latency_ms": round(latency * 1000, 1),
                "tokens": {
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                },
                "cost_usd": round(usage.get("cost_usd", 0), 6),
                "budget": {
                    "total_spent": round(TRACKER.total_cost, 6),
                    "remaining": round(TRACKER.remaining_budget, 4),
                    "pct_used": round(TRACKER.pct_used * 100, 2),
                },
                "session_messages": len(history),
                "context_window_tokens": self._estimate_message_tokens(messages),
            })

        except Exception as e:
            BREAKER.record_failure()
            raise

    def _api_cowork(self, body: dict):
        """Autonomous Agentic Iteration loop for the Cowork Sandbox."""
        import subprocess
        import re
        import json

        model = body.get("model", "claude")
        raw_message = body.get("message", "")
        session = body.get("session_id", "cowork_session")
        
        if not raw_message or not raw_message.strip():
            return self._json_response({"error": "message required"}, 400)
            
        enriched_message = self._inject_url_context(raw_message)

        if session not in CHAT_HISTORIES:
            CHAT_HISTORIES[session] = []
        history = CHAT_HISTORIES[session]
        trimmed_history = self._trim_history_to_budget(history, max_tokens=15000)

        # Initialize the working session
        messages = [{"role": "system", "content": COWORK_SYSTEM}]
        messages.extend(trimmed_history)
        messages.append({"role": "user", "content": enriched_message})

        model_id = MODELS.get(model, model)
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0

        # Start streaming response
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()

        def stream_chunk(data: dict):
            try:
                self.wfile.write((json.dumps(data) + '\n').encode('utf-8'))
                self.wfile.flush()
            except:
                pass
        
        max_iterations = 5
        final_content = ""
        
        for iteration in range(max_iterations):
            try:
                stream_chunk({"type": "step", "msg": f"Thinking (Iteration {iteration+1}/{max_iterations})..."})
                
                content, usage = call_model(
                    model_id, messages,
                    max_tokens=2048, temperature=0.5,
                    provider_name=model, purpose="cowork"
                )
                
                total_input_tokens += usage.get("input_tokens", 0)
                total_output_tokens += usage.get("output_tokens", 0)
                total_cost += usage.get("cost_usd", 0)
                
                # Check for autonomous execution
                ag_match = re.search(r"<send_antigravity>(.*?)</send_antigravity>", content, re.DOTALL)
                if ag_match:
                    ag_payload = ag_match.group(1).strip()
                    messages.append({"role": "assistant", "content": content})
                    stream_chunk({"type": "step", "msg": "Syncing payload to Antigravity Inbox..."})
                    
                    try:
                        from datetime import datetime
                        from pathlib import Path
                        inbox_dir = Path.home() / ".gemini" / "antigravity" / "brain" / "forge-inbox"
                        inbox_dir.mkdir(parents=True, exist_ok=True)
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        safe_title = f"{ts}_cowork_{model}"
                        fname = f"{safe_title}.json"
                        
                        payload = {
                            "exported_at": datetime.now().isoformat(),
                            "type": "cowork_automation",
                            "model": model,
                            "title": "Direct Message from Forge Agent",
                            "content": {"response": ag_payload}
                        }
                        (inbox_dir / fname).write_text(json.dumps(payload, indent=2), encoding="utf-8")
                        
                        # --- EXPLICIT CLIPBOARD SYNC FOR IDE PASTING ---
                        import subprocess
                        subprocess.run(["powershell", "-command", "Set-Clipboard", "-Value", "$input"], input=ag_payload.encode('utf-8'))
                        
                        # --- AGGRESSIVE NATIVE IDE UX INJECTION ---
                        try:
                            from pywinauto import Desktop
                            from pywinauto.keyboard import send_keys
                            import time
                            
                            for w in Desktop(backend="uia").windows():
                                if "Antigravity" in w.window_text() or "Visual Studio Code" in w.window_text() or "Cursor" in w.window_text():
                                    w.set_focus()
                                    time.sleep(0.3)
                                    send_keys("^v")
                                    time.sleep(0.1)
                                    send_keys("{ENTER}")
                                    break
                        except Exception as e:
                            print(f"[Bridge Warn] GUI Activation failed: {e}")
                        
                        stream_chunk({"type": "ag_redirect", "file": fname})
                        
                        sys_msg = f"OUTPUT FROM ANTIGRAVITY BRIDGE:\nPayload successfully saved to {inbox_dir}\\{fname}. Antigravity can now read this securely. Tell the user you have sent it!"
                        messages.append({"role": "user", "content": sys_msg})
                        continue
                    except Exception as e:
                        messages.append({"role": "user", "content": f"[ERROR: Antigravity Bridge failed: {str(e)}]"})
                        continue

                match = re.search(r"<execute_powershell>(.*?)</execute_powershell>", content, re.DOTALL)
                if match:
                    cmd = match.group(1).strip()
                    # Append AI's intent to context
                    messages.append({"role": "assistant", "content": content})
                    stream_chunk({"type": "execute", "cmd": cmd})
                    
                    try:
                        # Execute natively!
                        result = subprocess.run(
                            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                            capture_output=True, text=True, timeout=20
                        )
                        output_str = result.stdout + result.stderr
                        if not output_str.strip():
                            output_str = "[Command executed successfully, no output returned.]"
                            
                        # Truncate massive outputs
                        if len(output_str) > 5000:
                            output_str = output_str[:5000] + "... [TRUNCATED]"
                            
                        # Feed the stdout immediately back to the LLM
                        sys_msg = f"OUTPUT FROM OS EXECUTION:\n```\n{output_str}\n```\nAnalyze this output and answer the user, or run another command."
                        messages.append({"role": "user", "content": sys_msg})
                        continue
                    except subprocess.TimeoutExpired:
                        stream_chunk({"type": "step", "msg": "Execution timed out."})
                        messages.append({"role": "user", "content": "[ERROR: Command timed out after 20 seconds]"})
                        continue
                    except Exception as e:
                        messages.append({"role": "user", "content": f"[ERROR: Execution failed: {str(e)}]"})
                        continue
                else:
                    # No command requested, LLM provided conversational answer + artifacts
                    final_content = content
                    break
                    
            except Exception as e:
                BREAKER.record_failure()
                stream_chunk({"type": "error", "error": str(e)})
                return
                
        # If loop ran out of gas, just take the last output
        if not final_content:
            final_content = content

        history.append({"role": "user", "content": raw_message})
        history.append({"role": "assistant", "content": final_content})

        stream_chunk({
            "type": "final",
            "response": final_content,
            "model": model,
            "model_id": model_id,
            "tokens": {"input": total_input_tokens, "output": total_output_tokens},
            "cost_usd": round(total_cost, 6)
        })

    def _api_research(self, body: dict):
        """Fan a research query to multiple models."""
        query = body.get("query", "")
        models = body.get("models", ["claude", "gpt", "gemini", "deepseek"])

        if not query:
            return self._json_response({"error": "query required"}, 400)
            
        query = self._inject_url_context(query)

        cost_before = TRACKER.total_cost
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
                results[name] = {
                    "response": content,
                    "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                    "cost_usd": round(usage.get("cost_usd", 0), 6),
                    "latency_ms": round(usage.get("latency_ms", 0), 1),
                }
            except Exception as e:
                results[name] = {"response": f"[ERROR: {e}]", "tokens": 0, "cost_usd": 0}

        # Record provenance
        PROVENANCE.record("research_query", f"research_{int(time.time())}", "arena", {
            "query": query[:100],
            "models": models,
            "total_cost": round(TRACKER.total_cost - cost_before, 6),
        })

        self._json_response({
            "results": results,
            "research_cost": round(TRACKER.total_cost - cost_before, 6),
            "budget": {
                "total_spent": round(TRACKER.total_cost, 6),
                "remaining": round(TRACKER.remaining_budget, 4),
            },
        })

    def _api_consensus(self, body: dict):
        """All models answer + synthesis."""
        query = body.get("query", "")
        models = body.get("models", ["claude", "gpt", "gemini", "deepseek"])

        if not query:
            return self._json_response({"error": "query required"}, 400)

        query = self._inject_url_context(query)

        cost_before = TRACKER.total_cost

        # Phase 1: collect from each model
        individual = {}
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
                individual[name] = content
            except Exception as e:
                individual[name] = f"[ERROR: {e}]"

        # Phase 2: synthesize
        synth_prompt = f"Original question: {query}\n\n"
        for name, answer in individual.items():
            synth_prompt += f"=== {name.upper()} ===\n{answer[:1500]}\n\n"
        synth_prompt += (
            "Synthesize the above into one authoritative answer. "
            "Note agreements and divergences. Be precise."
        )

        synth_model = MODELS.get("gemini", "google/gemini-2.5-pro")
        messages = [
            {"role": "system", "content": "You are a synthesis agent. Merge multiple AI responses into one authoritative answer."},
            {"role": "user", "content": synth_prompt},
        ]
        merged, _ = call_model(
            synth_model, messages,
            max_tokens=2048, temperature=0.3,
            provider_name="gemini", purpose="synthesis",
        )

        PROVENANCE.record("consensus_query", f"consensus_{int(time.time())}", "arena", {
            "query": query[:100],
            "models": models,
        })

        self._json_response({
            "individual": {k: v[:3000] for k, v in individual.items()},
            "consensus": merged,
            "cost_usd": round(TRACKER.total_cost - cost_before, 6),
            "budget": {
                "total_spent": round(TRACKER.total_cost, 6),
                "remaining": round(TRACKER.remaining_budget, 4),
            },
        })

    def _api_compile(self, body: dict):
        """Compile .sl code through Vitalis."""
        source = body.get("source", "")
        if not source:
            return self._json_response({"error": "source required"}, 400)

        # Policy check
        policy = get_policy_engine()
        violations = policy.check_all({"source_code": source})
        if any(v["action"] == "block" for v in violations):
            return self._json_response({
                "success": False,
                "error": f"Policy violation: {violations[0]['description']}",
                "violations": violations,
            })

        result = compile_and_run(source)

        PROVENANCE.record("compilation", f"compile_{int(time.time())}", "compiler", {
            "success": result.success,
            "gate": result.gate_reached.value if result.gate_reached else "unknown",
        })

        self._json_response({
            "success": result.success,
            "output": result.output,
            "gate_reached": result.gate_reached.value if result.gate_reached else "unknown",
            "compile_time_ms": round(result.compile_time_ms, 1),
            "error": result.error,
            "warnings": result.warnings or [],
        })

    def _api_build(self, body: dict):
        """Agent Factory: build any artifact from natural language."""
        description = body.get("description", "")
        if not description:
            return self._json_response({"error": "description required"}, 400)

        artifact_type = body.get("artifact_type", "auto")
        models = body.get("models", ["claude", "gpt", "gemini", "deepseek"])
        extra = body.get("extra_context", "")
        do_score = body.get("score", True)

        description = self._inject_url_context(description)

        try:
            result = factory_build(
                description=description,
                artifact_type=artifact_type,
                models=models,
                extra_context=extra,
                score=do_score,
            )

            leaderboard = []
            for a in sorted(result.artifacts, key=lambda x: x.score_total, reverse=True):
                leaderboard.append({
                    "provider": a.provider_name,
                    "score_total": round(a.score_total, 2),
                    "score_heuristic": round(a.heuristic_total, 1),
                    "score_correctness": a.score_correctness,
                    "score_quality": a.score_quality,
                    "cost": round(a.cost_usd, 4),
                    "tokens": a.tokens_used,
                    "latency_ms": round(a.latency_ms, 0),
                    "lines": len(a.source.split('\n')),
                })

            self._json_response({
                "run_id": result.run_id,
                "artifact_type": result.artifact_type,
                "winner": {
                    "provider": result.winner.provider_name if result.winner else None,
                    "score": round(result.winner.score_total, 2) if result.winner else 0,
                    "code": result.winner.source[:10000] if result.winner else "",
                    "language": result.winner.language if result.winner else "",
                },
                "leaderboard": leaderboard,
                "total_cost": result.total_cost,
                "total_tokens": result.total_tokens,
                "elapsed_s": result.elapsed_s,
                "output_path": str(result.output_path),
                "budget": {
                    "total_spent": round(TRACKER.total_cost, 6),
                    "remaining": round(TRACKER.remaining_budget, 4),
                },
            })

        except Exception as e:
            traceback.print_exc()
            self._json_response({"error": str(e)}, 500)

    def _api_score(self, body: dict):
        """Vitalis Fitness Engine — score any code in any language."""
        code = body.get("code", "")
        language = body.get("language", "auto")

        if not code:
            return self._json_response({"error": "code required"}, 400)

        try:
            report = fitness_score(code, language)
            self._json_response(report.certificate)
        except Exception as e:
            traceback.print_exc()
            self._json_response({"error": str(e)}, 500)

    def _api_orchestrate(self, body: dict):
        """Master Orchestrator: Real-world code generation pipeline.
        
        1. Claude architects the solution (spec, language, structure)
        2. Multiple LLMs compete to implement it
        3. Heuristic scoring picks the winner
        4. Winner saved to disk as a real, runnable file
        """
        task = body.get("task", "")
        if not task:
            return self._json_response({"error": "task required"}, 400)

        trace = get_tracer().start_trace("orchestrate", task)
        get_tracer().add_step(trace.id, "thought", f"Orchestrating multi-model competition. Task: {task}")

        task = self._inject_url_context(task)

        language = body.get("language", "auto")
        worker_models = body.get("models", ["gemini", "claude"])

        cost_before = TRACKER.total_cost

        # ── Step 1: Claude architects the solution ────────────────────
        architect_prompt = (
            "You are a senior software architect. The user will describe what they need built. "
            "You must output ONLY valid JSON (no markdown fences). Design the solution:\n\n"
            "{\n"
            '  "name": "short_snake_case_name",\n'
            '  "title": "Human Readable Title",\n'
            '  "description": "What this code does in 2-3 sentences",\n'
            '  "language": "python|typescript|rust|go|bash|powershell",\n'
            '  "filename": "suggested_filename.ext",\n'
            '  "requirements": ["list", "of", "pip/npm packages needed"],\n'
            '  "architecture": "Brief architecture description",\n'
            '  "key_features": ["feature1", "feature2"],\n'
            '  "implementation_notes": "Any special instructions for the coder"\n'
            "}\n\n"
            f"USER REQUEST: {task}\n\n"
            f"LANGUAGE PREFERENCE: {language} (if 'auto', pick the best language for this task)"
        )

        try:
            arch_content, arch_usage = call_openrouter(
                MODELS["claude"],
                [{"role": "user", "content": architect_prompt}],
                max_tokens=1500,
                provider_name="claude", purpose="orchestrate_plan",
            )
            arch_content = arch_content.replace("```json", "").replace("```", "").strip()
            spec = json.loads(arch_content)

            PROVENANCE.record("orchestrate_plan", spec.get("name", "orch"), "claude", {
                "task": task[:100], "language": spec.get("language", "python"),
            })

        except Exception as e:
            return self._json_response({"error": f"Architect failed: {e}"}, 500)

        # ── Step 2: Competitive code generation ───────────────────────
        lang = spec.get("language", "python")
        filename = spec.get("filename", f"solution.{lang[:2]}")

        code_prompt = (
            f"You are an expert {lang} developer. Write production-quality, "
            f"well-documented, immediately runnable code.\n\n"
            f"TASK: {task}\n\n"
            f"ARCHITECTURE SPEC:\n"
            f"  Title: {spec.get('title', '')}\n"
            f"  Description: {spec.get('description', '')}\n"
            f"  Key Features: {', '.join(spec.get('key_features', []))}\n"
            f"  Requirements: {', '.join(spec.get('requirements', []))}\n"
            f"  Notes: {spec.get('implementation_notes', '')}\n\n"
            f"RULES:\n"
            f"- Write COMPLETE, RUNNABLE code — not pseudo-code or stubs\n"
            f"- Include proper error handling, logging, and docstrings\n"
            f"- Include a main() or entry point so it can actually be executed\n"
            f"- Include usage examples in comments or a help message\n"
            f"- Return ONLY the code, no explanation before or after\n"
        )

        candidates = []
        for model_name in worker_models:
            model_id = MODELS.get(model_name, model_name)
            try:
                t0 = time.time()
                content, usage = call_model(
                    model_id,
                    [{"role": "user", "content": code_prompt}],
                    max_tokens=4096, temperature=0.4,
                    provider_name=model_name, purpose="orchestrate_code",
                )
                latency = time.time() - t0

                # Clean code fences
                code = content.strip()
                if code.startswith("```"):
                    first_nl = code.index("\n")
                    code = code[first_nl+1:]
                if code.endswith("```"):
                    code = code[:-3].rstrip()

                # Heuristic scoring
                lines = len(code.split("\n"))
                has_main = "main(" in code or 'if __name__' in code or "async def main" in code
                has_docstring = '"""' in code or "'''" in code or "///" in code or "/**" in code
                has_error_handling = "try" in code or "except" in code or "catch" in code or "Result<" in code
                has_imports = "import " in code or "from " in code or "require(" in code or "use " in code
                has_comments = code.count("//") + code.count("#") >= 3

                score = 0
                score += min(lines / 10, 10)        # length (up to 10)
                score += 5 if has_main else 0         # has entry point
                score += 3 if has_docstring else 0    # documented
                score += 3 if has_error_handling else 0
                score += 2 if has_imports else 0
                score += 2 if has_comments else 0

                tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                cost = usage.get("cost_usd", 0)

                candidates.append({
                    "provider": model_name,
                    "code": code,
                    "score": round(score, 1),
                    "lines": lines,
                    "tokens": tokens,
                    "cost": round(cost, 6),
                    "latency_ms": round(latency * 1000),
                    "has_main": has_main,
                    "has_docs": has_docstring,
                    "has_error_handling": has_error_handling,
                })
            except Exception as e:
                candidates.append({
                    "provider": model_name, "code": "", "score": 0,
                    "lines": 0, "tokens": 0, "cost": 0, "latency_ms": 0,
                    "error": str(e),
                })

        # Sort by score descending
        candidates.sort(key=lambda c: c["score"], reverse=True)
        winner = candidates[0] if candidates and candidates[0]["code"] else None

        # ── Step 3: Save winner to disk ───────────────────────────────
        output_path = ""
        if winner and winner["code"]:
            out_dir = Path("C:/TheForge/output/orchestrate")
            out_dir.mkdir(parents=True, exist_ok=True)
            safe_name = spec.get("name", "solution").replace(" ", "_")[:40]
            out_file = out_dir / f"{safe_name}_{int(time.time())}.{_ext_for_lang(lang)}"
            out_file.write_text(winner["code"], encoding="utf-8")
            output_path = str(out_file)

            PROVENANCE.record("orchestrate_build", safe_name, f"provider:{winner['provider']}", {
                "language": lang, "lines": winner["lines"], "score": winner["score"],
                "output_path": output_path,
            })
            get_tracer().add_step(trace.id, "observation", f"Winner {winner['provider']} saved to disk.")

        total_cost = round(TRACKER.total_cost - cost_before, 6)
        get_tracer().finish_trace(trace.id, "completed")

        # ── Step 4: Auto-score winner with Vitalis Fitness Engine ─────
        fitness_certificate = None
        if winner and winner["code"]:
            try:
                report = fitness_score(winner["code"], lang)
                fitness_certificate = report.certificate
            except Exception:
                pass

        self._json_response({
            "spec": {
                "name": spec.get("name", ""),
                "title": spec.get("title", ""),
                "description": spec.get("description", ""),
                "language": lang,
                "filename": filename,
                "requirements": spec.get("requirements", []),
                "architecture": spec.get("architecture", ""),
                "key_features": spec.get("key_features", []),
            },
            "candidates": [{
                "provider": c["provider"],
                "score": c["score"],
                "lines": c["lines"],
                "tokens": c["tokens"],
                "cost": c["cost"],
                "latency_ms": c["latency_ms"],
                "has_main": c.get("has_main", False),
                "has_docs": c.get("has_docs", False),
                "has_error_handling": c.get("has_error_handling", False),
                "error": c.get("error"),
            } for c in candidates],
            "winner": {
                "provider": winner["provider"],
                "code": winner["code"],
                "score": winner["score"],
                "lines": winner["lines"],
            } if winner else None,
            "fitness": fitness_certificate,
            "output_path": output_path,
            "total_cost": total_cost,
            "budget": {
                "total_spent": round(TRACKER.total_cost, 6),
                "remaining": round(TRACKER.remaining_budget, 4),
            },
        })

    def _api_deploy(self, body: dict):
        """Deploy a previously-built artifact to its target environment."""
        run_id = body.get("run_id", "")
        if not run_id:
            return self._json_response({"error": "run_id required"}, 400)

        output_base = Path(f"C:/TheForge/output/{run_id}")
        manifest_path = output_base / "manifest.json"
        if not manifest_path.exists():
            return self._json_response({"error": f"Run {run_id} not found at {output_base}"}, 404)
            
        trace = get_tracer().start_trace(f"deploy_{run_id}", f"Deploy Artifact {run_id}")
        get_tracer().add_step(trace.id, "thought", f"Preparing to deploy {run_id}. Checking permissions...")
        
        approved = get_tracer().hitl.request_approval(
            action="Code Deployment (File System)",
            details=f"Agent wants to execute deployment logic for compiled run: {run_id}",
            model="System/Deployer"
        )
        if not approved:
            get_tracer().add_step(trace.id, "observation", "Deployment blocked by operator.")
            get_tracer().finish_trace(trace.id, "failed")
            return self._json_response({"error": "Deployment rejected by Human-in-the-loop Governance"}, 403)
            
        get_tracer().add_step(trace.id, "action", "Permission granted. Executing deploy payload...")

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            winner_data = manifest.get("winner", {})

            from forge.factory import Artifact, FactoryResult
            winner = Artifact(
                artifact_type=manifest.get("artifact_type", "agent"),
                language=winner_data.get("language", "python"),
                description=manifest.get("description", ""),
                provider_name=winner_data.get("provider", "unknown"),
                source=winner_data.get("code", manifest_path.parent.read_text() if False else
                    next((f for ext in [".py",".ts",".go",".rs",".yml"] if (output_base / f"winner_{winner_data.get('provider','')}{ext}").exists()),
                         None) and (list(output_base.glob(f"winner_*.*"))[0].read_text(encoding="utf-8") if list(output_base.glob("winner_*.*")) else "")),
            )
            result = FactoryResult(
                run_id=run_id,
                artifact_type=manifest.get("artifact_type", "agent"),
                winner=winner,
                output_path=output_base,
            )
            deploy_result = factory_deploy(result)

            self._json_response({
                "run_id": run_id,
                "artifact_type": deploy_result.artifact_type,
                "output_path": str(deploy_result.output_path),
                "files_created": deploy_result.files_created,
                "commands": deploy_result.commands,
                "instructions": deploy_result.instructions,
                "mcp_config": deploy_result.mcp_config,
            })
            
            get_tracer().add_step(trace.id, "observation", f"Successfully deployed to {deploy_result.output_path}")
            get_tracer().finish_trace(trace.id, "completed")

        except Exception as e:
            traceback.print_exc()
            self._json_response({"error": str(e)}, 500)

    def _api_hitl_resolve(self, body: dict):
        req_id = body.get("req_id")
        approved = body.get("approved", False)
        if not req_id:
            self._json_response({"error": "Missing req_id"}, 400)
            return
            
        get_tracer().hitl.resolve(req_id, approved)
        self._json_response({"status": "resolved", "req_id": req_id, "approved": approved})

    # ── Helpers ────────────────────────────────────────────────────────────

    def _read_body(self) -> dict:
        """Read and parse JSON request body with validation."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        if length > 10_000_000:  # 10MB hard cap
            raise ValueError("Request body too large (max 10MB)")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Invalid JSON: {e}")

    def _json_response(self, data: dict, status: int = 200):
        """Send a JSON response with security headers."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        """CORS headers — locked to localhost for security."""
        origin = self.headers.get("Origin", "")
        allowed = ("http://localhost", "http://127.0.0.1")
        if any(origin.startswith(a) for a in allowed) or not origin:
            self.send_header("Access-Control-Allow-Origin", origin or "*")
        else:
            self.send_header("Access-Control-Allow-Origin", "http://localhost:8777")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, format, *args):
        """Structured request logging with timestamps."""
        import datetime
        msg = format % args
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        if "/api/" in msg:
            print(f"  [{ts}] [API] {msg}")


# ── Utils ─────────────────────────────────────────────────────────────────────

_START_TIME = time.time()

_LANG_EXTS = {
    "python": "py", "typescript": "ts", "javascript": "js",
    "rust": "rs", "go": "go", "bash": "sh", "powershell": "ps1",
    "ruby": "rb", "java": "java", "csharp": "cs", "cpp": "cpp",
}

def _ext_for_lang(lang: str) -> str:
    return _LANG_EXTS.get(lang.lower(), "py")

def _safe_vitalis_version() -> str:
    try:
        return vitalis_version()
    except Exception:
        return "unavailable"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8777

    vendors = get_vendor_status()
    active_v = [k for k, v in vendors.items() if v["active"]]
    ide_targets = os.environ.get("FORGE_IDE_TARGETS", "antigravity,clipboard")
    print(f"""
==============================================================
  THE FORGE — Multi-Vendor Live API Server                     
  http://localhost:{port}                                       
==============================================================
  Dashboard:  http://localhost:{port}/                           
  API:        http://localhost:{port}/api/health                 
  Models:     {len(MODELS)} registered                                    
  Vendors:    {', '.join(active_v) or 'none configured'}                      
  Budget:     ${TRACKER.budget.max_budget_usd:.2f} max                                      
  Vitalis:    {_safe_vitalis_version():<20s}                          
  IDE Bridge: {ide_targets}                          
  Tracer:     Active (Threading Mode)
==============================================================
    """)

    server = ThreadingHTTPServer(("0.0.0.0", port), ForgeHandler)
    global _SERVER_INSTANCE
    _SERVER_INSTANCE = server
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
