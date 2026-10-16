"""LLM provider client (OpenAI-compatible chat completions, stdlib only). OWNER: agent D (voice).

Environment: LLM_BASE_URL (default https://api.openai.com/v1), LLM_API_KEY, LLM_MODEL, LLM_MODEL_ALLOWLIST (comma list).
The key goes in the Authorization header only. Never log the key, prompts or replies.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_BASE_URL = "https://api.openai.com/v1"
TEMPERATURE = 0.8

PostFn = Callable[[str, Dict[str, Any], Dict[str, str], float], Tuple[int, Dict[str, Any]]]


class ProviderError(Exception):
    """Carries a short reason code like 'http_429', 'timeout', 'bad_json', 'model_not_allowed'."""


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


class Provider:
    def __init__(self, base_url: str, api_key: str, model: str, allowlist: List[str],
                 http_post: Optional[PostFn] = None, json_mode: bool = False):
        """Raises ProviderError('model_not_allowed') when model is not in a non-empty allowlist, and
        ProviderError('missing_key') when api_key is empty."""
        if not api_key:
            raise ProviderError("missing_key")
        allowed = [m.strip() for m in (allowlist or []) if m and m.strip()]
        if not allowed or model not in allowed:
            raise ProviderError("model_not_allowed")
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._api_key = api_key
        self.model = model
        self.allowlist = allowed
        self.json_mode = bool(json_mode)
        self._post: PostFn = http_post or _default_post

    @classmethod
    def from_env(cls, env: Dict[str, str]) -> "Provider":
        allowlist = [m.strip() for m in (env.get("LLM_MODEL_ALLOWLIST") or "").split(",") if m.strip()]
        return cls(
            base_url=env.get("LLM_BASE_URL") or DEFAULT_BASE_URL,
            api_key=env.get("LLM_API_KEY") or "",
            model=env.get("LLM_MODEL") or "",
            allowlist=allowlist,
        )

    def __repr__(self) -> str:  # never show the key
        return "Provider(model=%r)" % self.model

    def complete(self, messages: List[Dict[str, str]], max_tokens: int, timeout: float = 4.0) -> Tuple[str, int, int]:
        """POST {base}/chat/completions with temperature 0.8, no streaming, no retries. Returns (text, tokens_in,
        tokens_out). Raises ProviderError on any problem."""
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": TEMPERATURE,
            "stream": False,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self._api_key,
        }
        url = self.base_url + "/chat/completions"
        try:
            status, data = self._post(url, body, headers, float(timeout))
        except ProviderError:
            raise
        except Exception as exc:  # reason only, never the message text (it may contain request details)
            raise ProviderError("timeout" if _is_timeout(exc) else "network")
        if status != 200:
            raise ProviderError("http_%d" % int(status))
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("bad_json")
        if not isinstance(text, str):
            raise ProviderError("bad_json")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        tokens_in = usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else 0
        tokens_out = usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else 0
        return text, tokens_in, tokens_out
