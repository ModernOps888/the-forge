"""
Forge Memory & Skills Engine — powered by Vitalis v60 hotpaths
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Three-tier memory architecture:
  Tier 1 WORKING    — in-RAM per session, cosine dedup via Vitalis
  Tier 2 EPISODIC   — JSON on disk, EMA importance decay
  Tier 3 SEMANTIC   — skills + instructions injected into system prompts

Vitalis hotpaths used:
  hotpath_cosine_similarity  → semantic deduplication & retrieval ranking
  hotpath_ema_update         → memory importance decay
  hotpath_boltzmann_select   → skill selection pressure
  hotpath_adaptive_fitness   → skill fitness scoring
  hotpath_mean               → stats aggregation
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# ── Vitalis integration ───────────────────────────────────────────────────────
try:
    from .vitalis_ffi import (
        hotpath_cosine_similarity,
        hotpath_ema_update,
        hotpath_boltzmann_select,
        hotpath_adaptive_fitness,
        hotpath_mean,
    )
    _VITALIS = True
except Exception:
    _VITALIS = False
    def hotpath_cosine_similarity(a, b):
        dot = sum(x*y for x,y in zip(a,b))
        na = math.sqrt(sum(x*x for x in a)) or 1e-9
        nb = math.sqrt(sum(y*y for y in b)) or 1e-9
        return dot/(na*nb)
    def hotpath_ema_update(old, new, alpha=0.3): return alpha*new+(1-alpha)*old
    def hotpath_boltzmann_select(f, temperature=1.0):
        mx = max(f) if f else 0
        exps=[math.exp((x - mx)/(temperature+1e-9)) for x in f]; s=sum(exps) or 1
        return [e/s for e in exps]
    def hotpath_adaptive_fitness(speed,correctness,complexity,security,generation=0):
        return correctness*0.4+speed*0.3+security*0.2+(1-complexity)*0.1
    def hotpath_mean(v): return sum(v)/len(v) if v else 0.0

# ── Paths ─────────────────────────────────────────────────────────────────────
MEMORY_DIR   = Path(__file__).parent.parent / "memory"
MEMORY_DIR.mkdir(exist_ok=True)
EPISODIC_PATH = MEMORY_DIR / "episodic.json"
SKILLS_PATH   = MEMORY_DIR / "skills.json"
INSTRUCT_PATH = MEMORY_DIR / "instructions.json"

# ── Embedding (bag-of-words → fixed-dim, no API needed) ──────────────────────
_DIM = 64
_STOP = {"a","an","the","is","it","to","and","or","in","of","for","with",
          "this","that","be","was","are","i","you","we","do","does","can",
          "will","have","has","at","by","from","not","on","but","if","as",
          "what","how","my","your","its","s","t","just","so","me","about"}

def _embed(text: str) -> list[float]:
    text = text.lower()
    tokens = [t for t in re.findall(r"[a-z0-9_]+", text) if len(t)>2 and t not in _STOP]
    vec = [0.0]*_DIM
    for token in tokens:
        idx = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4],"little") % _DIM
        vec[idx] += 1.0
    mag = math.sqrt(sum(v*v for v in vec)) or 1.0
    return [v/mag for v in vec]

# ── Data models ───────────────────────────────────────────────────────────────
@dataclass
class MemoryEntry:
    id: str
    role: str
    content: str
    session: str
    timestamp: float
    importance: float
    access_count: int = 0
    embedding: list[float] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    def __post_init__(self):
        if not self.embedding:
            self.embedding = _embed(self.content)

@dataclass
class Skill:
    name: str
    description: str
    trigger_keywords: list[str]
    system_prompt_injection: str
    use_count: int = 0
    fitness: float = 0.5
    enabled: bool = True
    def matches(self, query: str) -> float:
        qv = _embed(query)
        sv = _embed(self.description + " " + " ".join(self.trigger_keywords))
        return max(0.0, hotpath_cosine_similarity(qv, sv))

@dataclass
class Instruction:
    id: str
    content: str
    scope: str      # global | chat | research | build | orchestrate
    priority: int = 5
    enabled: bool = True

# ── Engine ────────────────────────────────────────────────────────────────────
class MemoryEngine:
    DEDUP_SIM   = 0.88
    MAX_WORKING = 30
    MAX_EPISODIC= 500
    DECAY_ALPHA = 0.05
    TOP_K       = 6
    MAX_CONTEXT = 2000

    def __init__(self):
        self._working: dict[str, list[MemoryEntry]] = {}
        self._episodic: list[MemoryEntry] = []
        self._skills: list[Skill] = []
        self._instructions: list[Instruction] = []
        self._load_all()
        tag = "[OK] Vitalis hotpaths" if _VITALIS else "[WARN] Python fallbacks"
        print(f"  [Memory] {tag} | episodic={len(self._episodic)} skills={len(self._skills)}")

    # ── Core API ──────────────────────────────────────────────────────────────
    def remember(self, role: str, content: str, session: str = "default",
                 tags: list[str] | None = None, persist: bool = False):
        if not content or len(content.strip()) < 8:
            return
        entry = MemoryEntry(
            id=hashlib.sha256(f"{session}{role}{content}{time.time()}".encode()).hexdigest()[:16],
            role=role, content=content, session=session,
            timestamp=time.time(), importance=1.0, tags=tags or [],
        )
        working = self._working.setdefault(session, [])
        if self._is_dup(entry, working):
            return
        working.append(entry)
        if len(working) > self.MAX_WORKING:
            working.sort(key=lambda e: e.importance)
            evicted = working.pop(0)
            self._episodic.append(evicted)
        if persist:
            self._episodic.append(entry)
            self._save_episodic()

    def recall(self, query: str, session: str = "default", k: int | None = None) -> list[MemoryEntry]:
        k = k or self.TOP_K
        qv = _embed(query)
        candidates: list[tuple[float, MemoryEntry]] = []
        for e in self._working.get(session, []):
            if e.embedding:
                s = hotpath_cosine_similarity(qv, e.embedding) * e.importance
                candidates.append((s, e))
        for e in self._episodic[-200:]:
            if e.embedding:
                s = hotpath_cosine_similarity(qv, e.embedding) * e.importance * 0.8
                candidates.append((s, e))
        candidates.sort(key=lambda x: x[0], reverse=True)
        top = [e for _, e in candidates[:k]]
        for e in top:
            e.access_count += 1
            e.importance = hotpath_ema_update(e.importance, 1.0, alpha=0.2)
        return top

    def decay(self):
        for entries in self._working.values():
            for e in entries:
                if e.access_count == 0:
                    e.importance = hotpath_ema_update(e.importance, 0.0, alpha=self.DECAY_ALPHA)
        for e in self._episodic:
            if e.access_count == 0:
                e.importance = hotpath_ema_update(e.importance, 0.0, alpha=self.DECAY_ALPHA*0.5)

    def get_skills(self, query: str, top_k: int = 3) -> list[Skill]:
        enabled = [s for s in self._skills if s.enabled]
        if not enabled: return []
        scores = [s.matches(query) for s in enabled]
        fitnesses = [sc * sk.fitness for sc, sk in zip(scores, enabled)]
        if not any(f > 0.05 for f in fitnesses): return []
        probs = hotpath_boltzmann_select(fitnesses, temperature=0.4)
        indexed = sorted(enumerate(probs), key=lambda x: x[1], reverse=True)
        selected = [enabled[i] for i, _ in indexed[:top_k] if probs[i] > 0.05]
        for sk in selected:
            sk.use_count += 1
        return selected

    def get_instructions(self, scope: str = "global") -> list[Instruction]:
        return sorted(
            [i for i in self._instructions if i.enabled and i.scope in (scope, "global")],
            key=lambda i: i.priority, reverse=True,
        )

    def build_context(self, query: str = "", session: str = "default", scope: str = "chat") -> str:
        parts = []
        instrs = self.get_instructions(scope)
        if instrs:
            parts.append("### Standing Instructions")
            parts += [f"- {i.content}" for i in instrs[:5]]
        if query:
            skills = self.get_skills(query)
            if skills:
                parts.append("\n### Active Skills")
                parts += [f"- **{s.name}**: {s.system_prompt_injection}" for s in skills]
            mems = self.recall(query, session=session)
            if mems:
                parts.append("\n### Relevant Memory")
                for m in mems:
                    age = _age(time.time() - m.timestamp)
                    icon = "👤" if m.role == "user" else "🔥"
                    parts.append(f"- [{age}] {icon} {m.content[:180]}")
        if not parts: return ""
        block = "## Forge Memory\n" + "\n".join(parts)
        return block[:self.MAX_CONTEXT] + ("\n[truncated]" if len(block) > self.MAX_CONTEXT else "")

    def add_skill(self, name: str, description: str, keywords: list[str], injection: str) -> Skill:
        sk = Skill(name=name, description=description, trigger_keywords=keywords, system_prompt_injection=injection)
        self._skills.append(sk)
        self._save_skills()
        return sk

    def add_instruction(self, content: str, scope: str = "global", priority: int = 5) -> Instruction:
        instr = Instruction(
            id=hashlib.sha256(f"{content}{time.time()}".encode()).hexdigest()[:12],
            content=content, scope=scope, priority=priority,
        )
        self._instructions.append(instr)
        self._save_instructions()
        return instr

    def remove_instruction(self, iid: str) -> bool:
        before = len(self._instructions)
        self._instructions = [i for i in self._instructions if i.id != iid]
        if len(self._instructions) < before:
            self._save_instructions()
            return True
        return False

    def update_skill_fitness(self, name: str, success: bool, latency_ms: float = 0.0):
        for sk in self._skills:
            if sk.name == name:
                speed = max(0.0, 1.0 - min(latency_ms/5000.0, 1.0))
                nf = hotpath_adaptive_fitness(speed, 1.0 if success else 0.2, 0.2, 0.9, sk.use_count)
                sk.fitness = hotpath_ema_update(sk.fitness, nf, alpha=0.3)
                break

    def stats(self) -> dict:
        all_w = [e for v in self._working.values() for e in v]
        imps = [e.importance for e in all_w + self._episodic] or [0.0]
        return {
            "vitalis": _VITALIS,
            "working": len(all_w),
            "episodic": len(self._episodic),
            "skills": len(self._skills),
            "instructions": len(self._instructions),
            "mean_importance": round(hotpath_mean(imps), 3),
            "sessions": list(self._working.keys()),
        }

    # ── Persistence ───────────────────────────────────────────────────────────
    def _load_all(self):
        # Episodic
        if EPISODIC_PATH.exists():
            try:
                self._episodic = [MemoryEntry(**e) for e in json.loads(EPISODIC_PATH.read_text("utf-8"))]
            except Exception: self._episodic = []
        # Skills
        if SKILLS_PATH.exists():
            try:
                self._skills = [Skill(**s) for s in json.loads(SKILLS_PATH.read_text("utf-8"))]
            except Exception: pass
        if not self._skills:
            self._skills = _default_skills(); self._save_skills()
        # Instructions
        if INSTRUCT_PATH.exists():
            try:
                self._instructions = [Instruction(**i) for i in json.loads(INSTRUCT_PATH.read_text("utf-8"))]
            except Exception: pass
        if not self._instructions:
            self._instructions = _default_instructions(); self._save_instructions()

    def _save_episodic(self):
        try:
            if len(self._episodic) > self.MAX_EPISODIC:
                self._episodic.sort(key=lambda e: e.importance)
                self._episodic = self._episodic[-self.MAX_EPISODIC:]
            EPISODIC_PATH.write_text(json.dumps([asdict(e) for e in self._episodic], indent=2), "utf-8")
        except Exception as ex: print(f"  [Memory] Episodic save error: {ex}")

    def _save_skills(self):
        try: SKILLS_PATH.write_text(json.dumps([asdict(s) for s in self._skills], indent=2), "utf-8")
        except Exception: pass

    def _save_instructions(self):
        try: INSTRUCT_PATH.write_text(json.dumps([asdict(i) for i in self._instructions], indent=2), "utf-8")
        except Exception: pass

    def _is_dup(self, candidate: MemoryEntry, pool: list[MemoryEntry]) -> bool:
        if not candidate.embedding: return False
        for e in pool[-20:]:
            if e.embedding and hotpath_cosine_similarity(candidate.embedding, e.embedding) > self.DEDUP_SIM:
                e.importance = hotpath_ema_update(e.importance, 1.2, alpha=0.3)
                e.access_count += 1
                return True
        return False

# ── Defaults ──────────────────────────────────────────────────────────────────
def _default_skills() -> list[Skill]:
    return [
        Skill("code_review","code review python rust typescript quality security bug analysis",
              ["review","analyze","code","quality","security","improve","fix","bug"],
              "You are a senior code reviewer. Focus on correctness, security, performance, and readability. Provide specific actionable fixes with code examples.", fitness=0.8),
        Skill("architecture","system design architecture patterns microservices api database scalability",
              ["architect","design","system","structure","pattern","api","database","scale"],
              "You are a senior software architect. Think in components, interfaces, data flow, and scalability. Prefer simple, maintainable designs.", fitness=0.75),
        Skill("vitalis_expert","vitalis slang compiler jit evolution arena fitness tournament sl",
              ["vitalis","slang","sl","compile","jit","evolve","arena","fitness"],
              "Expert in Vitalis (.sl): statically typed, JIT executed. Types: i64, f64, bool, str. Syntax: fn name(a: i64) -> i64 { ... }. main() must return i64.", fitness=0.9),
        Skill("forge_meta","forge server api dashboard self-improve patch evolve bridge",
              ["forge","server","dashboard","api","endpoint","self-improve","evolve","patch"],
              "You operate within The Forge. APIs: /api/chat, /api/research, /api/build, /api/orchestrate, /api/self-improve, /api/quick-chat, /api/analyze-file.", fitness=0.85),
        Skill("debugging","debug error traceback exception crash fix problem root cause",
              ["error","exception","crash","debug","traceback","fails","broke","issue","problem"],
              "Debugging expert: 1) Identify error type 2) Trace root cause 3) Provide fix 4) Explain why 5) Prevention strategy.", fitness=0.8),
        Skill("research_synthesis","research synthesis consensus compare models multi-agent analysis",
              ["research","consensus","compare","synthesize","analyze","summarize"],
              "Research synthesis specialist: identify consensus points, note divergences, assess confidence, produce authoritative synthesis with caveats.", fitness=0.7),
    ]

def _default_instructions() -> list[Instruction]:
    return [
        Instruction("g001","Be concise and direct. Prefer code examples over long explanations.","global",9),
        Instruction("g002","Python code: use type hints, docstrings, PEP 8. No bare excepts.","global",7),
        Instruction("g003","Vitalis .sl code: explicit return types on all functions. main() returns i64.","global",8),
        Instruction("c001","Remember conversation context. Refer back to earlier points when relevant.","chat",6),
        Instruction("r001","Research: cite reasoning, note confidence levels, flag model disagreements.","research",8),
        Instruction("b001","Code gen: correctness first, then performance. Include a test main() function.","build",9),
    ]

def _age(s: float) -> str:
    if s < 60: return "now"
    if s < 3600: return f"{int(s/60)}m"
    if s < 86400: return f"{int(s/3600)}h"
    return f"{int(s/86400)}d"

# ── Singleton ─────────────────────────────────────────────────────────────────
_inst: MemoryEngine | None = None
def get_memory() -> MemoryEngine:
    global _inst
    if _inst is None: _inst = MemoryEngine()
    return _inst
