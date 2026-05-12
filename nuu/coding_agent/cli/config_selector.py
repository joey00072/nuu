"""
Interactive config selector UI. Uses Textual (if available) or a fallback
prompt to let users pick provider, model, and other session settings.

Owns: config selection UI logic, fallback to non-TUI mode.
Delegates to: textual for TUI, rich or input for fallback prompts.

Depends on: textual (optional), rich (optional), pathlib
"""

from __future__ import annotations

from pathlib import Path

try:
    from rich.console import Console
    from rich.prompt import Prompt
    from rich.table import Table

    _rich = True
except ImportError:
    _rich = False


def _config_dir() -> Path:
    return Path.home() / ".nuu" / "agent"


def _discover_config_files() -> list[Path]:
    cfg_dir = _config_dir()
    if not cfg_dir.exists():
        return []
    files: list[Path] = []
    for p in cfg_dir.iterdir():
        if p.suffix in (".json", ".yaml", ".yml", ".toml"):
            files.append(p)
    return sorted(files)


def _read_config_preview(path: Path, max_len: int = 80) -> str:
    try:
        text = path.read_text("utf-8").strip()
        if len(text) > max_len:
            text = text[:max_len] + "..."
        return text
    except Exception:
        return "(unreadable)"


def select_config_file() -> Path | None:
    files = _discover_config_files()
    if not files:
        print("No config files found in", _config_dir())
        return None

    if _rich:
        console = Console()
        table = Table(title="Config Files")
        table.add_column("#", style="cyan")
        table.add_column("File", style="green")
        table.add_column("Preview", style="dim")
        for i, f in enumerate(files, start=1):
            table.add_row(str(i), f.name, _read_config_preview(f))
        console.print(table)
        choice = Prompt.ask(
            f"Select config file (1-{len(files)}) or Enter to skip",
            default="",
        )
    else:
        print("\nConfig files in", _config_dir())
        for i, f in enumerate(files, start=1):
            preview = _read_config_preview(f)
            print(f"  {i}. {f.name}")
            if preview:
                print(f"     {preview}")
        choice = input(f"\nSelect config file (1-{len(files)}) or Enter to skip: ")

    if not choice:
        return None

    try:
        index = int(choice) - 1
        if 0 <= index < len(files):
            return files[index]
    except (ValueError, IndexError):
        pass

    print("Invalid selection")
    return None
