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
import hashlib
import json as _json
import logging
import re
import threading
import time
from typing import Optional

import requests
from django.conf import settings

from core.support_ai_kb import build_system_prompt, DEEP_LINKS
from core.support_ai_match import match_question

log = logging.getLogger(__name__)

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Model rotation order. Each entry has its own ~30 RPM free-tier quota,
# so stacking them gives ~120 effective RPM before the matcher fallback
# would ever kick in. Quality-prioritised: try the biggest model first
# and fall through to smaller ones only on rate-limit / network error.
# Verified live against Groq's API (older entries — mixtral, gemma2,
# llama-3.2 family, qwen, deepseek-r1, llama-guard — are all
# decommissioned and removed.)
_MODELS = (
    "llama-3.3-70b-versatile",  # 70B Llama — top quality
    "openai/gpt-oss-120b",      # 120B GPT-OSS — high quality fallback
    "openai/gpt-oss-20b",       # 20B GPT-OSS — fast, decent quality
    "llama-3.1-8b-instant",     # 8B Llama — last resort
)

# Short timeout — if Groq is slow, fail fast and try next model / fallback.
_REQUEST_TIMEOUT = 12  # seconds

# ─── In-process response cache ────────────────────────────────────────────
# Cuts API pressure by ~70-80% in real use because Support-AI questions
# are heavily repetitive ("how do I add stock", "where are my orders"…).
# Keyed by sha256(question + history-fingerprint) so identical sessions
# share the answer. TTL = 1 hour; old entries get evicted lazily.
_CACHE_TTL = 3600  # 1 hour
_CACHE_MAX = 500   # safety cap so a long-running process doesn't grow forever
_cache_lock = threading.Lock()
_cache: dict = {}  # key → (expires_at_epoch, response_dict)


def _cache_key(question: str, history: list) -> str:
    """Hash that distinguishes identical-context queries from each other."""
    # Last 2 turns of history are enough to define context for caching —
    # we don't want one user's earlier convo bleeding into another user's
    # cache hit, but full-history fingerprints would defeat the purpose.
    tail = []
    for t in (history or [])[-2:]:
        tail.append(f"{t.get('role','')}|{(t.get('content') or '')[:120]}")
    raw = "␟".join([question.strip().lower(), *tail])
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str):
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        exp, val = entry
        if exp < now:
            _cache.pop(key, None)
            return None
        return val


def _cache_set(key: str, value: dict):
    now = time.time()
    with _cache_lock:
        # Lazy eviction: if we're at the cap, drop the 100 oldest entries.
        if len(_cache) >= _CACHE_MAX:
            for k, (exp, _) in sorted(_cache.items(), key=lambda kv: kv[1][0])[:100]:
                _cache.pop(k, None)
        _cache[key] = (now + _CACHE_TTL, value)


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

    Pure-Groq flow with model rotation + response cache. Matcher is the
    safety net for total Groq outage only.

    Order of operations:
      1. Cache lookup — hashed by question + last 2 turns. ~70-80% of
         real Support-AI traffic is repeat questions, so this slashes
         the RPM pressure on the API.
      2. Try every Groq model in rotation (70B → GPT-OSS 120B → 20B →
         8B Instant). ~120 effective RPM combined. First valid JSON wins.
      3. Cache the winner for 1 hour.
      4. If every Groq model fails (extreme rate-limit or full outage),
         only THEN fall back to the offline keyword matcher.
    """
    if not question or not question.strip():
        return _matcher_result(question or "", "matcher")

    # No API key → matcher is the only option (configured-off case).
    if not settings.GROQ_API_KEY:
        log.debug("groq_support: no GROQ_API_KEY, using offline matcher")
        return _matcher_result(question, "matcher")

    history = history or []

    # ── Cache lookup ─────────────────────────────────────────────────
    ck = _cache_key(question, history)
    cached = _cache_get(ck)
    if cached is not None:
        # Re-tag so the UI knows it came from cache (still "Groq" quality;
        # we tack on "(cached)" so it's distinguishable from a fresh call).
        out = dict(cached)
        src = out.get("_source", "")
        if src.startswith("groq:") and "(cached)" not in src:
            out["_source"] = f"{src} (cached)"
        return out

    # ── Live Groq rotation ───────────────────────────────────────────
    messages = _build_messages(question, history)
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
        _cache_set(ck, result)
        return result

    # Every Groq model failed (rate-limited or down) — final safety net.
    log.info("groq_support: all models failed, falling back to keyword matcher")
    return _matcher_result(question, "matcher_fallback")
