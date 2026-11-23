"""
llm.py — one text-generation call with automatic fallback:

    Gemini Flash (GEMINI_API_KEY)  →  Claude (ANTHROPIC_API_KEY)  →  None (callers use their offline path)

Keys come from .env (loaded by config). The model text is always treated as untrusted: callers validate it
(the CV summary is rejected if it adds any number or tool that isn't in cv_facts.json).
"""

import json
import os
import re

import config  # noqa: F401  — loads .env

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
_gemini_model: str | None = None
_disabled: set[str] = set()          # a provider that failed authentication isn't retried in this run


def _gemini_pick_model(key: str) -> str:
    """GEMINI_MODEL if set; otherwise the newest 'flash' model the key can use."""
    global _gemini_model
    if _gemini_model:
        return _gemini_model
    if os.getenv("GEMINI_MODEL"):
        _gemini_model = os.environ["GEMINI_MODEL"]
        return _gemini_model
    import requests
    r = requests.get(f"{GEMINI_URL}/models", headers={"x-goog-api-key": key}, params={"pageSize": 200}, timeout=20)
    r.raise_for_status()
    names = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in m["name"]
             and not re.search(r"lite|image|tts|audio|live|thinking|exp", m["name"])]
    if not names:
        raise RuntimeError("no Flash model available for this key")
    names.sort(key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"[.\-]", n)], reverse=True)
    _gemini_model = names[0]
    return _gemini_model


def _gemini(system: str, user: str, max_tokens: int) -> str:
    import requests
    key = os.environ["GEMINI_API_KEY"].strip()
    model = _gemini_pick_model(key)
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": max(max_tokens, 1500),
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    r = requests.post(f"{GEMINI_URL}/models/{model}:generateContent", headers={"x-goog-api-key": key},
                      json=body, timeout=60)
    if r.status_code in (400, 401, 403):
        _disabled.add("gemini")
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def _claude(system: str, user: str, max_tokens: int) -> str:
    import anthropic
    try:
        # thinking off + headroom, so the whole budget goes to the answer text
        r = anthropic.Anthropic().messages.create(model=config.MODEL_FAST, max_tokens=max(max_tokens, 1500),
                                                  system=system, thinking={"type": "disabled"},
                                                  messages=[{"role": "user", "content": user}])
    except anthropic.AuthenticationError:
        _disabled.add("claude")
        raise
    return " ".join(b.text for b in r.content if b.type == "text").strip()


def providers() -> list[str]:
    out = []
    if os.getenv("GEMINI_API_KEY") and "gemini" not in _disabled:
        out.append("gemini")
    if os.getenv("ANTHROPIC_API_KEY") and "claude" not in _disabled:
        out.append("claude")
    return out


def ask(system: str, user: str, max_tokens: int = 400) -> tuple[str | None, str | None]:
    """Returns (text, provider) or (None, None) when no provider works."""
    for name in providers():
        try:
            text = (_gemini if name == "gemini" else _claude)(system, user, max_tokens)
            if text:
                return text, name
        except Exception as e:
            _disabled.add(name)                       # don't retry a failing provider for every CV in this run
            print(f"ℹ️ {name} unavailable ({type(e).__name__}); using the next option for this run")
    return None, None


def ask_json_list(system: str, user: str, max_tokens: int = 400) -> list[str]:
    """Asks for a JSON array of strings and parses it leniently."""
    text, _ = ask(system + "\nReply with ONLY a JSON array of strings.", user, max_tokens)
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.S)
    try:
        return [str(x) for x in json.loads(m.group(0))] if m else []
    except Exception:
        return []
