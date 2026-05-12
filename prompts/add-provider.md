---
description: Add a new LLM provider
argument-hint: "<provider-name>"
---
You are adding a new LLM provider: {name}

Follow the checklist in AGENTS.md:
1. Add to KnownApi/KnownProvider in nuu/ai/types.py
2. Create provider file in nuu/ai/providers/{name}.py
3. Register in nuu/ai/providers/register_builtins.py
4. Add env vars in nuu/ai/env_api_keys.py
5. Add auth guidance in nuu/coding_agent/core/auth_guidance.py
6. Add display name in nuu/coding_agent/core/provider_display_names.py
