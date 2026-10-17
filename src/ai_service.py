"""
==============================================================================
AI REASONING SERVICE MODULE (src/ai_service.py)
==============================================================================
Provides concise, single-message multi-provider replies with bounded response reads
and Slack shortcode normalization.
==============================================================================
"""

import os
import re
import json
import logging
import time
import zlib
import requests
from typing import Optional, List, Dict, Any
from config import (
    AI_PROVIDER,
    AI_API_KEY,
    AI_MODEL,
    AI_BASE_URL,
    AI_TIMEOUT_SECONDS,
    AI_MAX_OUTPUT_TOKENS,
    AI_MAX_REPLY_CHARS,
    SOUL_PATH,
    CONFIG
)

logger = logging.getLogger("SlackBotAI")

SLACK_EMOJI_CODES = {
    "🎉": ":sparkles:", "⭐": ":sparkles:", "🚀": ":rocket:", "💡": ":sparkles:", "🔥": ":rocket:",
    "✨": ":sparkles:", "👏": ":sparkles:", "🏆": ":sparkles:", "🌱": ":sparkles:", "🌿": ":sparkles:",
    "🦆": ":duck:", "👀": ":eyes:", "🍞": ":duck:", "👋": ":duck:", "🤔": ":eyes:",
    "⚡": ":rocket:", "❤️": ":sparkles:", "✅": ":sparkles:", "⚠️": ":eyes:",
}
_UNCONVERTED_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2300-\u23FF\u2600-\u27BF\uFE0E\uFE0F\u200D]")
_SLACK_SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:", re.IGNORECASE)
MAX_AI_RESPONSE_BYTES = 1024 * 1024


def _limit_reply(text: str, max_chars: Optional[int] = None) -> str:
    if max_chars is None:
        max_chars = AI_MAX_REPLY_CHARS
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars - 1]
    boundary = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
    if boundary >= max_chars // 2:
        return clipped[:boundary + 1].rstrip()
    return clipped.rsplit(" ", 1)[0].rstrip() + "…"


def _post_ai_json(endpoint: str, payload: Dict[str, Any], headers: Dict[str, str],
                  timeout: float) -> Dict[str, Any]:
    """Make one request and bound the whole JSON-body read, including slow trickle responses."""
    deadline = time.monotonic() + timeout
    request_headers = dict(headers)
    request_headers["Accept-Encoding"] = "identity"
    response = requests.post(endpoint, json=payload, headers=request_headers, timeout=timeout, stream=True)
    try:
        if response.status_code != 200:
            raise RuntimeError("AI API status %s" % response.status_code)
        raw = getattr(response, "raw", None)
        reader = getattr(raw, "read", None)
        if not callable(reader):
            raise RuntimeError("AI response body is not stream-readable")

        encoding = str(response.headers.get("Content-Encoding") or "identity").lower()
        if encoding not in ("", "identity", "gzip", "deflate"):
            raise RuntimeError("Unsupported compressed AI response")
        decoder = None
        if encoding == "gzip":
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            decoder = zlib.decompressobj()
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_AI_RESPONSE_BYTES:
            raise RuntimeError("AI response too large")
        socket_obj = getattr(
            getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None),
            "_sock",
            None,
        )
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.exceptions.Timeout("AI response deadline exceeded")
            if socket_obj is not None:
                socket_obj.settimeout(remaining)
            try:
                chunk = reader(4096, decode_content=False)
            except TypeError:
                chunk = reader(4096)
            if not chunk:
                break
            if decoder is None:
                decoded = chunk
            else:
                decoded = decoder.decompress(chunk, MAX_AI_RESPONSE_BYTES + 1 - len(body))
                if decoder.unconsumed_tail:
                    raise RuntimeError("AI response too large")
            body.extend(decoded)
            if len(body) > MAX_AI_RESPONSE_BYTES:
                raise RuntimeError("AI response too large")
            if time.monotonic() > deadline:
                raise requests.exceptions.Timeout("AI response deadline exceeded")
        if decoder is not None:
            body.extend(decoder.flush())
        if len(body) > MAX_AI_RESPONSE_BYTES:
            raise RuntimeError("AI response too large")
        return json.loads(body.decode("utf-8"))
    finally:
        response.close()


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

    text = text.strip()
    for symbol, shortcode in SLACK_EMOJI_CODES.items():
        text = text.replace(symbol, shortcode)
    text = _UNCONVERTED_EMOJI_RE.sub("", text)
    emoji_cfg = CONFIG.get("emoji_settings", {})
    allowed = {
        str(code).lower()
        for code in emoji_cfg.get("allowed_emojis", [":duck:", ":eyes:", ":sparkles:", ":rocket:"])
        if _SLACK_SHORTCODE_RE.fullmatch(str(code))
    }
    emoji_limit = 0 if str(emoji_cfg.get("frequency", "low")).lower() == "none" else 1
    used = 0

    def keep_allowed_shortcode(match):
        nonlocal used
        shortcode = match.group(0).lower()
        if shortcode not in allowed or used >= emoji_limit:
            return ""
        used += 1
        return shortcode

    text = _SLACK_SHORTCODE_RE.sub(keep_allowed_shortcode, text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([,.;!?])", r"\1", text)
    return _limit_reply(text.strip())


def load_soul_prompt() -> str:
    if SOUL_PATH.exists():
        try:
            with open(SOUL_PATH, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            logger.warning(f"Could not read soul.md: {e}. Using fallback persona.")

    return (
        "You are Goose Lee, a practical duck and developer peer. Give short, direct answers with dry wit. "
        "Skip forced hype, catchphrases, and emoji. If asked to choose between Jackie Chan and Bruce Lee, pick Bruce Lee as a joke."
    )


def build_system_instructions() -> str:
    base_soul = load_soul_prompt()
    emoji_cfg = CONFIG.get("emoji_settings", {})
    frequency = str(emoji_cfg.get("frequency", "low")).lower()
    allowed_emojis = emoji_cfg.get("allowed_emojis", [":duck:", ":eyes:", ":sparkles:", ":rocket:"])
    if frequency == "none" or not allowed_emojis:
        emoji_rule = "- Do not use emoji."
    else:
        emoji_rule = (
            "- Most replies use none; at most one Slack shortcode from this list: "
            f"{' '.join(allowed_emojis)}"
        )

    emoji_instruction = (
        f"\n\nEMOJI RULES:\n"
        f"{emoji_rule}\n"
        f"- Keep responses direct and conversational, at most 2 short sentences and {AI_MAX_REPLY_CHARS} characters.\n"
        f"- Do not add forced hype, catchphrases, or a joke to every reply.\n"
        f"- Never output internal reasoning, scratchpad, or <think> tags."
    )

    privacy_and_persona_rules = (
        "\n\nCONTEXT AND PRIVACY:\n"
        "- Answer direct mentions, direct messages, clear Goose name calls, and follow-ups in a thread you opened.\n"
        "- For other untagged channel messages, answer only an explicit opinion question that names Goose, Goose Lee, or Orry. Stay silent on ordinary messages.\n"
        "- Use supplied context from the same channel or the mentioned thread only to understand the current request.\n"
        "- Do not bring up unrelated messages or answer an older question unless the current mention refers to it or asks your opinion.\n"
        "- If needed context is missing, ask the user to include it in a message that mentions you.\n"
        "- If asked who is better, Jackie Chan or Bruce Lee, pick Bruce Lee as your brief opinion. If asked whether you or Orry is better, pick yourself playfully without insulting Orry.\n"
        "- For account-security questions, distinguish evidence from guesses; a commit author email alone does not prove account access.\n"
        "- Do not invent live facts, member activity, or scores; use local commands for tracked stats."
    )
    return base_soul + privacy_and_persona_rules + emoji_instruction


def get_offline_fallback(user_id: str) -> str:
    templates = CONFIG.get("templates", {})
    fallback = templates.get(
        "ai_offline_fallback",
        "Hi <@{user_id}>. I can't reach my answer service right now. Try again shortly."
    )
    return fallback.format(user_id=user_id)


def build_messages_payload(
    system_prompt: str,
    user_message: str,
    context_messages: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    """Build from the direct mention and context selected for that channel or thread."""
    messages = [{"role": "system", "content": system_prompt}]
    if context_messages:
        for turn in context_messages:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message.strip()})
    return messages


def generate_response(
    user_id: str,
    user_message: str,
    channel_id: Optional[str] = None,
    context_messages: Optional[List[Dict[str, str]]] = None,
    timeout: Optional[float] = None
) -> str:
    """Generate a reply from the direct mention and messages in its own thread, if any."""
    if not AI_API_KEY or AI_PROVIDER == "mock":
        return get_offline_fallback(user_id)

    system_prompt = build_system_instructions()
    messages = build_messages_payload(system_prompt, user_message, context_messages)
    request_timeout = AI_TIMEOUT_SECONDS if timeout is None else timeout

    try:
        if AI_PROVIDER == "gemini":
            return _call_gemini(messages, system_prompt, request_timeout)
        elif AI_PROVIDER in ("openai", "openrouter", "custom"):
            return _call_openai_compatible(messages, request_timeout)
        elif AI_PROVIDER == "anthropic":
            return _call_anthropic(messages, system_prompt, request_timeout)
        else:
            return get_offline_fallback(user_id)
    except Exception as e:
        logger.error("AI provider request failed (%s). Returning safe fallback.", type(e).__name__)
        return get_offline_fallback(user_id)


def _call_gemini(messages: List[Dict[str, str]], system_prompt: str, timeout: float) -> str:
    model = AI_MODEL or "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={AI_API_KEY}"

    contents = []
    for message in messages:
        if message["role"] == "system":
            continue
        role = "user" if message["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": message["content"]}]})

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": AI_MAX_OUTPUT_TOKENS, "temperature": 0.7},
    }
    data = _post_ai_json(url, payload, {"Content-Type": "application/json"}, timeout)
    candidates = data.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        if parts:
            cleaned = clean_ai_output(parts[0].get("text") or "")
            if cleaned:
                return cleaned
    return "I could not get a useful answer. Try asking another way."

def _call_openai_compatible(messages: List[Dict[str, str]], timeout: Optional[float] = None) -> str:
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
        "max_tokens": AI_MAX_OUTPUT_TOKENS,
        "temperature": 0.7,
    }
    reasoning_effort = os.getenv("AI_REASONING_EFFORT", "low").strip().lower()
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    data = _post_ai_json(
        endpoint,
        payload,
        headers,
        AI_TIMEOUT_SECONDS if timeout is None else timeout,
    )
    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        cleaned = clean_ai_output(message.get("content") or "")
        if cleaned:
            return cleaned
    return "I could not get a useful answer. Try asking another way."

def _call_anthropic(messages: List[Dict[str, str]], system_prompt: str, timeout: float) -> str:
    model = AI_MODEL or "claude-3-5-haiku-20241022"
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": AI_API_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    turns = [
        {"role": message["role"], "content": message["content"]}
        for message in messages if message["role"] != "system"
    ]
    payload = {
        "model": model,
        "system": system_prompt,
        "messages": turns,
        "max_tokens": AI_MAX_OUTPUT_TOKENS,
    }
    data = _post_ai_json(url, payload, headers, timeout)
    content = data.get("content", [])
    if content and content[0].get("type") == "text":
        cleaned = clean_ai_output(content[0].get("text") or "")
        if cleaned:
            return cleaned
    return "I could not get a useful answer. Try asking another way."
