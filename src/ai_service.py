"""
==============================================================================
AI REASONING SERVICE MODULE (src/ai_service.py)
==============================================================================
Provides concise, single-message multi-provider replies with bounded response
reads, code-safe Markdown-to-Slack formatting, and shortcode normalization.
==============================================================================
"""

import json
import logging
import os
import re
import time
import zlib
from typing import Any, Dict, List, Optional, Tuple
import requests

from config import (
    AI_API_KEY,
    AI_BASE_URL,
    AI_MAX_OUTPUT_TOKENS,
    AI_MAX_REPLY_CHARS,
    AI_MODEL,
    AI_PROVIDER,
    AI_TIMEOUT_SECONDS,
    CONFIG,
    SOUL_PATH,
)

logger = logging.getLogger("SlackBotAI")

SLACK_EMOJI_CODES = {
    "🎉": ":sparkles:",
    "⭐": ":sparkles:",
    "🚀": ":rocket:",
    "💡": ":sparkles:",
    "🔥": ":rocket:",
    "✨": ":sparkles:",
    "👏": ":sparkles:",
    "🏆": ":sparkles:",
    "🌱": ":sparkles:",
    "🌿": ":sparkles:",
    "🦆": ":duck:",
    "👀": ":eyes:",
    "🍞": ":duck:",
    "👋": ":duck:",
    "🤔": ":eyes:",
    "⚡": ":rocket:",
    "❤️": ":sparkles:",
    "✅": ":sparkles:",
    "⚠️": ":eyes:",
}
_UNCONVERTED_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\u2300-\u23FF\u2600-\u27BF\uFE0E\uFE0F\u200D]"
)
_SLACK_SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:", re.IGNORECASE)
_CODE_SPAN_RE = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)
MAX_AI_RESPONSE_BYTES = 1024 * 1024

_SOUL_CACHE: Optional[Tuple[str, float, str]] = None


def _limit_reply(text: str, max_chars: Optional[int] = None) -> str:
    if max_chars is None:
        max_chars = AI_MAX_REPLY_CHARS
    if len(text) <= max_chars:
        return text
    clipped = text[: max_chars - 1]
    boundary = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
    if boundary >= max_chars // 2:
        return clipped[: boundary + 1].rstrip()
    return clipped.rsplit(" ", 1)[0].rstrip() + "…"


def _post_ai_json(
    endpoint: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout: float,
) -> Dict[str, Any]:
    """Make one request and bound the whole JSON-body read, including slow trickle responses."""
    deadline = time.monotonic() + timeout
    request_headers = dict(headers)
    request_headers["Accept-Encoding"] = "identity"
    response = requests.post(
        endpoint,
        json=payload,
        headers=request_headers,
        timeout=timeout,
        stream=True,
    )
    try:
        if response.status_code != 200:
            raise RuntimeError(f"AI API status {response.status_code}")
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
        parsed = json.loads(body.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    finally:
        response.close()


def _format_markdown_outside_code(segment: str) -> str:
    # Convert Markdown links [Label](URL) to Slack links <URL|Label>
    segment = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"<\2|\1>", segment)
    # Convert Markdown headers (### Header) to Slack bold (*Header*)
    segment = re.sub(r"^[ \t]*#{1,6}[ \t]+([^\r\n]+)$", r"*\1*", segment, flags=re.MULTILINE)
    # Convert Markdown bold-italic ***text*** to *_text_* and bold **text** to *text*
    segment = re.sub(r"\*\*\*([^*\n]+)\*\*\*", r"*_\1_*", segment)
    segment = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", segment)
    return segment


def clean_ai_output(text: str) -> str:
    """
    Cleans AI output thoroughly without corrupting code blocks:
    1. Strips internal chain-of-thought scratchpads and <think> tags.
    2. Converts Markdown links, headers, and bold outside code spans to Slack mrkdwn.
    3. Normalizes emojis to approved Slack shortcodes and enforces length limits.
    """
    if not text:
        return ""

    # 1. Strip completed <think>...</think> blocks (case-insensitive)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    if re.search(r"</think>", text, flags=re.IGNORECASE):
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]

    # 2. Strip unclosed <think>... if cut off while thinking
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL | re.IGNORECASE)

    text = text.strip()

    # 3. Strip leading and trailing internal scratchpad notes
    text = re.sub(
        r"^(?:Thinking Process|Thought|Reasoning|Internal):\s*.*?\n\n",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"\n+(?:Thinking Process|Thought|Reasoning|Internal):.*$",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 4. Format Markdown outside of inline/fenced code spans
    parts = _CODE_SPAN_RE.split(text)
    formatted_parts = [
        part if idx % 2 == 1 else _format_markdown_outside_code(part)
        for idx, part in enumerate(parts)
    ]
    text = "".join(formatted_parts).strip()

    # 5. Normalize Unicode emojis to Slack shortcodes
    for symbol, shortcode in SLACK_EMOJI_CODES.items():
        text = text.replace(symbol, shortcode)
    text = _UNCONVERTED_EMOJI_RE.sub("", text)

    emoji_cfg = CONFIG.get("emoji_settings") or {}
    allowed = {
        str(code).lower()
        for code in emoji_cfg.get("allowed_emojis", [":duck:", ":eyes:", ":sparkles:", ":rocket:"])
        if _SLACK_SHORTCODE_RE.fullmatch(str(code))
    }
    freq = str(emoji_cfg.get("frequency", "low")).lower()
    if freq == "none":
        emoji_limit = 0
    elif freq == "moderate":
        emoji_limit = 2
    elif freq == "high":
        emoji_limit = 4
    else:
        emoji_limit = 1

    used = 0

    def keep_allowed_shortcode(match: re.Match) -> str:
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
    global _SOUL_CACHE
    fallback = (
        "You are Goose Lee, a practical community duck and developer peer. Give short, direct answers with dry wit. "
        "Avoid canned hype, catchphrases, and emoji. If asked to choose between Jackie Chan and Bruce Lee, pick Bruce Lee as a joke."
    )
    if SOUL_PATH.exists():
        try:
            path_str = str(SOUL_PATH)
            mtime = SOUL_PATH.stat().st_mtime if hasattr(SOUL_PATH, "stat") else 0.0
            if _SOUL_CACHE and _SOUL_CACHE[0] == path_str and _SOUL_CACHE[1] == mtime:
                return _SOUL_CACHE[2]
            with open(SOUL_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                _SOUL_CACHE = (path_str, mtime, content)
                return content
        except Exception as e:
            logger.warning("Could not read soul.md: %s. Using fallback persona.", e)

    return fallback


def build_system_instructions() -> str:
    base_soul = load_soul_prompt()
    emoji_cfg = CONFIG.get("emoji_settings") or {}
    frequency = str(emoji_cfg.get("frequency", "low")).lower()
    allowed_emojis = emoji_cfg.get("allowed_emojis") or [":duck:", ":eyes:", ":sparkles:", ":rocket:"]
    if frequency == "none" or not allowed_emojis:
        emoji_rule = "- Do not use emoji."
    else:
        emoji_rule = (
            "- Most replies use none; at most one Slack shortcode from this list: "
            f"{' '.join(str(e) for e in allowed_emojis)}"
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
    templates = CONFIG.get("templates") or {}
    fallback = templates.get(
        "ai_offline_fallback",
        "Hi <@{user_id}>. I can't reach my answer service right now. Try again shortly.",
    )
    try:
        return fallback.format(user_id=user_id)
    except Exception:
        return f"Hi <@{user_id}>. I can't reach my answer service right now. Try again shortly."


def build_messages_payload(
    system_prompt: str,
    user_message: str,
    context_messages: Optional[List[Dict[str, str]]] = None,
    conversation_history: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    """Build from the direct mention and context selected for that channel or thread."""
    messages = [{"role": "system", "content": system_prompt}]
    history = context_messages if context_messages is not None else conversation_history
    clean_prompt = (user_message or "").strip() or "Hello!"

    if history:
        valid_turns = []
        for turn in history:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                valid_turns.append({"role": role, "content": content})
        if valid_turns and valid_turns[-1]["role"] == "user" and valid_turns[-1]["content"] == clean_prompt:
            valid_turns.pop()
        messages.extend(valid_turns)

    messages.append({"role": "user", "content": clean_prompt})
    return messages


def _normalize_alternating_turns(
    messages: List[Dict[str, str]],
    assistant_role: str = "assistant",
) -> List[Dict[str, str]]:
    """Ensures messages start with 'user' and strictly alternate roles by merging adjacent same-role turns."""
    normalized: List[Dict[str, str]] = []
    for msg in messages:
        if msg.get("role") == "system":
            continue
        role = "user" if msg.get("role") == "user" else assistant_role
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if not normalized and role != "user":
            continue
        if normalized and normalized[-1]["role"] == role:
            normalized[-1]["content"] = f"{normalized[-1]['content']}\n\n{content}"
        else:
            normalized.append({"role": role, "content": content})
    return normalized


def generate_response(
    user_id: str,
    user_message: str,
    channel_id: Optional[str] = None,
    context_messages: Optional[List[Dict[str, str]]] = None,
    conversation_history: Optional[List[Dict[str, str]]] = None,
    timeout: Optional[float] = None,
) -> str:
    """Generate a reply from the direct mention and messages in its own thread or channel."""
    if not AI_API_KEY or AI_PROVIDER == "mock":
        return get_offline_fallback(user_id)

    try:
        system_prompt = build_system_instructions()
        messages = build_messages_payload(
            system_prompt,
            user_message,
            context_messages=context_messages,
            conversation_history=conversation_history,
        )
        request_timeout = AI_TIMEOUT_SECONDS if timeout is None else timeout

        if AI_PROVIDER == "gemini":
            return _call_gemini(messages, system_prompt, request_timeout)
        if AI_PROVIDER in ("openai", "openrouter", "custom", "deepseek"):
            return _call_openai_compatible(messages, request_timeout)
        if AI_PROVIDER == "anthropic":
            return _call_anthropic(messages, system_prompt, request_timeout)
        return get_offline_fallback(user_id)
    except Exception as e:
        logger.error("AI provider request failed (%s). Returning safe fallback.", type(e).__name__)
        return get_offline_fallback(user_id)


def _call_gemini(messages: List[Dict[str, str]], system_prompt: str, timeout: float) -> str:
    model = AI_MODEL or "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": AI_API_KEY,
    }

    turns = _normalize_alternating_turns(messages, assistant_role="model")
    contents = [
        {"role": turn["role"], "parts": [{"text": turn["content"]}]}
        for turn in turns
    ]

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": AI_MAX_OUTPUT_TOKENS, "temperature": 0.7},
    }
    data = _post_ai_json(url, payload, headers, timeout)
    candidates = data.get("candidates") or []
    if candidates:
        content_obj = (candidates[0] or {}).get("content") or {}
        parts = content_obj.get("parts") or []
        text_parts = [
            str(p.get("text") or "")
            for p in parts
            if isinstance(p, dict) and not p.get("thought") and p.get("text")
        ]
        if not text_parts and parts and isinstance(parts[0], dict):
            text_parts = [str(parts[0].get("text") or "")]
        cleaned = clean_ai_output("\n".join(text_parts))
        if cleaned:
            return cleaned
    return "I could not get a useful answer. Try asking another way."


def _supports_reasoning_effort(provider: str, model: str) -> bool:
    model_lower = (model or "").lower()
    if provider == "custom":
        return True
    return any(
        token in model_lower
        for token in ("grok", "o1", "o3", "o4", "reasoner", "r1")
    )


def _call_openai_compatible(messages: List[Dict[str, str]], timeout: Optional[float] = None) -> str:
    model = AI_MODEL
    base_url = AI_BASE_URL
    if AI_PROVIDER == "openrouter":
        base_url = base_url or "https://openrouter.ai/api/v1"
        model = model or "meta-llama/llama-3-8b-instruct:free"
    elif AI_PROVIDER == "deepseek":
        base_url = base_url or "https://api.deepseek.com/v1"
        model = model or "deepseek-chat"
    else:
        base_url = base_url or "https://api.openai.com/v1"
        model = model or "gpt-4o-mini"

    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": AI_MAX_OUTPUT_TOKENS,
        "temperature": 0.7,
    }
    reasoning_effort = os.getenv("AI_REASONING_EFFORT", "low").strip().lower()
    if reasoning_effort and _supports_reasoning_effort(AI_PROVIDER, model):
        payload["reasoning_effort"] = reasoning_effort

    data = _post_ai_json(
        endpoint,
        payload,
        headers,
        AI_TIMEOUT_SECONDS if timeout is None else timeout,
    )
    choices = data.get("choices") or []
    if choices:
        message = (choices[0] or {}).get("message") or {}
        cleaned = clean_ai_output(str(message.get("content") or ""))
        if cleaned:
            return cleaned
    return "I could not get a useful answer. Try asking another way."


def _call_anthropic(messages: List[Dict[str, str]], system_prompt: str, timeout: float) -> str:
    model = AI_MODEL or "claude-3-5-haiku-20241022"
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": AI_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    turns = _normalize_alternating_turns(messages, assistant_role="assistant")
    payload = {
        "model": model,
        "system": system_prompt,
        "messages": turns,
        "max_tokens": AI_MAX_OUTPUT_TOKENS,
    }
    data = _post_ai_json(url, payload, headers, timeout)
    content = data.get("content") or []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            cleaned = clean_ai_output(str(block.get("text") or ""))
            if cleaned:
                return cleaned
    return "I could not get a useful answer. Try asking another way."
