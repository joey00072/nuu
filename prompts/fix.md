---
description: Fix issues identified in a code review
argument-hint: "<issue description>"
---
You are fixing issues identified in a code review.

Issue: {issue}

Steps:
1. Read the relevant files fully
2. Understand the root cause
3. Apply the minimal fix
4. Run `uv run ruff check .` and `uv run pytest -x`
5. Fix any failures
