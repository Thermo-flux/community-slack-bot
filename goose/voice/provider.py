"""LLM provider client. Works with any provider that speaks one of two wire formats. Stdlib only.

  openai     POST {base}/chat/completions. Used by OpenAI and by most other providers and gateways (OpenRouter, Gemini's
             OpenAI-compatible endpoint, DeepSeek, Mistral, Groq, Together, local servers). Most of them cache a stable
             prompt prefix on their own, so nothing extra is sent. Cached tokens are read from the usage block when reported.
  anthropic  POST {base}/messages. Used by Anthropic. The static prompt block gets an explicit cache marker, because
             Anthropic only caches what is marked.

Environment:
  LLM_API_FORMAT        openai (default) or anthropic
  LLM_BASE_URL          optional; defaults to the format's usual base URL
  LLM_API_KEY           the key, sent in a header only
  LLM_MODEL             the model id; must appear in LLM_MODEL_ALLOWLIST
  LLM_MODEL_ALLOWLIST   comma-separated model ids allowed to run
  LLM_CACHE             on (default) or off. Caching never changes the answer, only the billed input cost.

Never log the key, prompts or replies.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

FORMATS = ("openai", "anthropic")
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}
DEFAULT_BASE_URL = DEFAULT_BASE_URLS["openai"]
ANTHROPIC_VERSION = "2023-06-01"
TEMPERATURE = 0.8

PostFn = Callable[[str, Dict[str, Any], Dict[str, str], float], Tuple[int, Dict[str, Any]]]


class ProviderError(Exception):
    """Carries a short reason code like 'http_429', 'timeout', 'bad_json', 'model_not_allowed'."""


@dataclass(frozen=True)
class Usage:
    """One completed call. tokens_in is the total input (cached or not); cached_in is the part served from cache."""
    text: str
    tokens_in: int
    tokens_out: int
    cached_in: int = 0


def _default_post(url: str, body: Dict[str, Any], headers: Dict[str, str], timeout: float) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = int(e.code)
        try:
            raw = e.read()
        except Exception:
            raw = b""
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (socket.timeout, TimeoutError))


LOCAL_HOSTS = ("localhost", "127.0.0.1")


def _secure_url(url: str) -> bool:
    """https always passes. http passes only for a local host (tests and local models)."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(host)
    return parts.scheme == "http" and host in LOCAL_HOSTS


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class Provider:
    def __init__(self, base_url: str, api_key: str, model: str, allowlist: List[str],
                 http_post: Optional[PostFn] = None, json_mode: bool = False,
                 api_format: str = "openai", cache: bool = True):
        """Raises ProviderError('missing_key') without a key, 'model_not_allowed' when the model is not in a non-empty
        allowlist, 'bad_format' for an unknown format, and 'insecure_url' when base_url is not https (plain http is
        accepted only for localhost and 127.0.0.1)."""
        if not api_key:
            raise ProviderError("missing_key")
        if api_format not in FORMATS:
            raise ProviderError("bad_format")
        allowed = [m.strip() for m in (allowlist or []) if m and m.strip()]
        if not allowed or model not in allowed:
            raise ProviderError("model_not_allowed")
        base = (base_url or DEFAULT_BASE_URLS[api_format]).rstrip("/")
        if not _secure_url(base):
            raise ProviderError("insecure_url")
        self.base_url = base
        self.api_format = api_format
        self.cache = bool(cache)
        self._api_key = api_key
        self.model = model
        self.allowlist = allowed
        self.json_mode = bool(json_mode)
        self._post: PostFn = http_post or _default_post

    @classmethod
    def from_env(cls, env: Dict[str, str]) -> "Provider":
        api_format = (env.get("LLM_API_FORMAT") or "openai").strip().lower()
        allowlist = [m.strip() for m in (env.get("LLM_MODEL_ALLOWLIST") or "").split(",") if m.strip()]
        cache = (env.get("LLM_CACHE") or "on").strip().lower() != "off"
        return cls(
            base_url=env.get("LLM_BASE_URL") or "",
            api_key=env.get("LLM_API_KEY") or "",
            model=env.get("LLM_MODEL") or "",
            allowlist=allowlist,
            api_format=api_format,
            cache=cache,
        )

    def __repr__(self) -> str:  # never show the key
        return "Provider(model=%r, format=%r)" % (self.model, self.api_format)

    # ------------------------------------------------------------------ calls

    def complete(self, messages: List[Dict[str, str]], max_tokens: int, timeout: float = 4.0,
                 parts: Optional[Dict[str, str]] = None) -> Tuple[str, int, int]:
        """Returns (text, tokens_in, tokens_out). See complete_usage for the cached count."""
        usage = self.complete_usage(messages, max_tokens, timeout=timeout, parts=parts)
        return usage.text, usage.tokens_in, usage.tokens_out

    def complete_usage(self, messages: List[Dict[str, str]], max_tokens: int, timeout: float = 4.0,
                       parts: Optional[Dict[str, str]] = None) -> Usage:
        """One POST, no streaming, no retries. `parts` (static, dynamic, user) lets the anthropic format mark the
        static block for caching; without it the system message is sent as one block. Raises ProviderError."""
        if self.api_format == "anthropic":
            url, headers, body = self._anthropic_request(messages, max_tokens, parts)
            parse = self._parse_anthropic
        else:
            url, headers, body = self._openai_request(messages, max_tokens)
            parse = self._parse_openai
        try:
            status, data = self._post(url, body, headers, float(timeout))
        except ProviderError:
            raise
        except Exception as exc:  # reason only, never the message text (it may contain request details)
            raise ProviderError("timeout" if _is_timeout(exc) else "network")
        if status != 200:
            raise ProviderError("http_%d" % int(status))
        return parse(data)

    def _openai_request(self, messages: List[Dict[str, str]], max_tokens: int) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": TEMPERATURE,
            "stream": False,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self._api_key}
        return self.base_url + "/chat/completions", headers, body

    def _anthropic_request(self, messages: List[Dict[str, str]], max_tokens: int,
                           parts: Optional[Dict[str, str]]) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        if parts is not None:
            blocks: List[Dict[str, Any]] = [{"type": "text", "text": parts["static"]}]
            if self.cache:
                blocks[0]["cache_control"] = {"type": "ephemeral"}
            if parts.get("dynamic"):
                blocks.append({"type": "text", "text": parts["dynamic"]})
        else:
            blocks = [{"type": "text", "text": "\n".join(m["content"] for m in messages if m["role"] == "system")}]
        turns = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] in ("user", "assistant")]
        body: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(max_tokens),
            "temperature": TEMPERATURE,
            "system": blocks,
            "messages": turns,
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        return self.base_url + "/messages", headers, body

    @staticmethod
    def _parse_openai(data: Dict[str, Any]) -> Usage:
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("bad_json")
        if not isinstance(text, str):
            raise ProviderError("bad_json")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        return Usage(text=text, tokens_in=_int(usage.get("prompt_tokens")),
                     tokens_out=_int(usage.get("completion_tokens")), cached_in=_int(details.get("cached_tokens")))

    @staticmethod
    def _parse_anthropic(data: Dict[str, Any]) -> Usage:
        content = data.get("content")
        if not isinstance(content, list):
            raise ProviderError("bad_json")
        text = next((b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"), None)
        if not isinstance(text, str):
            raise ProviderError("bad_json")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cache_read = _int(usage.get("cache_read_input_tokens"))
        cache_write = _int(usage.get("cache_creation_input_tokens"))
        # Anthropic reports uncached input separately from cache reads and writes; the total is their sum.
        total_in = _int(usage.get("input_tokens")) + cache_read + cache_write
        return Usage(text=text, tokens_in=total_in, tokens_out=_int(usage.get("output_tokens")), cached_in=cache_read)
