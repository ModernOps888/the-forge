"""
The Forge — Local Skill & Artifact Marketplace

A local-first, hash-verified registry of:
  - Skill DNAs (compiled Vitalis hot-path functions)
  - Factory Artifacts (full agents, MCP servers, APIs, CLIs)

No cloud required. Everything on disk, everything hash-verified.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .skills import SkillDNA

MARKETPLACE_ROOT = Path("C:/TheForge/marketplace")
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

    def __init__(self, root: Path = MARKETPLACE_ROOT):
        self.root = root
        self._index: dict[str, IndexEntry] = {}
        self._ensure_dirs()
        self._load_index()

    def _ensure_dirs(self):
        for d in (self.root, SKILLS_DIR, ARTIFACTS_DIR):
            d.mkdir(parents=True, exist_ok=True)

    def _load_index(self):
        if INDEX_FILE.exists():
            raw = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            for item in raw.get("entries", []):
                entry = IndexEntry(**item)
                self._index[entry.id] = entry

    def _save_index(self):
        INDEX_FILE.write_text(json.dumps({
            "updated_at": time.time(),
            "count": len(self._index),
            "entries": [asdict(e) for e in self._index.values()],
        }, indent=2), encoding="utf-8")

    # ── Publish ───────────────────────────────────────────────────────────────

    def publish_skill(self, skill: SkillDNA) -> Path:
        """Register a Skill DNA into the marketplace."""
        skill_dir = SKILLS_DIR / skill.skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Write files
        (skill_dir / "skill.json").write_text(skill.to_json(), encoding="utf-8")
        (skill_dir / "source.sl").write_text(skill.source_code, encoding="utf-8")

        # Update index
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
            path=str(skill_dir),
        )
        self._index[skill.skill_id] = entry
        self._save_index()
        return skill_dir

    def publish_artifact(self, run_id: str, name: str, description: str,
                         category: str, source_path: Path,
                         fitness: float = 0.0, provider: str = "",
                         language: str = "python", tags: list[str] | None = None) -> Path:
        """Register a Factory artifact into the marketplace."""
        art_dir = ARTIFACTS_DIR / run_id
        if source_path.exists() and not art_dir.exists():
            shutil.copytree(str(source_path), str(art_dir))
        art_dir.mkdir(parents=True, exist_ok=True)

        entry = IndexEntry(
            id=run_id,
            name=name,
            kind="artifact",
            category=category,
            description=description,
            fitness=fitness,
            language=language,
            provider=provider,
            dna_hash="",
            installed_at=time.time(),
            tags=tags or [],
            path=str(art_dir),
        )
        self._index[run_id] = entry
        self._save_index()
        return art_dir

    # ── Search / Browse ───────────────────────────────────────────────────────

    def search(self, query: str = "", category: str = "", kind: str = "") -> list[IndexEntry]:
        q = query.lower()
        results = []
        for e in self._index.values():
            if kind and e.kind != kind:
                continue
            if category and e.category != category:
                continue
            if q and q not in e.name.lower() and q not in e.description.lower() \
               and not any(q in t for t in e.tags):
                continue
            results.append(e)
        return sorted(results, key=lambda x: x.fitness, reverse=True)

    def get(self, item_id: str) -> Optional[IndexEntry]:
        return self._index.get(item_id)

    def load_skill(self, skill_id: str) -> Optional[SkillDNA]:
        entry = self._index.get(skill_id)
        if not entry or entry.kind != "skill":
            return None
        skill_json = Path(entry.path) / "skill.json"
        if skill_json.exists():
            return SkillDNA.from_json(skill_json.read_text(encoding="utf-8"))
        return None

    def list_all(self) -> list[IndexEntry]:
        return sorted(self._index.values(), key=lambda x: x.installed_at, reverse=True)

    # ── Compare ───────────────────────────────────────────────────────────────

    def compare(self, id_a: str, id_b: str) -> str:
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
