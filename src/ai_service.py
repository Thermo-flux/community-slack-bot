"""
==============================================================================
AI REASONING SERVICE MODULE (src/ai_service.py)
==============================================================================
"""

import json
import logging
import requests
from typing import Optional
from config import (
    AI_PROVIDER,
    AI_API_KEY,
    AI_MODEL,
    AI_BASE_URL,
    SOUL_PATH,
    CONFIG
)

logger = logging.getLogger("SlackBotAI")


def load_soul_prompt() -> str:
    if SOUL_PATH.exists():
        try:
            with open(SOUL_PATH, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            logger.warning(f"Could not read soul.md: {e}. Using fallback persona.")

    return (
        "You are Community Champion Bot, a friendly, encouraging Slack bot that tracks "
        "user contributions, rewards positive ideas, and keeps conversations constructive."
    )


def build_system_instructions() -> str:
    base_soul = load_soul_prompt()
    emoji_cfg = CONFIG.get("emoji_settings", {})
    frequency = emoji_cfg.get("frequency", "moderate")
    allowed_emojis = emoji_cfg.get("allowed_emojis", ["🎉", "⭐", "🚀", "💡", "🔥", "✨"])

    emoji_instruction = (
        f"\n\nEMOJI RULES:\n"
        f"- Target emoji frequency: {frequency}\n"
        f"- Only use these approved emojis where appropriate: {' '.join(allowed_emojis)}\n"
        f"- Keep responses concise and formatted cleanly with Slack Markdown (*bold*, _italic_, `code`)."
    )

    # Inject live community data so AI can accurately answer queries about any user's stats
    community_context = ""
    try:
        from database import get_all_users_summary
        from leveling import get_level_tiers

        tiers = get_level_tiers()
        tier_lines = "\n".join([f"  • Level {t['level']}: {t['min_xp']} XP ({t['title']})" for t in tiers])

        users_summary = get_all_users_summary(limit=20)
        user_lines = []
        for u in users_summary:
            alias_part = f" (aliases: {u['aliases']})" if u.get("aliases") else ""
            user_lines.append(
                f"  • <@{u['user_id']}>{alias_part}: Level {u['level']}, {u['xp']} XP, {u['message_count']} contributions"
            )

        community_context = (
            f"\n\nLIVE COMMUNITY SYSTEM & LEADERBOARD DATA:\n"
            f"- Anyone in the channel is allowed to view anyone's level, points, rank, or card. This is public, non-sensitive community data.\n"
            f"- Level Progression Tiers:\n{tier_lines}\n"
            f"- Current Tracked Members:\n" + ("\n".join(user_lines) if user_lines else "  • No activity recorded yet.") + "\n"
            f"- If a user asks for anyone's card, level, XP, or ranking (such as for Netic, Jerry, or others), you have access to the data above. Accurately report their stats and celebrate their progress!"
        )
    except Exception as e:
        logger.warning(f"Could not load live community context into AI prompt: {e}")

    return base_soul + emoji_instruction + community_context


def get_offline_fallback(user_id: str) -> str:
    templates = CONFIG.get("templates", {})
    fallback = templates.get(
        "ai_offline_fallback",
        "Hey <@{user_id}>! 👋 I received your message! My AI reasoning engine is currently in standby mode, but I am actively monitoring this channel and recording your activity points! 🚀"
    )
    return fallback.format(user_id=user_id)


def generate_response(user_id: str, user_message: str, channel_id: Optional[str] = None, timeout: int = 15) -> str:
    if not AI_API_KEY or AI_PROVIDER == "mock":
        return get_offline_fallback(user_id)

    system_prompt = build_system_instructions()

    try:
        if AI_PROVIDER == "gemini":
            return _call_gemini(user_message, system_prompt, timeout)
        elif AI_PROVIDER in ("openai", "openrouter", "custom"):
            return _call_openai_compatible(user_message, system_prompt, timeout)
        elif AI_PROVIDER == "anthropic":
            return _call_anthropic(user_message, system_prompt, timeout)
        else:
            return get_offline_fallback(user_id)
    except Exception as e:
        logger.error(f"Error calling AI provider '{AI_PROVIDER}': {e}. Returning safe fallback.")
        return get_offline_fallback(user_id)


def _call_gemini(prompt: str, system_prompt: str, timeout: int) -> str:
    model = AI_MODEL or "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={AI_API_KEY}"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 400, "temperature": 0.7}
    }
    response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout)
    if response.status_code == 200:
        candidates = response.json().get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                text = parts[0].get("text") or ""
                if text.strip():
                    return text.strip()
        return "I'm here and listening! Keep up the great discussion! 🚀"
    raise RuntimeError(f"Gemini API status {response.status_code}")


def _call_openai_compatible(prompt: str, system_prompt: str, timeout: int) -> str:
    model = AI_MODEL
    base_url = AI_BASE_URL
    if AI_PROVIDER == "openrouter":
        base_url = base_url or "https://openrouter.ai/api/v1"
        model = model or "meta-llama/llama-3-8b-instruct:free"
    else:
        base_url = base_url or "https://api.openai.com/v1"
        model = model or "gpt-4o-mini"

    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
        "max_tokens": 400,
        "temperature": 0.7
    }
    response = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
    if response.status_code == 200:
        choices = response.json().get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            content = msg.get("content") or ""
            if not content and "reasoning_content" in msg:
                content = msg.get("reasoning_content") or ""
            if content.strip():
                return content.strip()
        return "I'm listening and tracking your activity! 🚀"
    raise RuntimeError(f"OpenAI API status {response.status_code}")


def _call_anthropic(prompt: str, system_prompt: str, timeout: int) -> str:
    model = AI_MODEL or "claude-3-5-haiku-20241022"
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": AI_API_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "system": system_prompt,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 400
    }
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if response.status_code == 200:
        content = response.json().get("content", [])
        if content and content[0].get("type") == "text":
            text = content[0].get("text") or ""
            if text.strip():
                return text.strip()
        return "I'm listening and tracking your activity! 🚀"
    raise RuntimeError(f"Anthropic API status {response.status_code}")

