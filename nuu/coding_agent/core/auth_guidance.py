"""
Auth setup guidance strings shown by /login and CLI when a provider has no
configured credentials. Maps provider names to human-readable instructions.

Owns: GUIDANCE dict.
Delegates to: nothing (static data only).

Depends on: nothing (standard library only)
"""

from __future__ import annotations

GUIDANCE: dict[str, str] = {
    "amazon-bedrock": "Configure AWS credentials via the AWS CLI or set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.",
    "anthropic": "Set the ANTHROPIC_API_KEY environment variable or use /login anthropic to configure authentication.",
    "azure-openai-responses": "Set the AZURE_OPENAI_API_KEY environment variable or use /login azure-openai-responses to configure authentication.",
    "cerebras": "Set the CEREBRAS_API_KEY environment variable or use /login cerebras to configure authentication.",
    "cloudflare-ai-gateway": "Set the CLOUDFLARE_API_TOKEN environment variable or use /login cloudflare-ai-gateway to configure authentication.",
    "cloudflare-workers-ai": "Set the CLOUDFLARE_API_TOKEN environment variable or use /login cloudflare-workers-ai to configure authentication.",
    "deepseek": "Set the DEEPSEEK_API_KEY environment variable or use /login deepseek to configure authentication.",
    "fireworks": "Set the FIREWORKS_API_KEY environment variable or use /login fireworks to configure authentication.",
    "github-copilot": "Authenticate via GitHub CLI (`gh auth login`) or set GITHUB_TOKEN environment variable.",
    "google": "Set the GOOGLE_API_KEY environment variable or use /login google to configure authentication.",
    "google-vertex": "Configure Google Application Default Credentials via gcloud CLI or use /login google-vertex.",
    "groq": "Set the GROQ_API_KEY environment variable or use /login groq to configure authentication.",
    "huggingface": "Set the HUGGINGFACE_API_KEY environment variable or use /login huggingface to configure authentication.",
    "kimi-coding": "Set the KIMI_API_KEY environment variable or use /login kimi-coding to configure authentication.",
    "minimax": "Set the MINIMAX_API_KEY environment variable or use /login minimax to configure authentication.",
    "minimax-cn": "Set the MINIMAX_API_KEY environment variable or use /login minimax-cn to configure authentication.",
    "mistral": "Set the MISTRAL_API_KEY environment variable or use /login mistral to configure authentication.",
    "moonshotai": "Set the MOONSHOT_API_KEY environment variable or use /login moonshotai to configure authentication.",
    "moonshotai-cn": "Set the MOONSHOT_API_KEY environment variable or use /login moonshotai-cn to configure authentication.",
    "openai": "Set the OPENAI_API_KEY environment variable or use /login openai to configure authentication.",
    "openai-codex": "Set the OPENAI_API_KEY environment variable or use /login openai-codex to configure authentication.",
    "opencode": "Set the OPENCODE_API_KEY environment variable or use /login opencode to configure authentication.",
    "opencode-go": "Set the OPENCODE_API_KEY environment variable or use /login opencode-go to configure authentication.",
    "openrouter": "Set the OPENROUTER_API_KEY environment variable or use /login openrouter to configure authentication.",
    "vercel-ai-gateway": "Set the VERCEL_AI_GATEWAY_API_KEY environment variable or use /login vercel-ai-gateway to configure authentication.",
    "xai": "Set the XAI_API_KEY environment variable or use /login xai to configure authentication.",
    "xiaomi": "Set the XIAOMI_API_KEY environment variable or use /login xiaomi to configure authentication.",
    "xiaomi-token-plan-ams": "Set the XIAOMI_API_KEY environment variable or use /login xiaomi-token-plan-ams to configure authentication.",
    "xiaomi-token-plan-cn": "Set the XIAOMI_API_KEY environment variable or use /login xiaomi-token-plan-cn to configure authentication.",
    "xiaomi-token-plan-sgp": "Set the XIAOMI_API_KEY environment variable or use /login xiaomi-token-plan-sgp to configure authentication.",
    "zai": "Set the ZAI_API_KEY environment variable or use /login zai to configure authentication.",
}


def get_auth_guidance(provider: str) -> str:
    return GUIDANCE.get(
        provider,
        f"No authentication guidance available for {provider}. Check the documentation for setup instructions.",
    )
