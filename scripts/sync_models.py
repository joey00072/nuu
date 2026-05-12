#!/usr/bin/env python3
"""Sync nuu/ai/models.json from Pi's models.generated.ts.

Usage:
    python3 scripts/sync_models.py

Reads:  ref/pi/packages/ai/src/models.generated.ts
Writes: nuu/ai/models.json
"""

import json
import pathlib
import re
import sys


def parse_pi_models(ts_path: pathlib.Path) -> dict:
    content = ts_path.read_text()

    # Strip full-line comments (avoid stripping // inside URL strings)
    content = re.sub(r"^\s*//[^\n]*\n", "\n", content, flags=re.MULTILINE)
    content = re.sub(r"import [^\n]+\n", "\n", content)
    content = re.sub(r"export const MODELS\s*=\s*", "", content)
    content = re.sub(r"\}\s+as\s+const\s*;?\s*$", "}", content.rstrip())
    # Remove "} satisfies Model<...>" type assertions
    content = re.sub(r"\}\s*satisfies\s+Model<[^>]+>", "}", content)

    # Quote unquoted TS identifier keys (e.g. `id:` -> `"id":`)
    def quote_key(line: str) -> str:
        m = re.match(r"^(\t+)([a-zA-Z_][a-zA-Z0-9_]*)(\s*:)", line)
        if m:
            return m.group(1) + '"' + m.group(2) + '"' + m.group(3) + line[m.end():]
        return line

    lines = [quote_key(l) for l in content.split("\n")]
    content = "\n".join(lines)

    # Remove trailing commas (TS allows, JSON doesn't)
    content = re.sub(r",(\s*\n\s*[}\]])", r"\1", content)

    return json.loads(content)


def convert_model(model: dict) -> dict:
    """Convert Pi model dict keys to nuu's camelCase JSON format (already camelCase)."""
    return model


def main() -> None:
    repo_root = pathlib.Path(__file__).parent.parent
    ts_path = repo_root / "ref" / "pi" / "packages" / "ai" / "src" / "models.generated.ts"
    out_path = repo_root / "nuu" / "ai" / "models.json"

    if not ts_path.exists():
        print(f"ERROR: {ts_path} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Reading {ts_path} ...")
    data = parse_pi_models(ts_path)

    total = sum(len(v) for v in data.values())
    print(f"Parsed {len(data)} providers, {total} models")

    print(f"Writing {out_path} ...")
    out_path.write_text(json.dumps(data, indent=2) + "\n")
    print("Done.")


if __name__ == "__main__":
    main()
