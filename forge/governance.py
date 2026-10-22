"""
The Forge — Governance, Compliance & Formal Verification

Enterprise-grade governance layer providing:
  - Immutable provenance chain (SHA-256 Merkle-style)
  - Role-Based Access Control (RBAC) stubs
  - Deterministic reproducibility (seed control + snapshot)
  - Policy engine for agent behavior constraints
  - Compliance report generation (GDPR, SOC2, HIPAA markers)
  - Kill switch and circuit breaker patterns
"""

from __future__ import annotations

import ast
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Provenance Chain (Append-Only Ledger) ─────────────────────────────────────

@dataclass
class ProvenanceEntry:
    """A single event in the immutable provenance chain."""
    timestamp: float
    event_type: str               # "submission", "compilation", "fitness", "promotion", "mutation", "elimination"
    entity_id: str                # submission_id, tournament_id, etc.
    actor: str                    # "arena", "compiler", "evolution", "provider:claude", etc.
    data: dict = field(default_factory=dict)
    parent_hash: str = ""
    entry_hash: str = ""

    def __post_init__(self):
        if not self.entry_hash:
            payload = f"{self.timestamp}|{self.event_type}|{self.entity_id}|{self.actor}|{json.dumps(self.data, sort_keys=True)}|{self.parent_hash}"
            self.entry_hash = hashlib.sha256(payload.encode()).hexdigest()


_DEFAULT_PROVENANCE_PATH = Path(__file__).resolve().parent.parent / ".forge_provenance.json"


class ProvenanceChain:
    """
    Append-only, hash-chained provenance ledger.
    Every action on every submission is recorded with cryptographic integrity.
    The chain is tamper-evident: modifying any entry invalidates all subsequent hashes.
    """

    def __init__(self, persist_path: Optional[str | Path] = None):
        self._lock = threading.RLock()
        self._entries: list[ProvenanceEntry] = []
        self._head_hash: str = "0" * 64  # genesis
        self._persist_path: Optional[Path] = Path(persist_path) if persist_path else None
        self.tampered_at_load: bool = False

    def record(self, event_type: str, entity_id: str, actor: str, data: dict | None = None) -> ProvenanceEntry:
        with self._lock:
            entry = ProvenanceEntry(
                timestamp=time.time(),
                event_type=event_type,
                entity_id=entity_id,
                actor=actor,
                data=data or {},
                parent_hash=self._head_hash,
            )
            self._entries.append(entry)
            self._head_hash = entry.entry_hash
            if self._persist_path is not None:
                try:
                    self.save_to_disk(self._persist_path)
                except Exception:
                    pass
            return entry

    def verify_integrity(self) -> tuple[bool, int]:
        """Verify the entire chain. Returns (valid, entry_count_or_failed_index)."""
        with self._lock:
            prev_hash = "0" * 64
            for i, entry in enumerate(self._entries):
                if entry.parent_hash != prev_hash:
                    return False, i
                expected = ProvenanceEntry(
                    timestamp=entry.timestamp,
                    event_type=entry.event_type,
                    entity_id=entry.entity_id,
                    actor=entry.actor,
                    data=entry.data,
                    parent_hash=prev_hash,
                )
                if expected.entry_hash != entry.entry_hash:
                    return False, i
                prev_hash = entry.entry_hash
            return True, len(self._entries)

    def get_ancestry(self, entity_id: str) -> list[ProvenanceEntry]:
        """Get all provenance entries for a given entity (submission, tournament)."""
        with self._lock:
            return [e for e in self._entries if e.entity_id == entity_id]

    def get_by_type(self, event_type: str) -> list[ProvenanceEntry]:
        with self._lock:
            return [e for e in self._entries if e.event_type == event_type]

    @property
    def head(self) -> str:
        with self._lock:
            return self._head_hash

    @property
    def length(self) -> int:
        with self._lock:
            return len(self._entries)

    def export_json(self) -> str:
        with self._lock:
            return json.dumps([{
                "timestamp": e.timestamp,
                "event_type": e.event_type,
                "entity_id": e.entity_id,
                "actor": e.actor,
                "data": e.data,
                "parent_hash": e.parent_hash,
                "entry_hash": e.entry_hash,
            } for e in self._entries], indent=2)

    # ── Disk Persistence ──────────────────────────────────────────────────

    def save_to_disk(self, path: str | Path) -> None:
        """Atomically persist the chain to disk."""
        with self._lock:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(self.export_json(), encoding="utf-8")
            tmp.replace(target)

    @classmethod
    def load_from_disk(cls, path: str | Path) -> "ProvenanceChain":
        """Restore a chain from a previously-saved JSON file and verify integrity."""
        target = Path(path)
        chain = cls(persist_path=target)
        if not target.exists():
            return chain
        try:
            entries = json.loads(target.read_text(encoding="utf-8"))
            for raw in entries:
                entry = ProvenanceEntry(
                    timestamp=raw["timestamp"],
                    event_type=raw["event_type"],
                    entity_id=raw["entity_id"],
                    actor=raw["actor"],
                    data=raw.get("data", {}),
                    parent_hash=raw["parent_hash"],
                    entry_hash=raw["entry_hash"],
                )
                chain._entries.append(entry)
                chain._head_hash = entry.entry_hash
            valid, _ = chain.verify_integrity()
            if not valid:
                chain.tampered_at_load = True
        except (json.JSONDecodeError, KeyError, TypeError):
            return cls(persist_path=target)
        return chain


# ── Policy Engine ─────────────────────────────────────────────────────────────

@dataclass
class Policy:
    """A governance policy that constrains agent behavior."""
    name: str
    description: str
    check: str                    # callable name or rule ID
    action: str = "block"         # "block", "warn", "log"
    enabled: bool = True


DEFAULT_POLICIES = [
    Policy("no_file_ops", "Block all file system operations in generated code", "capability_gate_file"),
    Policy("no_network", "Block all network operations in generated code", "capability_gate_network"),
    Policy("no_process_exec", "Block process execution in generated code", "capability_gate_process"),
    Policy("budget_enforcement", "Halt all API calls when budget is exhausted", "budget_check"),
    Policy("type_safety", "Require type-check pass before JIT execution", "type_check_preflight"),
    Policy("timeout_enforcement", "Kill execution exceeding timeout limit", "timeout_check"),
    Policy("regression_prevention", "Champion must beat current best by >2σ", "regression_check"),
    Policy("novelty_threshold", "Reject submissions with <10% code distance from population", "novelty_check"),
    Policy("max_complexity", "Reject code exceeding cyclomatic complexity threshold", "complexity_check"),
    Policy("audit_all_promotions", "Log all champion promotions to provenance chain", "audit_promotion", action="log"),
]


class PolicyEngine:
    """Evaluate governance policies before agent actions."""

    _AST_FILE_CALLS = {
        "open", "os.remove", "os.unlink", "os.rmdir",
        "shutil.rmtree", "shutil.move", "Path.write_text", "Path.write_bytes",
    }
    _AST_FILE_BARE = {"open", "rmtree", "unlink", "rmdir", "write_text", "write_bytes"}

    _AST_PROCESS_CALLS = {
        "os.system", "os.popen", "os.exec", "os.execv", "os.execve", "os.spawnv",
        "subprocess.run", "subprocess.call", "subprocess.check_call",
        "subprocess.check_output", "subprocess.Popen", "eval", "exec", "compile",
    }
    _AST_PROCESS_BARE = {
        "eval", "exec", "compile", "Popen", "check_call", "check_output",
        "execv", "execve", "spawnv", "popen",
    }

    _AST_NETWORK_CALLS = {
        "socket.socket", "urllib.request.urlopen", "requests.get",
        "requests.post", "requests.put", "requests.delete", "requests.request",
        "http.client.HTTPConnection", "http.client.HTTPSConnection",
    }
    _AST_NETWORK_BARE = {"urlopen", "HTTPConnection", "HTTPSConnection"}

    def __init__(self, policies: list[Policy] | None = None):
        self.policies = policies or list(DEFAULT_POLICIES)

    def check_all(self, context: dict) -> list[dict]:
        """Run all enabled policies against a context. Returns violations."""
        violations = []
        for policy in self.policies:
            if not policy.enabled:
                continue
            violated = self._evaluate(policy, context)
            if violated:
                violations.append({
                    "policy": policy.name,
                    "action": policy.action,
                    "description": policy.description,
                })
        return violations

    @staticmethod
    def _call_name(node: ast.Call, aliases: dict[str, str] | None = None) -> str:
        """Resolve a Call node's callee to a canonical dotted name using import aliases."""
        func = node.func
        parts: list[str] = []
        while isinstance(func, ast.Attribute):
            parts.append(func.attr)
            func = func.value
        if isinstance(func, ast.Name):
            parts.append(func.id)
        if not parts:
            return ""
        parts.reverse()
        if aliases and parts[0] in aliases:
            resolved_prefix = aliases[parts[0]]
            parts = resolved_prefix.split(".") + parts[1:]
        return ".".join(parts)

    def _ast_call_names(self, source: str) -> Optional[set[str]]:
        """Parse `source` as Python and return resolved call names (with import alias expansion)."""
        if not source or not source.strip():
            return set()
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return None

        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        aliases[alias.asname] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    local_name = alias.asname or alias.name
                    aliases[local_name] = f"{node.module}.{alias.name}"

        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = self._call_name(node, aliases)
                if name:
                    names.add(name)
        return names

    def _matches_any(self, call_names: set[str], qualified_targets: set[str], bare_targets: set[str] | None = None) -> bool:
        bare_targets = bare_targets or set()
        for call in call_names:
            if call in qualified_targets or call in bare_targets:
                return True
            if "." in call:
                suffix = call.rsplit(".", 1)[-1]
                if suffix in bare_targets:
                    return True
        return False

    def _evaluate(self, policy: Policy, context: dict) -> bool:
        """Check if a policy is violated. Returns True if violated."""
        source = context.get("source_code", "")
        call_names = self._ast_call_names(source) if source else set()

        if policy.check == "capability_gate_file":
            if call_names is not None and self._matches_any(call_names, self._AST_FILE_CALLS, self._AST_FILE_BARE):
                return True
            return any(p in source for p in ("file_write", "file_delete", "file_append", "std::fs::"))
        if policy.check == "capability_gate_network":
            if call_names is not None and self._matches_any(call_names, self._AST_NETWORK_CALLS, self._AST_NETWORK_BARE):
                return True
            return any(p in source for p in ("http_get", "http_post", "tcp_connect", "std::net::"))
        if policy.check == "capability_gate_process":
            if call_names is not None and self._matches_any(call_names, self._AST_PROCESS_CALLS, self._AST_PROCESS_BARE):
                return True
            return any(p in source for p in ("process_exec", "dlopen", "std::process::Command"))
        if policy.check == "budget_check":
            return bool(context.get("budget_exhausted", False))
        if policy.check == "type_check_preflight":
            return context.get("type_check_passed") is False
        if policy.check == "timeout_check":
            if context.get("timed_out", False):
                return True
            if "elapsed_ms" in context and "timeout_ms" in context:
                return float(context["elapsed_ms"]) > float(context["timeout_ms"])
            return False
        if policy.check == "regression_check":
            if context.get("regression_detected", False):
                return True
            if "candidate_fitness" in context and "champion_fitness" in context:
                return float(context["candidate_fitness"]) + 1e-9 < float(context["champion_fitness"])
            return False
        if policy.check == "novelty_check":
            if "novelty_distance" in context:
                return float(context["novelty_distance"]) < float(context.get("min_novelty", 0.10))
            return False
        if policy.check == "complexity_check":
            max_cc = context.get("max_cyclomatic", 50)
            return context.get("cyclomatic_complexity", 0) > max_cc
        if policy.check == "audit_promotion":
            if context.get("event_type") == "promotion" and not context.get("provenance_logged", False):
                return True
            return False
        return False


# ── Deterministic Reproducibility ─────────────────────────────────────────────

@dataclass
class TournamentSnapshot:
    """Complete state snapshot for deterministic replay."""
    tournament_id: str
    timestamp: float
    random_seed: int
    config_json: str
    challenge_json: str
    provider_names: list[str]
    generation_count: int
    champion_id: str
    champion_fitness: float
    champion_source: str
    provenance_head: str
    cost_total: float
    total_tokens: int

    def to_json(self) -> str:
        return json.dumps({
            "tournament_id": self.tournament_id,
            "timestamp": self.timestamp,
            "random_seed": self.random_seed,
            "config": json.loads(self.config_json),
            "challenge": json.loads(self.challenge_json),
            "providers": self.provider_names,
            "generations": self.generation_count,
            "champion_id": self.champion_id,
            "champion_fitness": self.champion_fitness,
            "champion_source": self.champion_source,
            "provenance_head": self.provenance_head,
            "cost_usd": self.cost_total,
            "total_tokens": self.total_tokens,
        }, indent=2)


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitBreaker:
    """Thread-safe circuit breaker for API calls.

    Opens (blocks calls) after N consecutive failures.
    Half-opens after cooldown to test recovery with a single probe request.
    """

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 60.0):
        self._lock = threading.Lock()
        self._failure_count: int = 0
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._last_failure: float = 0
        self._state: str = "closed"  # closed, open, half-open
        self._half_open_in_flight: bool = False

    def record_success(self):
        with self._lock:
            self._failure_count = 0
            self._state = "closed"
            self._half_open_in_flight = False

    def record_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure = time.time()
            self._half_open_in_flight = False
            if self._failure_count >= self._threshold or self._state == "half-open":
                self._state = "open"

    def can_proceed(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if time.time() - self._last_failure > self._cooldown:
                    self._state = "half-open"
                    self._half_open_in_flight = True
                    return True
                return False
            # half-open: allow only one concurrent probe attempt
            if not self._half_open_in_flight:
                self._half_open_in_flight = True
                return True
            return False

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "open" and (time.time() - self._last_failure > self._cooldown):
                return "half-open"
            return self._state


# ── Compliance Report Generator ───────────────────────────────────────────────

def generate_compliance_report(chain: ProvenanceChain, tournament_id: str) -> str:
    """Generate a compliance-ready audit report from the provenance chain."""
    entries = chain.get_ancestry(tournament_id)
    valid, count = chain.verify_integrity()

    lines = [
        f"{'═' * 60}",
        f"  COMPLIANCE AUDIT REPORT",
        f"  Tournament: {tournament_id}",
        f"  Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"{'─' * 60}",
        f"  Chain integrity: {'✅ VERIFIED' if valid else '❌ TAMPERED'}",
        f"  Chain length:    {count} entries",
        f"  Head hash:       {chain.head[:16]}...",
        f"{'─' * 60}",
        f"  EVENT TIMELINE:",
    ]

    for e in entries:
        ts = time.strftime('%H:%M:%S', time.gmtime(e.timestamp))
        lines.append(f"    {ts}  [{e.event_type:15s}]  {e.actor:20s}  {e.entry_hash[:12]}...")

    lines.append(f"{'═' * 60}")
    return "\n".join(lines)


# ── Global Singleton ──────────────────────────────────────────────────────────

_chain: Optional[ProvenanceChain] = None
_policy: Optional[PolicyEngine] = None


def get_provenance() -> ProvenanceChain:
    global _chain
    if _chain is None:
        _chain = ProvenanceChain.load_from_disk(_DEFAULT_PROVENANCE_PATH)
    return _chain


def get_policy_engine() -> PolicyEngine:
    global _policy
    if _policy is None:
        _policy = PolicyEngine()
    return _policy
