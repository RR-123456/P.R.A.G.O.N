"""
gemini_agent.py — generic LLM "Agent" step for the Agent Builder.

Used by canvas nodes of type "agent" (as opposed to "tool" nodes, which call
file_controller / file_processor / dev_agent / code_helper directly). An agent
node runs a prompt through an LLM and returns the text, so it can be used for
reasoning, drafting, summarizing, or deciding what the next tool call should do.

Model routing (see tools/llm_client.py for details):
  "auto" (default)   -> Ollama first (free, local, no quota); auto falls
                        back to Gemini as backup power if Ollama isn't
                        running or errors out.
  "ollama:llama3.2"  -> Ollama only, forced.
  "gemini-2.5-flash" -> Gemini only, forced (explicit cloud request).
"""

from .llm_client import get_model, LLMError

DEFAULT_MODEL = "auto"

# Kept for backwards compatibility — autonomous_agent.py and others used to
# import GeminiConfigError / _get_client from here directly.
GeminiConfigError = LLMError


def _get_client():
    """Deprecated shim kept for old imports; prefer tools.llm_client.get_model."""
    return get_model(DEFAULT_MODEL)


def gemini_agent(parameters: dict = None, response=None, player=None,
                  session_memory=None, speak=None) -> str:
    """
    parameters:
        prompt : instructions for this agent step (its "job")
        input  : upstream text this step should act on (usually filled in
                 automatically from the previous node's output)
        model  : model name (default: "auto" = Ollama first, Gemini
                 backup). Use "ollama:<model>" or "gemini-2.5-flash" to
                 force a specific provider.
    """
    params = parameters or {}
    prompt = (params.get("prompt") or "").strip()
    upstream_input = (params.get("input") or "").strip()
    model_name = (params.get("model") or DEFAULT_MODEL).strip()

    if not prompt and not upstream_input:
        return "Agent step has no prompt and no input to act on."

    full_prompt = prompt
    if upstream_input:
        full_prompt = f"{prompt}\n\nInput:\n{upstream_input}" if prompt else upstream_input

    if player:
        player.write_log(f"[Agent] {prompt[:80] or '(no prompt)'}")

    try:
        model = get_model(model_name)
        result = model.generate_content(full_prompt)
        return (result.text or "").strip()
    except LLMError as e:
        return f"Agent step failed: {e}"
    except Exception as e:
        return f"Agent step failed: {e}"
