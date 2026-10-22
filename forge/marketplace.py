"""
The Forge — Local Skill & Artifact Marketplace

A local-first, hash-verified registry of:
  - Skill DNAs (compiled Vitalis hot-path functions)
  - Factory Artifacts (full agents, MCP servers, APIs, CLIs)

No cloud required. Everything on disk, everything hash-verified.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .skills import SkillDNA, register as register_skill_in_memory

MARKETPLACE_ROOT = Path(__file__).resolve().parent.parent / "marketplace"
INDEX_FILE = MARKETPLACE_ROOT / "index.json"
SKILLS_DIR = MARKETPLACE_ROOT / "skills"
ARTIFACTS_DIR = MARKETPLACE_ROOT / "artifacts"


# ── Index Entry ───────────────────────────────────────────────────────────────

@dataclass
class IndexEntry:
    id: str
    name: str
    kind: str           # "skill" | "artifact"
    category: str
    description: str
    fitness: float
    language: str
    provider: str
    dna_hash: str
    installed_at: float
    tags: list[str]
    path: str


# ── Registry ──────────────────────────────────────────────────────────────────

class Marketplace:
    """Local marketplace — search, install, publish, compare Forge artifacts."""

    def __init__(self, root: Path | str = MARKETPLACE_ROOT):
        self.root = Path(root)
        self.index_file = self.root / "index.json"
        self.skills_dir = self.root / "skills"
        self.artifacts_dir = self.root / "artifacts"
        self._lock = threading.RLock()
        self._index: dict[str, IndexEntry] = {}
        self._ensure_dirs()
        self._load_index()

    def _ensure_dirs(self):
        for d in (self.root, self.skills_dir, self.artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _load_index(self):
        with self._lock:
            if not self.index_file.exists():
                return
            try:
                raw = json.loads(self.index_file.read_text(encoding="utf-8"))
                valid_fields = set(IndexEntry.__dataclass_fields__.keys())
                for item in raw.get("entries", []):
                    if isinstance(item, dict):
                        filtered = {k: v for k, v in item.items() if k in valid_fields}
                        entry = IndexEntry(**filtered)
                        self._index[entry.id] = entry
                        if entry.kind == "skill":
                            try:
                                sk_json = Path(entry.path) / "skill.json"
                                if sk_json.exists():
                                    sk = SkillDNA.from_json(sk_json.read_text(encoding="utf-8"))
                                    if sk.verify_integrity():
                                        register_skill_in_memory(sk)
                            except Exception:
                                pass
            except (json.JSONDecodeError, KeyError, TypeError):
                self._index = {}

    def _save_index(self):
        with self._lock:
            tmp = self.index_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "updated_at": time.time(),
                "count": len(self._index),
                "entries": [asdict(e) for e in self._index.values()],
            }, indent=2), encoding="utf-8")
            tmp.replace(self.index_file)

    # ── Publish ───────────────────────────────────────────────────────────────

    def publish_skill(self, skill: SkillDNA) -> Path:
        """Register a Skill DNA into the marketplace."""
        with self._lock:
            skill_dir = self.skills_dir / skill.skill_id
            skill_dir.mkdir(parents=True, exist_ok=True)

            (skill_dir / "skill.json").write_text(skill.to_json(), encoding="utf-8")
            (skill_dir / "source.sl").write_text(skill.source_code, encoding="utf-8")
            register_skill_in_memory(skill)

            try:
                rel_skill_path = str(skill_dir.relative_to(self.root)).replace("\\", "/")
            except ValueError:
                rel_skill_path = str(skill_dir)

            entry = IndexEntry(
                id=skill.skill_id,
                name=skill.name,
                kind="skill",
                category=skill.category,
                description=skill.description,
                fitness=skill.fitness_score,
                language="vitalis",
                provider=skill.champion_provider,
                dna_hash=skill.dna_hash,
                installed_at=time.time(),
                tags=skill.tags,
                path=rel_skill_path,
            )
            self._index[skill.skill_id] = entry
            self._save_index()
            return skill_dir

    def publish_artifact(
        self,
        run_id_or_artifact=None,
        name: str = "",
        description: str = "",
        category: str = "",
        source_path: Optional[Path] = None,
        fitness: float = 0.0,
        provider: str = "",
        language: str = "python",
        tags: list[str] | None = None,
        run_id: str = "",
        artifact_type: str = "",
        source_code: str = "",
    ) -> IndexEntry:
        """Register a Factory artifact into the marketplace.
        Accepts either an `Artifact` object, positional `(run_id, name, ...)`, or keyword args `(name=..., source_code=...)`.
        """
        with self._lock:
            if hasattr(run_id_or_artifact, "artifact_id") and hasattr(run_id_or_artifact, "source"):
                art = run_id_or_artifact
                rid = run_id or art.artifact_id
                name = name or f"{art.artifact_type}_{rid}"
                description = description or art.description
                category = category or art.artifact_type or artifact_type or "artifact"
                fitness = fitness or art.score_total
                provider = provider or art.provider_name
                language = art.language or language
                source_text = art.source or source_code or ""
                art_dir = self.artifacts_dir / rid
                art_dir.mkdir(parents=True, exist_ok=True)
                art_dict = art.to_dict() if hasattr(art, "to_dict") else {
                    "artifact_id": rid,
                    "artifact_type": category,
                    "language": language,
                    "description": description,
                    "provider_name": provider,
                    "score_total": fitness,
                }
                (art_dir / "artifact.json").write_text(json.dumps(art_dict, indent=2), encoding="utf-8")
                (art_dir / "source.txt").write_text(source_text, encoding="utf-8")
            else:
                rid = str(run_id_or_artifact or run_id or f"art_{int(time.time() * 1000)}")
                category = category or artifact_type or "artifact"
                art_dir = self.artifacts_dir / rid
                source_text = source_code or ""
                if source_path is not None:
                    source_path = Path(source_path)
                    if source_path.is_dir() and not art_dir.exists():
                        shutil.copytree(str(source_path), str(art_dir))
                    elif source_path.is_file():
                        art_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(source_path), str(art_dir / source_path.name))
                        source_text = source_path.read_text(encoding="utf-8", errors="replace")
                art_dir.mkdir(parents=True, exist_ok=True)
                if source_text:
                    (art_dir / "source.txt").write_text(source_text, encoding="utf-8")

            dna_hash = hashlib.sha256(
                f"{rid}|{name}|{source_text}|{fitness:.4f}".encode("utf-8")
            ).hexdigest()

            try:
                rel_art_path = str(art_dir.relative_to(self.root)).replace("\\", "/")
            except ValueError:
                rel_art_path = str(art_dir)

            entry = IndexEntry(
                id=rid,
                name=name,
                kind="artifact",
                category=category,
                description=description,
                fitness=fitness,
                language=language,
                provider=provider,
                dna_hash=dna_hash,
                installed_at=time.time(),
                tags=tags or [],
                path=rel_art_path,
            )
            self._index[rid] = entry
            self._save_index()
            return entry

    # ── Search / Browse ───────────────────────────────────────────────────────

    def search(self, query: str = "", category: str = "", kind: str = "") -> list[IndexEntry]:
        with self._lock:
            q = query.lower()
            results = []
            for e in self._index.values():
                if kind and e.kind != kind:
                    continue
                if category and e.category != category:
                    continue
                if (
                    q
                    and q not in e.name.lower()
                    and q not in e.description.lower()
                    and not any(q in t.lower() for t in e.tags)
                ):
                    continue
                results.append(e)
            return sorted(results, key=lambda x: x.fitness, reverse=True)

    def get(self, item_id: str) -> Optional[IndexEntry]:
        with self._lock:
            return self._index.get(item_id)

    def load_skill(self, skill_id: str, verify: bool = True) -> Optional[SkillDNA]:
        with self._lock:
            entry = self._index.get(skill_id)
            if not entry or entry.kind != "skill":
                return None
            skill_dir = Path(entry.path) if Path(entry.path).is_absolute() else (self.root / entry.path)
            skill_json = skill_dir / "skill.json"
            if skill_json.exists():
                skill = SkillDNA.from_json(skill_json.read_text(encoding="utf-8"))
                if verify and not skill.verify_integrity():
                    raise ValueError(f"Skill DNA integrity verification failed for '{skill_id}'")
                return skill
            return None

    def list_all(self) -> list[IndexEntry]:
        with self._lock:
            return sorted(self._index.values(), key=lambda x: x.installed_at, reverse=True)

    # ── Compare ───────────────────────────────────────────────────────────────

    def compare(self, id_a: str, id_b: str) -> str:
        with self._lock:
            a, b = self._index.get(id_a), self._index.get(id_b)
            if not a or not b:
                return "One or both items not found"
            lines = [
                f"  {'─'*55}",
                f"  Comparison: {a.name}  vs  {b.name}",
                f"  {'─'*55}",
                f"  {'Fitness':15s}│ {a.fitness:>8.1f}  │ {b.fitness:>8.1f}",
                f"  {'Provider':15s}│ {a.provider[:14]:>14s}  │ {b.provider[:14]:>14s}",
                f"  {'Language':15s}│ {a.language:>14s}  │ {b.language:>14s}",
                f"  {'Kind':15s}│ {a.kind:>14s}  │ {b.kind:>14s}",
                f"  {'─'*55}",
                f"  Winner: {'A — ' + a.name if a.fitness >= b.fitness else 'B — ' + b.name}",
            ]
            return "\n".join(lines)

    def summary(self) -> str:
        with self._lock:
            skills = [e for e in self._index.values() if e.kind == "skill"]
            artifacts = [e for e in self._index.values() if e.kind == "artifact"]
            return (
                f"  Marketplace: {len(self._index)} items "
                f"({len(skills)} skills, {len(artifacts)} artifacts)\n"
                f"  Root: {self.root}"
            )


# ── Global Singleton ──────────────────────────────────────────────────────────

_mp: Optional[Marketplace] = None


def get_marketplace() -> Marketplace:
    global _mp
    if _mp is None:
        _mp = Marketplace()
    return _mp
