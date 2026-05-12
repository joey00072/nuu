"""
Project resource loader: discovers context files (CONTEXT.md, .ctx.md),
config files (.nuu/config.*), README.md, documentation, examples, and git
info from the project tree.

Owns: ResourceLoader class, context file discovery, git info extraction.
Delegates to: subprocess for git commands, pathlib for file I/O.

Data flow: cwd + agent_dir -> load_project_context_files() -> ContextFile list

Depends on: subprocess, pathlib
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class ContextFile:
    path: Path
    content: str
    priority: int = 0


@dataclass
class ConfigFile:
    path: Path
    content: str
    format: Literal["json", "yaml", "toml"]


@dataclass
class DocFile:
    path: Path
    content: str
    title: str


@dataclass
class ExampleFile:
    path: Path
    content: str


@dataclass
class GitInfo:
    branch: str
    root: Path
    has_uncommitted: bool


_CONTEXT_CANDIDATES = [".ctx.md", "CONTEXT.md", "CONTEXT.MD"]
_AGENTS_MD_CANDIDATES = ["AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"]


class ResourceLoader:
    def __init__(self, cwd: Path, agent_dir: Path) -> None:
        self._cwd = cwd.resolve()
        self._agent_dir = agent_dir.resolve()

    async def reload(self) -> None:
        pass

    def load_project_context_files(self) -> list[ContextFile]:
        seen: set[Path] = set()
        files: list[ContextFile] = []

        global_ctx = self._load_context_from_dir(self._agent_dir)
        if global_ctx is not None:
            files.append(global_ctx)
            seen.add(global_ctx.path)

        ancestor: list[ContextFile] = []
        current = self._cwd
        root = Path("/").resolve()

        while True:
            ctx = self._load_context_from_dir(current)
            if ctx is not None and ctx.path not in seen:
                ancestor.insert(0, ctx)
                seen.add(ctx.path)
            if current == root:
                break
            parent = current.parent
            if parent == current:
                break
            current = parent

        files.extend(ancestor)
        return files

    def load_agents_md(self) -> str | None:
        parts: list[str] = []
        seen: set[Path] = set()

        global_file = self._load_agents_md_from_dir(self._agent_dir)
        if global_file is not None:
            parts.append(global_file.content)
            seen.add(global_file.path)

        current = self._cwd
        root = Path("/").resolve()
        ancestors: list[ContextFile] = []

        while True:
            cf = self._load_agents_md_from_dir(current)
            if cf is not None and cf.path not in seen:
                ancestors.insert(0, cf)
                seen.add(cf.path)
            if current == root:
                break
            parent = current.parent
            if parent == current:
                break
            current = parent

        parts.extend(cf.content for cf in ancestors)
        return "\n\n".join(parts) if parts else None

    def load_system_md(self) -> str | None:
        candidates = [
            self._cwd / ".nuu" / "SYSTEM.md",
            self._cwd / ".nuu" / "SYSTEM.MD",
            self._agent_dir / "SYSTEM.md",
            self._agent_dir / "SYSTEM.MD",
        ]
        for path in candidates:
            if path.is_file():
                try:
                    return path.read_text("utf-8")
                except Exception:
                    return None
        return None

    def load_append_system_md(self) -> str | None:
        candidates = [
            self._cwd / ".nuu" / "APPEND_SYSTEM.md",
            self._cwd / ".nuu" / "APPEND_SYSTEM.MD",
            self._agent_dir / "APPEND_SYSTEM.md",
            self._agent_dir / "APPEND_SYSTEM.MD",
        ]
        for path in candidates:
            if path.is_file():
                try:
                    return path.read_text("utf-8")
                except Exception:
                    return None
        return None

    def load_config_files(self) -> list[ConfigFile]:
        config_dir = self._cwd / ".nuu"
        if not config_dir.is_dir():
            return []
        results: list[ConfigFile] = []
        for entry in sorted(config_dir.iterdir()):
            if not entry.is_file():
                continue
            name = entry.name
            fmt: Literal["json", "yaml", "toml"] | None = None
            if name.startswith("config."):
                if name.endswith(".json"):
                    fmt = "json"
                elif name.endswith(".yaml") or name.endswith(".yml"):
                    fmt = "yaml"
                elif name.endswith(".toml"):
                    fmt = "toml"
            elif name == "config":
                fmt = "json"
            if fmt is None:
                continue
            try:
                content = entry.read_text("utf-8")
            except Exception:
                continue
            results.append(ConfigFile(path=entry, content=content, format=fmt))
        return results

    def load_readme(self) -> str | None:
        candidates = ["README.md", "Readme.md", "readme.md"]
        for name in candidates:
            path = self._cwd / name
            if path.is_file():
                try:
                    return path.read_text("utf-8")
                except Exception:
                    return None
        return None

    def load_docs(self) -> list[DocFile]:
        doc_dirs = [self._cwd / "docs", self._cwd / "documentation"]
        results: list[DocFile] = []
        for d in doc_dirs:
            if not d.is_dir():
                continue
            for entry in sorted(d.rglob("*.md")):
                if not entry.is_file():
                    continue
                try:
                    content = entry.read_text("utf-8")
                except Exception:
                    continue
                title = entry.stem.replace("-", " ").replace("_", " ").title()
                results.append(DocFile(path=entry, content=content, title=title))
        return results

    def load_examples(self) -> list[ExampleFile]:
        example_dirs = [
            self._cwd / "examples",
            self._cwd / "example",
            self._cwd / "samples",
        ]
        results: list[ExampleFile] = []
        for d in example_dirs:
            if not d.is_dir():
                continue
            for entry in sorted(d.rglob("*")):
                if not entry.is_file():
                    continue
                try:
                    content = entry.read_text("utf-8")
                except Exception:
                    continue
                results.append(ExampleFile(path=entry, content=content))
        return results

    def resolve_glob(self, pattern: str) -> list[Path]:
        return sorted(self._cwd.glob(pattern))

    def get_project_git_info(self) -> GitInfo | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(self._cwd),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            root = Path(result.stdout.strip())
        except Exception:
            return None

        branch: str = ""
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                branch = r.stdout.strip()
        except Exception:
            pass

        has_uncommitted = False
        try:
            r = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                has_uncommitted = bool(r.stdout.strip())
        except Exception:
            pass

        return GitInfo(branch=branch, root=root, has_uncommitted=has_uncommitted)

    def _load_agents_md_from_dir(self, directory: Path) -> ContextFile | None:
        for name in _AGENTS_MD_CANDIDATES:
            path = directory / name
            if path.is_file():
                try:
                    content = path.read_text("utf-8")
                    return ContextFile(path=path, content=content)
                except Exception:
                    return None
        return None

    def _load_context_from_dir(self, directory: Path) -> ContextFile | None:
        for name in _CONTEXT_CANDIDATES:
            path = directory / name
            if path.is_file():
                try:
                    content = path.read_text("utf-8")
                    return ContextFile(path=path, content=content)
                except Exception:
                    return None
        return None
