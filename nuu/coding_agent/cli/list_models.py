"""
CLI command to list available models. Displays all registered models grouped
by provider with thinking level support info.

Owns: list_models() output formatting.
Delegates to: nuu.ai.models for model data.

Data flow: get_providers() -> get_models() -> formatted table output

Depends on: nuu.ai.models (get_models, get_providers)
"""

from __future__ import annotations

from ...ai.models import EXTENDED_THINKING_LEVELS, get_models, get_providers
from ...ai.types import ModelInfo

try:
    from rich.console import Console
    from rich.table import Table

    _rich = True
except ImportError:
    _rich = False


def format_token_count(count: int) -> str:
    if count >= 1_000_000:
        millions = count / 1_000_000
        return f"{int(millions)}M" if millions % 1 == 0 else f"{millions:.1f}M"
    if count >= 1_000:
        thousands = count / 1_000
        return f"{int(thousands)}K" if thousands % 1 == 0 else f"{thousands:.1f}K"
    return str(count)


def print_models_table(models: list[ModelInfo]) -> None:
    if not models:
        print("No models available")
        return

    rows: list[dict[str, str]] = []
    for m in models:
        has_thinking = (
            any(level != "off" for level in EXTENDED_THINKING_LEVELS)
            if m.reasoning
            else False
        )
        has_images = "image" in m.input
        rows.append(
            {
                "provider": m.provider,
                "model": m.id,
                "context": format_token_count(m.context_window),
                "max_out": format_token_count(m.max_tokens),
                "thinking": "yes" if has_thinking else "no",
                "images": "yes" if has_images else "no",
            }
        )

    rows.sort(key=lambda r: (r["provider"], r["model"]))

    headers = ["provider", "model", "context", "max_out", "thinking", "images"]
    widths: dict[str, int] = {}
    for h in headers:
        widths[h] = max(len(h), max((len(r[h]) for r in rows), default=0))

    if _rich:
        console = Console()
        table = Table()
        for h in headers:
            table.add_column(h, style="bold" if h == "model" else None)
        for r in rows:
            table.add_row(*[r[h] for h in headers])
        console.print(table)
    else:
        header_line = "  ".join(h.ljust(widths[h]) for h in headers)
        print(header_line)
        for r in rows:
            line = "  ".join(r[h].ljust(widths[h]) for h in headers)
            print(line)


def list_models(provider: str | None = None) -> None:
    providers = [provider] if provider else get_providers()
    if not providers:
        print("No providers registered")
        return

    all_models: list[ModelInfo] = []
    for p in providers:
        models = get_models(p)
        all_models.extend(models)

    if not all_models:
        print(f"No models found for {provider}" if provider else "No models found")
        return

    print_models_table(all_models)
