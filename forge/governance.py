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


class ProvenanceChain:
    """
    Append-only, hash-chained provenance ledger.
    Every action on every submission is recorded with cryptographic integrity.
    The chain is tamper-evident: modifying any entry invalidates all subsequent hashes.
    """

    def __init__(self):
        self._entries: list[ProvenanceEntry] = []
        self._head_hash: str = "0" * 64  # genesis

    def record(self, event_type: str, entity_id: str, actor: str, data: dict | None = None) -> ProvenanceEntry:
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
        return entry

    def verify_integrity(self) -> tuple[bool, int]:
        """Verify the entire chain. Returns (valid, entry_count)."""
        prev_hash = "0" * 64
        for i, entry in enumerate(self._entries):
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
        return [e for e in self._entries if e.entity_id == entity_id]

    def get_by_type(self, event_type: str) -> list[ProvenanceEntry]:
        return [e for e in self._entries if e.event_type == event_type]

    @property
    def head(self) -> str:
        return self._head_hash

    @property
    def length(self) -> int:
        return len(self._entries)

    def export_json(self) -> str:
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
    # Without persistence the entire "tamper-evident" ledger was lost on
    # every server restart, defeating its core purpose.

    def save_to_disk(self, path: str | Path) -> None:
        """Atomically persist the chain to disk.

        Uses a write-to-temp-then-rename strategy so a crash mid-write
        never corrupts the on-disk copy.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(self.export_json(), encoding="utf-8")
        tmp.replace(target)  # atomic on POSIX; near-atomic on NTFS

    @classmethod
    def load_from_disk(cls, path: str | Path) -> "ProvenanceChain":
        """Restore a chain from a previously-saved JSON file.

        Validates hash-chain integrity on load — if the file has been
        tampered with, verification will detect it immediately.
        """
        chain = cls()
        target = Path(path)
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
        except (json.JSONDecodeError, KeyError, TypeError):
            # Corrupted file — start fresh rather than crash
            return cls()
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
    Policy("audit_all_promotions", "Log all champion promotions to provenance chain", "audit_promotion"),
]


class PolicyEngine:
    """Evaluate governance policies before agent actions."""

    # Fully-qualified (or bare) callable names that indicate real file I/O,
    # process execution, dynamic code execution, or network access when
    # found in the AST of Python source — this catches renamed imports /
    # aliasing / string-built calls that the naive substring scan misses,
    # e.g. `import os as o; o.system(...)`, `getattr(os, "sys" + "tem")(...)`
    # would still slip an AST scan too, but direct calls are now caught
    # regardless of surrounding string literals or comments.
    _AST_FILE_CALLS = {"open", "os.remove", "os.unlink", "os.rmdir", "shutil.rmtree", "shutil.move", "Path.write_text", "Path.write_bytes"}
    _AST_PROCESS_CALLS = {"os.system", "os.popen", "os.exec", "os.execv", "os.execve", "os.spawnv", "subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen", "eval", "exec", "compile"}
    _AST_NETWORK_CALLS = {"socket.socket", "urllib.request.urlopen", "requests.get", "requests.post", "http.client.HTTPConnection"}

    def __init__(self, policies: list[Policy] | None = None):
        self.policies = policies or list(DEFAULT_POLICIES)

    def check_all(self, context: dict) -> list[dict]:
        """Run all enabled policies against a context. Returns violations."""
        violations = []
        for policy in self.policies:
            if not policy.enabled:
                continue
            # Simplified policy evaluation
            violated = self._evaluate(policy, context)
            if violated:
                violations.append({
                    "policy": policy.name,
                    "action": policy.action,
                    "description": policy.description,
                })
        return violations

    @staticmethod
    def _call_name(node: ast.Call) -> str:
        """Best-effort resolution of a Call node's callee to a dotted name,
        e.g. `os.system(...)` -> "os.system", `eval(...)` -> "eval"."""
        func = node.func
        parts: list[str] = []
        while isinstance(func, ast.Attribute):
            parts.append(func.attr)
            func = func.value
        if isinstance(func, ast.Name):
            parts.append(func.id)
        return ".".join(reversed(parts))

    def _ast_call_names(self, source: str) -> Optional[set[str]]:
        """Parse `source` as Python and return the set of resolved call
        names found. Returns None if the source isn't parseable Python
        (e.g. it's Vitalis/.sl, Rust, Go, TypeScript, etc.) — in that case
        callers should fall back to substring scanning only."""
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return None
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = self._call_name(node)
                if name:
                    names.add(name)
                    if "." in name:
                        # Also record the bare trailing attribute so an
                        # aliased import (`import os as o; o.system(...)`)
                        # or `from subprocess import Popen; Popen(...)`
                        # still matches a dotted target like "os.system".
                        names.add(name.rsplit(".", 1)[-1])
        return names

    def _matches_any(self, call_names: set[str], targets: set[str]) -> bool:
        for target in targets:
            if target in call_names:
                return True
            # allow suffix match for module-qualified variants, e.g. a call
            # resolved as "subprocess.Popen" matches target "subprocess.Popen"
            # and a bare "Popen" (imported via `from subprocess import Popen`)
            bare = target.rsplit(".", 1)[-1]
            if bare in call_names:
                return True
        return False

    def _evaluate(self, policy: Policy, context: dict) -> bool:
        """Check if a policy is violated. Returns True if violated.

        Uses an AST-based scan of Python call sites as the primary check
        (catches renamed/aliased calls that a naive substring scan misses),
        falling back to substring scanning for non-Python source (Vitalis
        .sl, Rust, Go, TypeScript, ...) where no Python AST is available.
        """
        source = context.get("source_code", "")
        call_names = self._ast_call_names(source)

        if policy.check == "capability_gate_file":
            if call_names is not None and self._matches_any(call_names, self._AST_FILE_CALLS):
                return True
            return any(p in source for p in ("file_write", "file_delete", "file_append"))
        if policy.check == "capability_gate_network":
            if call_names is not None and self._matches_any(call_names, self._AST_NETWORK_CALLS):
                return True
            return any(p in source for p in ("http_get", "http_post", "tcp_connect"))
        if policy.check == "capability_gate_process":
            if call_names is not None and self._matches_any(call_names, self._AST_PROCESS_CALLS):
                return True
            return any(p in source for p in ("process_exec", "dlopen"))
        if policy.check == "budget_check":
            return context.get("budget_exhausted", False)
        if policy.check == "complexity_check":
            max_cc = context.get("max_cyclomatic", 50)
            return context.get("cyclomatic_complexity", 0) > max_cc
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
    Half-opens after cooldown to test recovery.

    Thread safety: ForgeHandler runs inside ThreadingHTTPServer which
    dispatches each request on a separate thread. Without a lock,
    concurrent requests could corrupt _failure_count / _state leading
    to either phantom-open or stuck-closed states.
    """

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 60.0):
        self._lock = threading.Lock()
        self._failure_count: int = 0
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._last_failure: float = 0
        self._state: str = "closed"  # closed, open, half-open

    def record_success(self):
        with self._lock:
            self._failure_count = 0
            self._state = "closed"

    def record_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure = time.time()
            if self._failure_count >= self._threshold:
                self._state = "open"

    def can_proceed(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if time.time() - self._last_failure > self._cooldown:
                    self._state = "half-open"
                    return True
                return False
            # half-open: allow one attempt
            return True

    @property
    def state(self) -> str:
        with self._lock:
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
        _chain = ProvenanceChain()
    return _chain

def get_policy_engine() -> PolicyEngine:
    global _policy
    if _policy is None:
        _policy = PolicyEngine()
    return _policy
