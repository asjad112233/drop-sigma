"""
Drop Sigma — Groq-powered Support AI client.

Cheapest path to Claude-quality conversational help: Groq's free tier.
We rotate across multiple Groq-hosted models so each model's 30 RPM limit
stacks (~90 effective RPM) before we fall through to the on-disk keyword
matcher as a final safety net.

Flow:
    Llama 3.3 70B Versatile  ← primary (best quality on free tier)
        ↓ on rate-limit/error
    Llama 3.1 8B Instant     ← fast fallback
        ↓ on rate-limit/error
    Mixtral 8x7B 32k         ← second fallback
        ↓ total Groq failure
    core.support_ai_match    ← offline keyword matcher (always works)

No new dependency: uses `requests` (already installed). Groq exposes an
OpenAI-compatible chat-completions endpoint, so the JSON contract is
straightforward.
"""

from __future__ import annotations
import json as _json
import logging
import re
import time
from typing import Optional

import requests
from django.conf import settings

from core.support_ai_kb import build_system_prompt, DEEP_LINKS
from core.support_ai_match import match_question

log = logging.getLogger(__name__)

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Model rotation order. Each entry has its own 30 RPM free-tier quota,
# so a busy minute can stack ~60 effective RPM before falling through to
# the matcher. Keep this list to currently-supported Groq free-tier
# models — older entries like gemma2-9b-it and mixtral were decommissioned
# and removed from this rotation.
_MODELS = (
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
)

# Short timeout — if Groq is slow, fail fast and try next model / fallback.
_REQUEST_TIMEOUT = 12  # seconds


def _build_messages(question: str, history: list) -> list:
    """Compose chat messages: system prompt + last few turns + current question.

    Only the last 4 turns are forwarded to Groq even if the frontend keeps a
    longer history in localStorage. This keeps input tokens (and thus cost)
    bounded — support questions almost never need deeper context to answer."""
    msgs = [{"role": "system", "content": build_system_prompt()}]
    for turn in (history or [])[-4:]:
        role = turn.get("role", "user")
        if role not in ("user", "assistant"):
            role = "user"
        content = (turn.get("content") or "").strip()[:500]
        if content:
            msgs.append({"role": role, "content": content})
    msgs.append({
        "role": "user",
        "content": question + "\n\nRespond as the assistant in the JSON format described."
    })
    return msgs


def _call_groq(model: str, messages: list) -> Optional[str]:
    """Single Groq call. Returns the assistant text or None on any failure."""
    api_key = settings.GROQ_API_KEY
    if not api_key:
        return None
    try:
        r = requests.post(
            _GROQ_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 800,
                # Ask Groq to enforce JSON output — many of their models honor this.
                "response_format": {"type": "json_object"},
            },
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning("groq_support: request error on %s: %s", model, e)
        return None

    if r.status_code == 429:
        # Visible at WARNING so production logs surface real rate-limit pressure.
        log.warning("groq_support: %s rate-limited, falling through", model)
        return None
    if r.status_code >= 400:
        log.warning("groq_support: %s returned %s: %s", model, r.status_code, r.text[:200])
        return None

    try:
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as e:
        log.warning("groq_support: bad response shape on %s: %s", model, e)
        return None


def _parse_json_payload(raw: str) -> Optional[dict]:
    """Tolerantly extract the JSON object the model emitted."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        out = _json.loads(text)
        if isinstance(out, dict):
            return out
    except _json.JSONDecodeError:
        pass
    # Fallback: regex-grab the first {...} block.
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            out = _json.loads(m.group(0))
            if isinstance(out, dict):
                return out
        except _json.JSONDecodeError:
            return None
    return None


def _normalise_payload(payload: dict) -> dict:
    """Coerce the model output into the strict response shape the frontend renders."""
    reply       = (payload.get("reply") or "").strip()
    steps_raw   = payload.get("steps") or []
    breadcrumb  = payload.get("breadcrumb") or []
    deep_link   = (payload.get("deep_link") or "").strip()

    # Steps can come as list of strings OR list of dicts; normalise to dicts.
    steps = []
    for s in steps_raw:
        if isinstance(s, dict):
            steps.append({
                "action": str(s.get("action") or s.get("text") or "").strip(),
                "hint":   str(s.get("hint") or "").strip(),
                "nav":    str(s.get("nav")  or "").strip(),
            })
        elif isinstance(s, str):
            steps.append({"action": s.strip()})

    # Filter out empty step rows
    steps = [s for s in steps if s.get("action")]

    return {
        "reply":         reply or "Here's what I found:",
        "steps":         steps,
        "breadcrumb":    [str(b).strip() for b in breadcrumb if str(b).strip()],
        "deep_link":     deep_link,
        "deep_link_url": DEEP_LINKS.get(deep_link, "") if deep_link else "",
    }


def _matcher_result(question: str, source_tag: str) -> dict:
    """Run the offline matcher and decorate with deep_link_url + source."""
    result = match_question(question)
    result.setdefault("deep_link_url", DEEP_LINKS.get(result.get("deep_link", ""), ""))
    result["_source"] = source_tag
    return result


def answer(question: str, history: list | None = None) -> dict:
    """Top-level entry. Returns the response dict the view will JSON-encode.

    Pure-Groq flow (matcher is ONLY a final safety net):
      1. Try every Groq model in rotation. First valid JSON wins.
      2. If every Groq model fails (rate limit, network, non-JSON), THEN
         fall back to the offline keyword matcher so the FAB still shows
         something useful.

    Matching the user's preference: Groq's conversational, Roman-Urdu-aware
    replies are higher quality than the matcher's canned KB phrasing.
    Mixed routing felt inconsistent in real use."""
    if not question or not question.strip():
        return _matcher_result(question or "", "matcher")

    # No API key → matcher is the only option.
    if not settings.GROQ_API_KEY:
        log.debug("groq_support: no GROQ_API_KEY, using offline matcher")
        return _matcher_result(question, "matcher")

    messages = _build_messages(question, history or [])
    t0 = time.time()
    for model in _MODELS:
        raw = _call_groq(model, messages)
        if raw is None:
            continue
        payload = _parse_json_payload(raw)
        if not payload:
            log.info("groq_support: %s returned non-JSON, trying next model", model)
            continue
        result = _normalise_payload(payload)
        result["_source"] = f"groq:{model}"
        result["_latency_ms"] = int((time.time() - t0) * 1000)
        return result

    # Every Groq model failed (rate-limited or down) — final safety net.
    log.info("groq_support: all models failed, falling back to keyword matcher")
    return _matcher_result(question, "matcher_fallback")
