"""Skill discovery and loading for microclaw."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


DEFAULT_GLOBAL_SKILLS_DIR = os.environ.get(
    "MICROCLAW_SKILLS_DIR",
    os.path.expanduser("~/.microclaw/skills"),
)


@dataclass
class SkillRecord:
    name: str
    description: str
    tags: list[str]
    body: str
    path: str
    source: str


class SkillLoader:
    """Load skills from workspace-local and global skill directories."""

    def __init__(
        self,
        workspace: Path | None = None,
        global_dir: Path | None = None,
        local_dir: Path | None = None,
    ) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()
        self.global_dir = Path(global_dir or DEFAULT_GLOBAL_SKILLS_DIR).expanduser()
        self.local_dir = Path(local_dir or (self.workspace / "skills"))
        self.skills: dict[str, SkillRecord] = {}
        self.reload()

    def reload(self) -> None:
        """Rescan all configured skill directories."""
        self.skills = {}
        for base_dir, source in (
            (self.global_dir, "global"),
            (self.local_dir, "workspace"),
        ):
            if not base_dir.exists():
                continue
            for path in sorted(base_dir.rglob("SKILL.md")):
                record = self._read_skill(path, source)
                self.skills[record.name] = record

    def available_names(self) -> list[str]:
        return sorted(self.skills.keys())

    def get(self, name: str) -> SkillRecord | None:
        return self.skills.get(name)

    def get_descriptions_text(self, active_skills: list[str] | None = None) -> str:
        """Return compact skill metadata for the system prompt."""
        active_set = set(active_skills or [])
        if not self.skills:
            return "(no skills available)"

        lines: list[str] = []
        for name in self.available_names():
            skill = self.skills[name]
            line = f"- {name}: {skill.description or 'No description'}"
            if skill.tags:
                line += f" [{', '.join(skill.tags)}]"
            if name in active_set:
                line += " (active)"
            lines.append(line)
        return "\n".join(lines)

    def get_active_skill_prompt_blocks(self, active_skills: list[str]) -> list[str]:
        """Return full prompt blocks for explicitly activated skills."""
        blocks: list[str] = []
        for name in active_skills:
            skill = self.get(name)
            if not skill:
                continue
            blocks.append(
                f"Active skill: {skill.name}\n"
                f"Source: {skill.source}\n"
                f"Path: {skill.path}\n\n"
                f"{skill.body}"
            )
        return blocks

    def get_tool_content(self, name: str) -> str:
        """Return the full skill body for tool_result injection."""
        skill = self.get(name)
        if not skill:
            available = ", ".join(self.available_names()) or "(none)"
            return f"Error: Unknown skill '{name}'. Available: {available}"
        return (
            f"<skill name=\"{skill.name}\" source=\"{skill.source}\" path=\"{skill.path}\">\n"
            f"{skill.body}\n"
            f"</skill>"
        )

    def _read_skill(self, path: Path, source: str) -> SkillRecord:
        text = path.read_text(encoding="utf-8")
        meta, body = self._parse_frontmatter(text)
        name = str(meta.get("name") or path.parent.name)
        description = str(meta.get("description") or "").strip()
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        elif not isinstance(tags, list):
            tags = []
        return SkillRecord(
            name=name,
            description=description,
            tags=[str(tag) for tag in tags],
            body=body.strip(),
            path=str(path),
            source=source,
        )

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
        if not match:
            return {}, text
        try:
            meta = yaml.safe_load(match.group(1)) or {}
            if not isinstance(meta, dict):
                meta = {}
        except yaml.YAMLError:
            meta = {}
        return meta, match.group(2)
