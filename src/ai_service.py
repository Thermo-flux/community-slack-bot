"""
==============================================================================
AI REASONING SERVICE MODULE (src/ai_service.py)
==============================================================================
Provides intelligent, multi-turn AI reasoning powered by DeepSeek / OpenAI / Gemini
with automatic conversation memory, thinking scratchpad cleanup, and live community stats.
==============================================================================
"""

import os
import re
import json
import logging
import requests
from typing import Optional, List, Dict, Any
from config import (
    AI_PROVIDER,
    AI_API_KEY,
    AI_MODEL,
    AI_BASE_URL,
    SOUL_PATH,
    CONFIG
)

logger = logging.getLogger("SlackBotAI")


def clean_ai_output(text: str) -> str:
    """
    Cleans AI output thoroughly:
    1. Strips internal chain-of-thought scratchpads and <think> tags.
    2. Strips unclosed <think> blocks if cut off during reasoning.
    3. Strips 'Thinking Process:' or 'Thought:' prefixes and suffixes.
    4. Converts standard Markdown links [Label](URL) to Slack links <URL|Label>.
    5. Converts Markdown headers ### to Slack *bold*.
    6. Converts Markdown bold **text** to Slack *text*.
    """
    if not text:
        return ""

    # 1. Strip completed <think>...</think> blocks
    if "</think>" in text:
        text = text.split("</think>")[-1]

    # 2. Strip unclosed <think>... if cut off while thinking
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)

    # 3. Strip leading and trailing internal scratchpad notes
    text = re.sub(r"^(?:Thinking Process|Thought|Reasoning|Internal):\s*.*?\n\n", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"\n+(?:Thinking Process|Thought|Reasoning|Internal):.*$", "", text, flags=re.DOTALL | re.IGNORECASE)

    # 4. Convert Markdown links [Label](URL) to Slack links <URL|Label>
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"<\2|\1>", text)

    # 5. Convert Markdown headers (### Header) to Slack bold (*Header*)
    text = re.sub(r"^[ \t]*#{1,4}[ \t]+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # 6. Convert Markdown bold-italic ***text*** to Slack *_text_* and bold **text** to *text*
    text = re.sub(r"\*\*\*([^*\n]+)\*\*\*", r"*_\1_*", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", text)

    return text.strip()


def load_soul_prompt() -> str:
    if SOUL_PATH.exists():
        try:
            with open(SOUL_PATH, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            logger.warning(f"Could not read soul.md: {e}. Using fallback persona.")

    return (
        "You are goose-lee, a friendly, encouraging Slack bot that tracks "
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
        f"- Keep responses concise and formatted cleanly with Slack Markdown (*bold*, _italic_, `code`).\n"
        f"- Be direct, casual, and natural. NEVER output your internal reasoning, scratchpad, or <think> tags."
    )

    # Inject live community data and repo links
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
            f"\n\nLIVE COMMUNITY & PROJECT CONTEXT:\n"
            f"- Bot Identity: goose-lee (@gooselee), resident hacker and cheerleader in Hack Club's `#loadout-development` channel.\n"
            f"- GitHub Repository: <https://github.com/Thermo-flux/community-slack-bot|Thermo-flux/community-slack-bot>. Drop this link ONLY if someone asks for our bot's code or repository! When someone talks about their own project, ask for their project link instead.\n"
            f"- Anyone in the channel is allowed to view anyone's level, points, rank, or card. This is public, non-sensitive data.\n"
            f"- Level Progression Tiers:\n{tier_lines}\n"
            f"- Current Tracked Members:\n" + ("\n".join(user_lines) if user_lines else "  • No activity recorded yet.") + "\n"
            f"- If a user asks for anyone's card, level, XP, or ranking (such as for Netic, Jerry, or others), you have access to the data above. Accurately report their stats and celebrate their progress!\n"
            f"- Speak naturally, punchy, with casual builder slang ('solid W', 'let's cook', 'my bad', 'Ja, ja') and high builder energy. Never output internal thought tags or robotic corporate disclaimers."
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


def build_messages_payload(
    system_prompt: str,
    user_message: str,
    conversation_history: Optional[List[Dict[str, str]]] = None
) -> List[Dict[str, str]]:
    """Builds an OpenAI-compatible messages array with system prompt and conversation history."""
    messages = [{"role": "system", "content": system_prompt}]

    if conversation_history:
        for turn in conversation_history:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                # Skip duplicate of the current user message if it is already at the end
                if role == "user" and content == user_message.strip():
                    continue
                messages.append({"role": role, "content": content})

    # Add current user prompt
    clean_prompt = user_message.strip()
    if not messages or messages[-1].get("content") != clean_prompt:
        messages.append({"role": "user", "content": clean_prompt})

    return messages


def generate_response(
    user_id: str,
    user_message: str,
    channel_id: Optional[str] = None,
    conversation_history: Optional[List[Dict[str, str]]] = None,
    timeout: int = 35
) -> str:
    """Generates an intelligent AI response with multi-turn conversation memory."""
    if not AI_API_KEY or AI_PROVIDER == "mock":
        return get_offline_fallback(user_id)

    system_prompt = build_system_instructions()
    messages = build_messages_payload(system_prompt, user_message, conversation_history)

    try:
        if AI_PROVIDER == "gemini":
            return _call_gemini(messages, system_prompt, timeout)
        elif AI_PROVIDER in ("openai", "openrouter", "custom"):
            return _call_openai_compatible(messages, timeout)
        elif AI_PROVIDER == "anthropic":
            return _call_anthropic(messages, system_prompt, timeout)
        else:
            return get_offline_fallback(user_id)
    except Exception as e:
        logger.error(f"Error calling AI provider '{AI_PROVIDER}': {e}. Returning safe fallback.")
        return get_offline_fallback(user_id)


def _call_gemini(messages: List[Dict[str, str]], system_prompt: str, timeout: int) -> str:
    model = AI_MODEL or "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={AI_API_KEY}"

    contents = []
    for m in messages:
        if m["role"] == "system":
            continue
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.7}
    }
    response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout)
    if response.status_code == 200:
        candidates = response.json().get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                text = parts[0].get("text") or ""
                cleaned = clean_ai_output(text)
                if cleaned:
                    return cleaned
        return "I'm right here! Feel free to ask a question, share an idea, or check your rank with `/level`! 🚀"
    raise RuntimeError(f"Gemini API status {response.status_code}")


def _call_openai_compatible(messages: List[Dict[str, str]], timeout: int = 35) -> str:
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
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.7
    }

    # Pass reasoning_effort="low" for reasoning models like Grok 4.7 to minimize token cost and latency
    reasoning_effort = os.getenv("AI_REASONING_EFFORT", "low").strip().lower()
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    last_err = None
    for attempt in range(2):
        try:
            response = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
            if response.status_code == 200:
                choices = response.json().get("choices", [])
                if choices:
                    msg = choices[0].get("message", {})
                    # CRUCIAL: Only extract actual user-facing content. Never leak raw internal reasoning!
                    raw_content = msg.get("content") or ""
                    cleaned = clean_ai_output(raw_content)
                    if cleaned:
                        return cleaned

                return "I'm right here! Feel free to ask a question, share an idea, or check your rank with `/level`! 🚀"
            raise RuntimeError(f"OpenAI API status {response.status_code}: {response.text}")
        except requests.exceptions.Timeout as e:
            last_err = e
            logger.warning(f"AI API timed out (attempt {attempt+1}/2). Retrying...")
            continue
        except Exception as e:
            last_err = e
            break

    if last_err:
        raise last_err
    return "I'm right here! Feel free to ask a question, share an idea, or check your rank with `/level`! 🚀"


def _call_anthropic(messages: List[Dict[str, str]], system_prompt: str, timeout: int) -> str:
    model = AI_MODEL or "claude-3-5-haiku-20241022"
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": AI_API_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}

    anthropic_messages = []
    for m in messages:
        if m["role"] == "system":
            continue
        anthropic_messages.append({"role": m["role"], "content": m["content"]})

    payload = {
        "model": model,
        "system": system_prompt,
        "messages": anthropic_messages,
        "max_tokens": 2048
    }
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if response.status_code == 200:
        content = response.json().get("content", [])
        if content and content[0].get("type") == "text":
            text = content[0].get("text") or ""
            cleaned = clean_ai_output(text)
            if cleaned:
                return cleaned
        return "I'm right here! Keep up the great discussion! 🚀"
    raise RuntimeError(f"Anthropic API status {response.status_code}")
