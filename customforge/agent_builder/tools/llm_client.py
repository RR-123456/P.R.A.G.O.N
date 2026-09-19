"""
llm_client.py — single place that turns a "model name" into a real LLM call.

Agent Builder now runs Ollama-first: a free, local, no-quota model
(https://ollama.com) is the primary engine for every agent/tool step,
powered by Gemini as a backup — if Ollama isn't running, isn't reachable,
or errors out, the same step automatically retries on Gemini so a step
never just fails because your machine doesn't have a local model pulled
yet. Entirely self-contained within Agent Builder (its own
config/api_keys.json, no dependency on anything outside this folder).

Model routing:
    "auto" (default)    -> Ollama first; auto falls back to Gemini as
                           backup power if Ollama is unreachable or errors.
    "ollama:llama3.2"   -> Ollama only, forced, using "llama3.2".
    "ollama"            -> Ollama only, forced, using the configured/
                           default local model (no Gemini fallback).
    "gemini-2.5-flash"  -> Gemini only, forced (explicit cloud request,
                           no local fallback).

Every call returns an object with a `.text` attribute (matching the shape
of google-genai's response), so existing call sites that do `result.text`
don't need to change.
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# Primary, local, free model. Override per-call with "ollama:<model>", or
# set "ollama_model" / "ollama_host" in config/api_keys.json.
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:3b"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"

# Backup cloud model used whenever Ollama can't handle a step.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


class LLMError(Exception):
    pass


class _Result:
    """Minimal stand-in for google.genai's response object."""
    def __init__(self, text: str):
        self.text = text


def _read_config() -> dict:
    if not API_CONFIG_PATH.exists():
        return {}
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _gemini_api_key() -> str:
    return (_read_config().get("gemini_api_key") or "").strip()


def get_ollama_config() -> dict:
    cfg = _read_config()
    host = os.environ.get("OLLAMA_HOST") or cfg.get("ollama_host") or DEFAULT_OLLAMA_HOST
    model = os.environ.get("OLLAMA_MODEL") or cfg.get("ollama_model") or DEFAULT_OLLAMA_MODEL
    return {"host": host.rstrip("/"), "model": model}


def is_quota_or_auth_error(err: Exception) -> bool:
    msg = str(err).lower()
    return any(s in msg for s in (
        "429", "quota", "resource_exhausted", "rate limit", "rate_limit",
        "exceeded", "permission_denied", "api key", "unauthenticated",
    ))


def ollama_available(timeout: float = 1.5) -> bool:
    import requests
    host = get_ollama_config()["host"]
    try:
        r = requests.get(f"{host}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def list_ollama_models() -> list:
    """Returns the local model names the user has already `ollama pull`ed."""
    import requests
    host = get_ollama_config()["host"]
    try:
        r = requests.get(f"{host}/api/tags", timeout=3)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
    except Exception:
        return []


def gemini_configured() -> bool:
    return bool(_gemini_api_key())


def _call_ollama(model: str, prompt: str, timeout: int = 300) -> str:
    import requests

    cfg = get_ollama_config()
    host = cfg["host"]
    model = model or cfg["model"]

    # Ollama defaults num_ctx to 2048 tokens unless told otherwise, which
    # silently truncates anything longer — size it to the prompt instead.
    est_input_tokens = len(prompt) // 4
    num_ctx = min(32768, max(8192, est_input_tokens + 4096))

    try:
        resp = requests.post(
            f"{host}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_predict": 4096, "temperature": 0.6, "num_ctx": num_ctx},
            },
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError as e:
        raise LLMError(
            f"Could not reach Ollama at {host} ({e}). Install it from "
            f"https://ollama.com, run `ollama serve`, and `ollama pull {model}`."
        )
    except requests.exceptions.Timeout:
        raise LLMError(
            f"Ollama request timed out after {timeout}s. '{model}' may be too slow "
            f"on this machine — try a smaller model, e.g. qwen2.5-coder:1.5b."
        )
    except Exception as e:
        raise LLMError(f"Ollama request failed: {e}")

    if resp.status_code == 404 or "not found" in resp.text.lower():
        raise LLMError(
            f"Model '{model}' isn't pulled in Ollama yet. Run: ollama pull {model}"
        )
    if resp.status_code >= 400:
        raise LLMError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
        return ((data.get("message") or {}).get("content") or "").strip()
    except Exception as e:
        raise LLMError(f"Unexpected Ollama response: {e}")


def _call_gemini(model: str, prompt: str) -> str:
    from google import genai
    key = _gemini_api_key()
    if not key:
        raise LLMError(
            "No Gemini API key configured (config/api_keys.json), so there's "
            "no backup available. Set gemini_api_key, or make sure Ollama is "
            "installed and running (`ollama serve`)."
        )
    client = genai.Client(api_key=key)
    result = client.models.generate_content(model=model or DEFAULT_GEMINI_MODEL, contents=prompt)
    return (getattr(result, "text", None) or "").strip()


def get_model(model_name: str = "auto"):
    """
    Returns a wrapper with .generate_content(contents) -> object with .text,
    matching genai's client.models interface so existing call sites
    (`resp = model.generate_content(prompt); resp.text`) don't change.
    """
    name = (model_name or "").strip() or "auto"

    class _Wrapper:
        def generate_content(self, contents):
            # Explicit cloud request: "gemini..." -> Gemini only, no fallback.
            if name.lower().startswith("gemini"):
                text = _call_gemini(name, contents)
                return _Result(text)

            # Explicit local request: "ollama:<model>" -> Ollama only, no
            # Gemini fallback (the user asked for local specifically).
            if name.lower() == "ollama" or name.lower().startswith("ollama:"):
                local_model = name.split(":", 1)[1].strip() if ":" in name else ""
                text = _call_ollama(local_model, contents)
                return _Result(text)

            # Default ("auto"): Ollama first (free, local, no quota),
            # powered by Gemini as backup if Ollama can't handle this step.
            ollama_model = get_ollama_config()["model"]
            try:
                if not ollama_available():
                    raise LLMError(
                        f"Ollama isn't reachable at {get_ollama_config()['host']}."
                    )
                text = _call_ollama(ollama_model, contents)
                return _Result(text)
            except Exception as ollama_err:
                if not gemini_configured():
                    raise LLMError(
                        f"Ollama failed ({ollama_err}) and no Gemini API key is "
                        f"configured as backup. Start Ollama (`ollama serve`) or "
                        f"set gemini_api_key in config/api_keys.json."
                    )
                try:
                    text = _call_gemini(DEFAULT_GEMINI_MODEL, contents)
                except Exception as gemini_err:
                    raise LLMError(
                        f"Both providers failed — Ollama: {ollama_err}; "
                        f"Gemini backup: {gemini_err}"
                    )
                note = (
                    f"[Note: Ollama was unavailable ({ollama_err}); this response "
                    f"used the Gemini backup instead.]\n\n"
                )
                return _Result(note + text)

    return _Wrapper()
