"""
The Forge — Skill DNA Engine

A Skill DNA is a compiled, benchmarked, cryptographically-certified Vitalis .sl
function that represents a reusable, verifiable capability.

Unlike SKILL.md (plain text), a Skill DNA:
  - Has been compiled through Vitalis JIT (7 gates)
  - Has a measured p95 latency and fitness score
  - Has a SHA-256 hash of source + fitness + signature
  - Has a provenance chain entry (tamper-evident)
  - Was evolved across multiple LLMs (not hand-written)
  - Can be directly exposed as an MCP tool

This is the trust anchor for the Forge Nexus Protocol.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ── Skill DNA ─────────────────────────────────────────────────────────────────

@dataclass
class SkillDNA:
    """
    A compiler-certified, evolution-selected, benchmarked Vitalis skill.
    
    The dna_hash is a SHA-256 of (source_code + signature + fitness_score),
    making it tamper-evident: any change to the source invalidates the hash.
    """
    # Identity
    skill_id: str = ""
    name: str = ""
    description: str = ""
    category: str = "general"           # "sorting", "math", "search", "string", etc.
    tags: list[str] = field(default_factory=list)

    # Code
    source_code: str = ""
    signature: str = ""                 # e.g. "fn sort(arr: [i64]) -> [i64]"
    language: str = "vitalis"

    # Forge Certification
    fitness_score: float = 0.0          # 0-100, from arena evolution
    correctness: float = 0.0            # 0.0-1.0
    performance_p95_us: float = 0.0     # microseconds, p95 latency
    performance_median_us: float = 0.0
    benchmark_runs: int = 0
    generation: int = 0                 # which generation this was champion
    champion_provider: str = ""         # e.g. "claude×gpt (gen42)"
    models_competed: list[str] = field(default_factory=list)

    # Cryptographic Provenance
    dna_hash: str = ""
    provenance_head: str = ""
    certified_at: float = 0.0

    # MCP Integration
    mcp_tool_name: str = ""             # e.g. "forge_sort_integers"
    mcp_description: str = ""
    mcp_input_schema: dict = field(default_factory=dict)
    mcp_output_schema: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.skill_id:
            self.skill_id = f"skill_{uuid.uuid4().hex[:8]}"
        if not self.certified_at:
            self.certified_at = time.time()
        if not self.dna_hash and self.source_code:
            self.dna_hash = self._compute_hash()
        if not self.mcp_tool_name and self.name:
            self.mcp_tool_name = "forge_" + self.name.lower().replace(" ", "_").replace("-", "_")

    def _compute_hash(self) -> str:
        payload = f"{self.source_code}|{self.signature}|{self.fitness_score:.4f}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def verify_integrity(self) -> bool:
        """Verify the DNA hash matches the source code."""
        return self.dna_hash == self._compute_hash()

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "SkillDNA":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_json(cls, s: str) -> "SkillDNA":
        return cls.from_dict(json.loads(s))

    def summary(self) -> str:
        verified = "✅" if self.verify_integrity() else "❌"
        return (
            f"  {verified} [{self.skill_id}] {self.name}\n"
            f"     Fitness: {self.fitness_score:.1f} | Correctness: {self.correctness:.2f} "
            f"| p95: {self.performance_p95_us:.1f}µs\n"
            f"     Provider: {self.champion_provider} | Gen: {self.generation}\n"
            f"     Hash: {self.dna_hash[:16]}... | MCP: {self.mcp_tool_name}"
        )


# ── Skill Factory ─────────────────────────────────────────────────────────────

def certify_from_compilation(
    name: str,
    description: str,
    source_code: str,
    signature: str,
    category: str = "general",
    fitness_score: float = 0.0,
    correctness: float = 1.0,
    performance_p95_us: float = 0.0,
    performance_median_us: float = 0.0,
    benchmark_runs: int = 0,
    generation: int = 0,
    champion_provider: str = "manual",
    models_competed: list[str] | None = None,
    provenance_head: str = "",
    tags: list[str] | None = None,
) -> SkillDNA:
    """
    Certify a piece of compiled, validated .sl code as a Skill DNA.
    Called after a successful arena run or manual compilation.
    """
    slug = name.lower().replace(" ", "_").replace("-", "_")
    mcp_name = f"forge_{slug}"

    # Build MCP schemas from the signature
    input_schema, output_schema = _schemas_from_signature(signature)

    return SkillDNA(
        skill_id=f"{slug}_{uuid.uuid4().hex[:6]}",
        name=name,
        description=description,
        category=category,
        tags=tags or [],
        source_code=source_code,
        signature=signature,
        fitness_score=fitness_score,
        correctness=correctness,
        performance_p95_us=performance_p95_us,
        performance_median_us=performance_median_us,
        benchmark_runs=benchmark_runs,
        generation=generation,
        champion_provider=champion_provider,
        models_competed=models_competed or [],
        provenance_head=provenance_head,
        certified_at=time.time(),
        mcp_tool_name=mcp_name,
        mcp_description=f"[Forge Certified ✅ fitness={fitness_score:.1f}] {description}",
        mcp_input_schema=input_schema,
        mcp_output_schema=output_schema,
    )


def _schemas_from_signature(sig: str) -> tuple[dict, dict]:
    """
    Parse a Vitalis function signature into JSON schemas for MCP.
    e.g. "fn sort(arr: [i64]) -> [i64]" → input: {arr: array}, output: {result: array}
    """
    # Simple heuristic — sufficient for current Vitalis type system
    type_map = {
        "i64": {"type": "integer"},
        "f64": {"type": "number"},
        "bool": {"type": "boolean"},
        "str": {"type": "string"},
        "[i64]": {"type": "array", "items": {"type": "integer"}},
        "[f64]": {"type": "array", "items": {"type": "number"}},
    }

    props = {}
    required = []

    try:
        params_part = sig[sig.index("(") + 1 : sig.index(")")]
        if params_part.strip():
            for param in params_part.split(","):
                param = param.strip()
                if ":" in param:
                    pname, ptype = [x.strip() for x in param.split(":", 1)]
                    props[pname] = type_map.get(ptype, {"type": "string", "description": ptype})
                    required.append(pname)

        ret_type = "i64"
        if "->" in sig:
            ret_type = sig.split("->")[1].strip().rstrip("{").strip()
    except Exception:
        ret_type = "i64"

    input_schema = {
        "type": "object",
        "properties": props,
        "required": required,
    }
    output_schema = {
        "type": "object",
        "properties": {
            "result": type_map.get(ret_type, {"type": "string", "description": ret_type}),
            "output_raw": {"type": "string"},
            "compile_time_ms": {"type": "number"},
            "gate_reached": {"type": "string"},
        },
    }
    return input_schema, output_schema


# ── Global Registry (in-memory) ───────────────────────────────────────────────

_registry: dict[str, SkillDNA] = {}

def register(skill: SkillDNA) -> None:
    _registry[skill.skill_id] = skill

def get(skill_id: str) -> Optional[SkillDNA]:
    return _registry.get(skill_id)

def all_skills() -> list[SkillDNA]:
    return list(_registry.values())

def search(query: str) -> list[SkillDNA]:
    q = query.lower()
    return [s for s in _registry.values()
            if q in s.name.lower() or q in s.description.lower()
            or any(q in t for t in s.tags) or q in s.category.lower()]
