"""
Data format migrations for the agent directory. Handles upgrading legacy
auth/session files to the current format. Each Migration has a version,
description, and run function.

Owns: Migration model, run_migrations(), version comparison.
Delegates to: json, shutil for file operations.

Data flow: agent_dir -> get_current_data_version() -> run_migrations() ->
  updated files + bumped data_version

Depends on: standard library only (json, os, pathlib, stat, dataclasses)
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


_DATA_VERSION_FILE = "data_version"


@dataclass
class Migration:
    version: str
    description: str
    run: Callable[[Path], None]


def get_current_data_version(agent_dir: Path) -> str:
    path = agent_dir / _DATA_VERSION_FILE
    if path.is_file():
        try:
            return path.read_text("utf-8").strip()
        except Exception:
            pass
    return "0.0.0"


def set_current_data_version(agent_dir: Path, version: str) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / _DATA_VERSION_FILE
    path.write_text(f"{version}\n", "utf-8")


def migrate_auth_json_v1(agent_dir: Path) -> None:
    auth_path = agent_dir / "auth.json"
    oauth_path = agent_dir / "oauth.json"
    settings_path = agent_dir / "settings.json"

    if auth_path.exists():
        return

    migrated: dict[str, object] = {}

    if oauth_path.exists():
        try:
            oauth = json.loads(oauth_path.read_text("utf-8"))
            for provider, cred in oauth.items():
                migrated[provider] = {
                    "type": "oauth",
                    **(cred if isinstance(cred, dict) else {}),
                }
            oauth_path.rename(oauth_path.with_suffix(oauth_path.suffix + ".migrated"))
        except Exception:
            pass

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text("utf-8"))
            api_keys = settings.get("apiKeys")
            if isinstance(api_keys, dict):
                for provider, key in api_keys.items():
                    if provider not in migrated and isinstance(key, str):
                        migrated[provider] = {"type": "api_key", "key": key}
                del settings["apiKeys"]
                settings_path.write_text(json.dumps(settings, indent=2), "utf-8")
        except Exception:
            pass

    if migrated:
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text(json.dumps(migrated, indent=2), "utf-8")
        os.chmod(str(auth_path), stat.S_IRUSR | stat.S_IWUSR)


def migrate_session_format_v1(agent_dir: Path) -> None:
    files: list[Path] = []
    try:
        for entry in agent_dir.iterdir():
            if entry.is_file() and entry.suffix == ".jsonl":
                files.append(entry)
    except Exception:
        return

    if not files:
        return

    sessions_dir = agent_dir / "sessions"

    for file in files:
        try:
            first_line = file.read_text("utf-8").split("\n")[0]
            if not first_line or not first_line.strip():
                continue
            header = json.loads(first_line)
            if not isinstance(header, dict) or header.get("type") != "session":
                continue
            cwd = header.get("cwd")
            if not isinstance(cwd, str) or not cwd:
                continue

            safe = (
                "--"
                + cwd.lstrip("/\\")
                .replace("/", "-")
                .replace("\\", "-")
                .replace(":", "-")
                + "--"
            )
            correct_dir = sessions_dir / safe
            correct_dir.mkdir(parents=True, exist_ok=True)

            new_path = correct_dir / file.name
            if new_path.exists():
                continue

            file.rename(new_path)
        except Exception:
            pass


_MIGRATIONS: list[Migration] = [
    Migration(
        version="1.0.0",
        description="Migrate legacy oauth.json and settings.json apiKeys to auth.json",
        run=migrate_auth_json_v1,
    ),
    Migration(
        version="1.1.0",
        description="Move session .jsonl files from agent root to sessions/ subdirectory",
        run=migrate_session_format_v1,
    ),
]


def run_migrations(agent_dir: Path) -> list[str]:
    current = get_current_data_version(agent_dir)
    applied: list[str] = []

    for migration in _MIGRATIONS:
        if _compare_versions(migration.version, current) <= 0:
            continue
        migration.run(agent_dir)
        set_current_data_version(agent_dir, migration.version)
        applied.append(migration.version)

    return applied


def _compare_versions(a: str, b: str) -> int:
    parts_a = [int(x) for x in a.split(".")]
    parts_b = [int(x) for x in b.split(".")]
    max_len = max(len(parts_a), len(parts_b))
    parts_a.extend([0] * (max_len - len(parts_a)))
    parts_b.extend([0] * (max_len - len(parts_b)))
    for va, vb in zip(parts_a, parts_b):
        if va < vb:
            return -1
        if va > vb:
            return 1
    return 0
