"""
YAML frontmatter parser for markdown files. Extracts metadata between ---
delimiters at the start of a file, returning both the parsed frontmatter
dict and the remaining markdown body.

Owns: parse_frontmatter().
Delegates to: json (simple fallback), standard library parsing.

Data flow: markdown string -> parse_frontmatter() -> (frontmatter dict, body str)

Depends on: standard library only (typing, re)
"""

from typing import Any, Dict, Tuple


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """
    Simplistic YAML frontmatter parser.
    Extracts key-value pairs between --- lines at the start of the file.
    """
    frontmatter = {}
    remaining_content = content

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1]
            remaining_content = parts[2]

            for line in fm_text.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip()
                    value = value.strip()

                    # Handle basic booleans and numbers
                    if value.lower() == "true":
                        value = True
                    elif value.lower() == "false":
                        value = False
                    elif value.isdigit():
                        value = int(value)

                    frontmatter[key] = value

    return frontmatter, remaining_content.strip()
