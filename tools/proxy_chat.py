"""Proxy /chat/completions (httpx) + .env. Parses OpenAI-style or Anthropic-style JSON bodies."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class ProxyConfig:
    api_key: str
    base_url: str
    model: str
    timeout_s: float = 90.0


def load_proxy_config(
    *,
    default_base_url: str,
    default_model: str,
    default_timeout_s: float = 90.0,
) -> ProxyConfig:
    load_env_file(Path(".env"))
    api_key = (
        os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("LSP_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    if not api_key:
        raise RuntimeError("Missing API key (e.g. ANTHROPIC_API_KEY in .env)")
    base_url = (
        os.getenv("ANTHROPIC_BASE_URL")
        or os.getenv("LSP_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or default_base_url
    ).rstrip("/")
    model = os.getenv("LLM_MODEL", default_model)
    return ProxyConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_s=default_timeout_s,
    )


def _chat_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def _assistant_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return ""


def _assistant_text_from_body(body: dict[str, Any]) -> str:
    # OpenAI chat.completions: choices[0].message.content
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            msg = first.get("message")
            if isinstance(msg, dict):
                t = _assistant_text(msg.get("content"))
                if t.strip():
                    return t

    # Anthropic message shape (some proxies return this from /chat/completions)
    if body.get("type") == "message" and body.get("role") == "assistant":
        t = _assistant_text(body.get("content"))
        if t.strip():
            return t

    # Generic fallback: top-level content blocks
    if body.get("content") is not None and not choices:
        t = _assistant_text(body.get("content"))
        if t.strip():
            return t

    return ""


def complete_chat(
    cfg: ProxyConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.0,
    max_tokens: int = 4096,
) -> str:
    # Some proxies front Anthropic's Messages API, which rejects role="system"
    # inside `messages` and wants a top-level `system` string instead. Pull
    # any leading system messages out into that top-level field.
    sys_parts: list[str] = []
    chat_messages: list[dict[str, str]] = []
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content") or ""
            if isinstance(content, str):
                sys_parts.append(content)
        else:
            chat_messages.append(m)

    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": chat_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if sys_parts:
        payload["system"] = "\n\n".join(sys_parts)
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=cfg.timeout_s) as client:
        r = client.post(_chat_url(cfg.base_url), headers=headers, json=payload)
    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        try:
            body: Any = r.json()
        except Exception:
            body = r.text
    else:
        body = r.text

    status = r.status_code
    if status >= 400:
        preview = (
            json.dumps(body, ensure_ascii=False)[:3000]
            if isinstance(body, (dict, list))
            else repr(body)
        )
        raise RuntimeError(f"LLM HTTP {status}: {preview}")
    if not isinstance(body, dict):
        raise RuntimeError(f"LLM non-JSON body: HTTP {status} {repr(body)[:4000]}")

    text = _assistant_text_from_body(body)
    if text.strip():
        return text
    err = body.get("error")
    preview = json.dumps(body, ensure_ascii=False)[:3000]
    raise RuntimeError(
        "LLM returned no assistant text. "
        f"HTTP {status}. error={err!r} body_preview={preview!r}"
    )


def iter_completion_stream(
    cfg: ProxyConfig,
    messages: list[dict[str, str]],
) -> Iterator[str]:
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=cfg.timeout_s) as client:
        with client.stream(
            "POST", _chat_url(cfg.base_url), headers=headers, json=payload
        ) as r:
            if r.status_code >= 400:
                text = r.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Stream failed HTTP {r.status_code}: {text[:4000]!r}"
                )
            for line in r.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                choices = obj.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                first = choices[0]
                if not isinstance(first, dict):
                    continue
                delta = first.get("delta")
                if isinstance(delta, dict):
                    yield _assistant_text(delta.get("content"))
