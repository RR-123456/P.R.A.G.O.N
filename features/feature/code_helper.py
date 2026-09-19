"""
features/feature/code_helper.py — Code Helper

Inline code review, debugging, and generation, callable as a voice/chat
tool exactly like Pragon's other features (weather_report, web_search,
etc.). Reuses FRIDAY's already-running Ollama connection via
pragon_ui.ask_friday() instead of opening a second LLM connection --
no new dependencies, no duplicated HTTP/host/model plumbing.
"""

from pragon_ui import ask_friday

_VALID_MODES = {"review", "debug", "fix", "generate", "explain", "optimize"}


def code_helper_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    code = (parameters.get("code") or "").strip()
    instruction = (parameters.get("instruction") or "").strip()
    mode = (parameters.get("mode") or "review").strip().lower()
    language = (parameters.get("language") or "").strip()

    if mode not in _VALID_MODES:
        mode = "review"

    # "generate" mode doesn't require existing code -- the instruction
    # itself is the spec (e.g. "write a Python function that flattens
    # a nested list").
    if mode != "generate" and not code:
        msg = "Sir, I need the code before I can help with that."
        _log(msg, player)
        return msg

    if mode == "generate":
        if not instruction:
            msg = "Sir, tell me what to generate."
            _log(msg, player)
            return msg
        prompt = (
            f"You are a senior software engineer. Write {language or 'code'} "
            f"for this request:\n{instruction}\n\n"
            "Return a single clean code block. After the code, add at most "
            "two short sentences of explanation -- no more."
        )
    else:
        task_line = {
            "review":   "Review this code for bugs, style issues, and risks.",
            "debug":    "Find the bug causing the described problem and explain the root cause.",
            "fix":      "Fix the code so it works correctly, and return the corrected version.",
            "explain":  "Explain what this code does, step by step but concisely.",
            "optimize": "Optimize this code for performance/readability without changing behavior.",
        }[mode]
        extra = f"\nAdditional context from the user: {instruction}" if instruction else ""
        prompt = (
            f"You are a senior software engineer. {task_line}{extra}\n\n"
            f"Language: {language or 'auto-detect from the code'}.\n"
            f"Code:\n```{language}\n{code}\n```\n\n"
            "If you return modified code, put it in a single code block. "
            "Keep any explanation to at most three sentences."
        )

    reply = ask_friday(prompt)
    _log(f"Code Helper ({mode}) done.", player)

    if session_memory:
        try:
            session_memory.set_last_search(query=f"code_helper:{mode}", response=reply[:200])
        except Exception:
            pass

    return reply


def _log(message: str, player=None) -> None:
    print(f"[CodeHelper] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass
