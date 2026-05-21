"""
Drop Sigma — Support AI Keyword Matcher.

Pure-Python, zero-API replacement for the Claude-backed support assistant.
Builds an inverted index over the FEATURES catalog in support_ai_kb.py at
import time, then matches incoming questions to the best (section, action)
pair using token overlap + synonym expansion. Output shape is identical to
what the frontend already renders:

    {reply, steps, breadcrumb, deep_link}

— so no JS changes are needed.

Why no AI?
  • KB is small (~14 sections, ~80 actions) and fully structured.
  • Answers are step-lists copied verbatim from the KB — no hallucination risk.
  • 0ms latency, no rate limit, no API key, no privacy concern.
"""

from __future__ import annotations
import re
from typing import Dict, List, Tuple, Optional

from core.support_ai_kb import FEATURES, DEEP_LINKS

# ─── Stopwords (common English words that don't discriminate sections) ──────
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "doing",
    "i", "me", "my", "we", "us", "our", "you", "your", "they", "them",
    "this", "that", "these", "those", "it", "its",
    "what", "which", "who", "when", "why", "how",
    "to", "of", "in", "on", "at", "by", "for", "with", "from",
    "and", "or", "but", "if", "as", "so", "than", "then",
    "can", "could", "would", "should", "may", "might", "must", "will", "shall",
    "please", "let", "lets", "go", "want", "wanna", "need", "needs",
    "tell", "show", "give",
})

# ─── Synonym map — query tokens expand to include synonyms before scoring ──
# Hand-curated for the dropshipping + ecommerce domain. Bi-directional pairs
# are listed both ways for simplicity.
_SYNONYMS: Dict[str, List[str]] = {
    # Inventory / stock
    "inventory":   ["stock", "warehouse"],
    "stock":       ["inventory", "warehouse"],
    "warehouse":   ["stock", "inventory"],
    "sku":         ["stock", "inventory", "product"],
    "product":     ["item", "sku"],
    "item":        ["product"],

    # Shipping / tracking
    "shipping":    ["tracking", "courier", "deliver"],
    "tracking":    ["shipping", "courier"],
    "courier":     ["tracking", "shipping"],
    "deliver":     ["shipping", "tracking"],
    "delivery":    ["shipping", "tracking"],

    # CRUD verbs
    "remove":      ["delete"],
    "delete":      ["remove"],
    "add":         ["create", "new", "make"],
    "create":      ["add", "new", "make"],
    "make":        ["add", "create", "new"],
    "new":         ["add", "create"],
    "edit":        ["update", "change", "modify"],
    "update":      ["edit", "change", "modify"],
    "change":      ["edit", "update"],
    "modify":      ["edit", "update"],

    # View / find
    "find":        ["search", "see", "view", "show", "where"],
    "see":         ["view", "find", "show"],
    "view":        ["see", "show", "find"],
    "show":        ["see", "view"],
    "search":      ["find"],

    # Email / messaging
    "email":       ["mail", "message", "inbox"],
    "mail":        ["email", "inbox"],
    "inbox":       ["email", "mail"],
    "message":     ["email", "chat"],
    "draft":       ["reply", "respond", "compose"],
    "compose":     ["draft", "write", "new"],
    "write":       ["compose", "draft"],
    "reply":       ["respond", "answer", "draft"],
    "respond":     ["reply", "answer"],
    "answer":      ["reply", "respond"],
    "send":        ["email", "reply"],

    # Team
    "team":        ["staff", "employee", "employees", "member", "members"],
    "staff":       ["team", "employee"],
    "employee":    ["team", "staff", "member"],
    "employees":   ["team", "staff", "member"],
    "member":      ["team", "employee"],
    "members":     ["team", "employees"],
    "invite":      ["add", "new"],

    # Vendors / suppliers
    "vendor":      ["supplier", "personal"],
    "vendors":     ["suppliers", "personal"],
    "supplier":    ["vendor"],
    "suppliers":   ["vendors"],

    # Stores
    "store":       ["shop", "site", "website", "platform"],
    "shop":        ["store"],
    "website":     ["store"],
    "woocommerce": ["store", "wc"],
    "shopify":     ["store"],
    "wc":          ["woocommerce", "store"],
    "connect":     ["link", "integrate", "add"],
    "link":        ["connect"],
    "integrate":   ["connect"],

    # Orders
    "order":       ["orders"],
    "orders":      ["order"],
    "purchase":    ["order"],
    "fulfill":     ["fulfillment", "ship"],
    "fulfillment": ["fulfill"],

    # Billing / plan
    "subscribe":   ["upgrade", "plan", "billing", "premium"],
    "subscription":["upgrade", "plan", "billing"],
    "upgrade":     ["subscribe", "plan", "billing", "premium", "pro", "paid"],
    "plan":        ["subscribe", "subscription", "upgrade", "billing"],
    "billing":     ["plan", "subscription", "upgrade", "payment", "pay"],
    "payment":     ["billing", "pay", "stripe", "paypal"],
    "pay":         ["payment", "billing"],
    "premium":     ["upgrade", "pro", "paid"],
    "pro":         ["premium"],

    # AI features
    "ai":          ["automation", "draft", "tone"],
    "automation":  ["ai", "auto"],
    "auto":        ["automatic", "automated", "automation"],
    "automatic":   ["auto", "automated"],

    # Tasks
    "task":        ["todo", "to-do"],
    "tasks":       ["todos"],
    "todo":        ["task"],

    # CSV
    "import":      ["upload", "csv", "xlsx"],
    "upload":      ["import"],
    "export":      ["download", "csv", "xlsx"],
    "download":    ["export"],
    "csv":         ["import", "export"],

    # Analytics
    "analytics":   ["report", "stats", "dashboard"],
    "stats":       ["analytics", "dashboard"],
    "report":      ["analytics"],
    "dashboard":   ["overview", "analytics"],
}

# Make synonyms bidirectional defensively (in case some pair is one-way).
_SYNONYMS_MAP: Dict[str, set] = {}
for k, vs in _SYNONYMS.items():
    _SYNONYMS_MAP.setdefault(k, set()).update(vs)
    for v in vs:
        _SYNONYMS_MAP.setdefault(v, set()).add(k)


_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    """Lowercase + strip non-alphanumeric + drop stopwords + dedupe order-preserving."""
    if not text:
        return []
    raw = _WORD_RE.findall(text.lower())
    seen = set()
    out = []
    for w in raw:
        if w in _STOPWORDS:
            continue
        if len(w) < 2:
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _expand(tokens: List[str]) -> Tuple[set, set]:
    """Return (direct_tokens, synonym_only_tokens) so scoring can give a
    bonus to direct hits over synonym-bridged hits. A direct token already
    in the query is NOT added to the synonym set even if another token
    happens to list it as a synonym."""
    direct = set(tokens)
    syn = set()
    for t in tokens:
        for s in _SYNONYMS_MAP.get(t, ()):
            if s not in direct:
                syn.add(s)
    return direct, syn


# ─── Build the index ONCE at import time ────────────────────────────────────
# Each doc is one (section_key, action_key) pair, plus a "section-level" doc
# for pages that have no specific action match (so "what is the stock page?"
# still routes to the Stock section).
_DOCS: List[Tuple[str, str, set, set, set, set, set]] = []
# tuple shape: (section_key, action_key, skey_tokens, label_tokens,
#               where_tokens, action_tokens, steps_tokens)

for skey, feat in FEATURES.items():
    if feat.get("_super_admin_only"):
        continue
    skey_t  = set(_tokenize(skey.replace("_", " ")))
    label_t = set(_tokenize(feat.get("label", "")))
    where_t = set(_tokenize(feat.get("where", "")))

    for akey, steps in feat.get("actions", {}).items():
        action_t = set(_tokenize(akey.replace("_", " ")))
        steps_text = " ".join(steps) if isinstance(steps, list) else str(steps)
        steps_t = set(_tokenize(steps_text))
        _DOCS.append((skey, akey, skey_t, label_t, where_t, action_t, steps_t))


# ─── Scoring weights ────────────────────────────────────────────────────────
_W_SKEY   = 10  # exact section-key match (e.g. user types "analytics")
_W_LABEL  = 6   # section label match (e.g., "Stock")
_W_ACTION = 5   # action key match (e.g., "delete", "connect_new")
_W_WHERE  = 3   # "Sidebar > Stock" path
_W_STEPS  = 1   # body text of the steps

# Minimum score to consider a match "confident" enough to skip the
# "did you mean?" fallback presentation.
_MIN_CONFIDENT_SCORE = 6


def _score(direct: set, syn: set, skey_t: set, label_t: set, where_t: set,
           action_t: set, steps_t: set) -> int:
    """Direct (query) tokens get full weight; synonym-only tokens get half
    weight (rounded down). A direct skey hit is the strongest signal and
    gets its own large bonus — e.g. typing "analytics" routes to the
    Analytics section even when 'dashboard' is a synonym of Overview."""
    s = 0
    # Direct skey hit — biggest signal
    s += _W_SKEY * len(direct & skey_t)
    # Direct hits
    s += _W_LABEL  * len(direct & label_t)
    s += _W_ACTION * len(direct & action_t)
    s += _W_WHERE  * len(direct & where_t)
    s += _W_STEPS  * len(direct & steps_t)
    # Synonym hits (half weight, integer-floored)
    s += (_W_LABEL  // 2) * len(syn & label_t)
    s += (_W_ACTION // 2) * len(syn & action_t)
    s += (_W_WHERE  // 2) * len(syn & where_t)
    s += (_W_STEPS  // 2) * len(syn & steps_t)
    return s


def _action_coverage(direct: set, syn: set, action_t: set) -> float:
    """Fraction of the action's own tokens that the query covered. Used as
    a tie-break so 'see orders' picks 'view list' (1 unrelated word: 'list')
    over 'see activity' (1 unrelated word: 'activity') when scores are
    equal — favours action keys whose tokens were ALL spoken/implied."""
    if not action_t:
        return 0.0
    hit = len(action_t & (direct | syn))
    return hit / len(action_t)


def _action_pretty(action_key: str) -> str:
    return action_key.replace("_", " ").strip()


def _format_match(section_key: str, action_key: str, confidence: str = "high") -> dict:
    feat = FEATURES[section_key]
    steps_raw = feat["actions"][action_key]
    label = feat.get("label", section_key.title())

    # Build structured step list (the frontend renders these as a card).
    steps = []
    for line in steps_raw:
        steps.append({"action": str(line)})

    breadcrumb = [s.strip() for s in feat.get("where", "").split(">") if s.strip()]

    # Reply summary — one line. Keep it human and grounded.
    action_label = _action_pretty(action_key)
    # Verbs that read naturally after "how to …" — anything else gets a
    # "guide for" phrasing to avoid awkward grammar like "how to billing".
    _VERB_PREFIXES = (
        "add", "create", "new", "delete", "remove", "edit", "update", "change",
        "view", "see", "find", "connect", "link", "import", "export", "upload",
        "download", "assign", "approve", "reject", "send", "compose", "draft",
        "reply", "manage", "set", "configure", "setup", "invite", "open",
        "close", "resolve", "track", "fulfill", "pause", "resume", "delete",
        "check", "auto", "bulk",
    )
    first_word = action_label.split(" ", 1)[0]
    if action_label in ("view", "view list"):
        reply = f"Here's where to find **{label}** in Drop Sigma:"
    elif action_label == "supported":
        reply = f"Here's what {label} supports:"
    elif first_word in _VERB_PREFIXES:
        reply = f"Here's how to **{action_label}** in **{label}**:"
    else:
        reply = f"Here's the **{action_label}** guide for **{label}**:"

    return {
        "reply": reply,
        "steps": steps,
        "breadcrumb": breadcrumb,
        "deep_link": section_key if section_key in DEEP_LINKS else "",
        "_confidence": confidence,
    }


def _format_low_confidence(top_candidates: List[Tuple[int, str, str]]) -> dict:
    """Multiple weak matches — present 'did you mean?' suggestions."""
    suggestions = []
    seen_sections = set()
    for _score_val, skey, akey in top_candidates:
        if skey in seen_sections:
            continue
        seen_sections.add(skey)
        feat = FEATURES[skey]
        suggestions.append({
            "action": f"{feat.get('label', skey.title())} → {_action_pretty(akey)}",
            "hint":   feat.get("where", ""),
        })
        if len(suggestions) >= 3:
            break

    return {
        "reply": "I'm not 100% sure which feature you mean. Did you mean one of these?",
        "steps": suggestions,
        "breadcrumb": [],
        "deep_link": "",
        "_confidence": "low",
    }


def _fallback_no_match() -> dict:
    """No match at all — friendly nudge with a list of top-level topics."""
    topics = []
    for skey, feat in list(FEATURES.items())[:8]:
        if feat.get("_super_admin_only"):
            continue
        topics.append(feat.get("label", skey.title()))
    return {
        "reply": (
            "I couldn't find that in the Drop Sigma help guide. Try asking about: "
            + ", ".join(topics)
            + ". Or rephrase with the feature name (e.g. \"how do I add stock?\")."
        ),
        "steps": [],
        "breadcrumb": [],
        "deep_link": "",
        "_confidence": "none",
    }


def match_question(question: str) -> dict:
    """Match a tenant question to the best KB entry. Always returns the
    same dict shape the frontend renders. Never raises."""
    if not question or not question.strip():
        return _fallback_no_match()

    tokens = _tokenize(question)
    if not tokens:
        return _fallback_no_match()
    direct, syn = _expand(tokens)

    scored: List[Tuple[int, float, str, str]] = []
    for skey, akey, skey_t, label_t, where_t, action_t, steps_t in _DOCS:
        s = _score(direct, syn, skey_t, label_t, where_t, action_t, steps_t)
        if s > 0:
            cov = _action_coverage(direct, syn, action_t)
            scored.append((s, cov, skey, akey))

    if not scored:
        return _fallback_no_match()

    # Sort by score desc, then by action-coverage desc (tie-break favours
    # actions whose own tokens are fully accounted for by the query).
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top_score, _top_cov, top_skey, top_akey = scored[0]

    # Tie-break: if top two are very close AND below confidence, ambiguous.
    if len(scored) >= 2 and scored[1][0] >= top_score - 1 and top_score < _MIN_CONFIDENT_SCORE:
        distinct_sections = {row[2] for row in scored[:4]}
        if len(distinct_sections) >= 2:
            return _format_low_confidence([(r[0], r[2], r[3]) for r in scored[:4]])

    if top_score < _MIN_CONFIDENT_SCORE:
        # Single weak match — show it anyway, plus a hint.
        result = _format_match(top_skey, top_akey, confidence="low")
        result["reply"] = "Best match I found — let me know if you meant something else:"
        return result

    return _format_match(top_skey, top_akey, confidence="high")
