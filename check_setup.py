"""
check_setup.py — confirms the keys are loaded and work, without ever printing them.

    python check_setup.py
"""

import os
import sys

import config


def masked(v: str) -> str:
    return f"{v[:7]}…{v[-4:]} ({len(v)} chars)" if v else "(not set)"


def main() -> int:
    ok = True
    print(f".env file: {config.ENV_FILE or 'not found (looked next to the scripts and in the current folder)'}")

    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    print(f"ANTHROPIC_API_KEY: {masked(key)}")
    if not key:
        ok = False
        print("  ✗ not set — CV tailoring still works (lexicon + template summary); AI features are skipped")
    elif not key.startswith("sk-ant-") or len(key) < 90:
        ok = False
        print("  ✗ looks incomplete — Anthropic keys start with sk-ant- and are about 100 characters; copy it again")
    else:
        try:
            import anthropic
            anthropic.Anthropic().models.list(limit=1)          # free call: checks the key only
            print("  ✓ accepted by the Anthropic API")
        except ImportError:
            ok = False
            print("  ✗ the anthropic package is missing: pip install -r requirements.txt")
        except Exception as e:
            ok = False
            status = getattr(e, "status_code", "")
            print(f"  ✗ rejected ({type(e).__name__} {status}) — create a new key at console.anthropic.com")

    g = os.getenv("GEMINI_API_KEY", "").strip()
    print(f"GEMINI_API_KEY: {masked(g)}")
    if g:
        try:
            import llm
            print(f"  ✓ accepted by the Gemini API — CV summaries will use {llm._gemini_pick_model(g)}")
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", "")
            print(f"  ✗ Gemini not usable ({type(e).__name__} {status}) — Claude will be used instead")
    else:
        print("  (not set — Claude is used for CV summaries)")

    for name in ("TG_API_ID", "TG_API_HASH"):
        v = os.getenv(name, "")
        print(f"{name}: {'set' if v else 'not set (Telegram agent will not run)'}")

    try:
        import dotenv  # noqa: F401
        print("python-dotenv: installed")
    except ImportError:
        print("python-dotenv: not installed (the built-in fallback parser is used; that's fine)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
