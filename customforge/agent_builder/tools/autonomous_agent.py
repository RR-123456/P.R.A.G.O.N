"""
autonomous_agent.py — a genuinely autonomous agent, not a single LLM call.

Unlike gemini_agent.py (one prompt in, one reply out), this repeatedly:
  1. Shows Gemini the goal + everything that's happened so far
  2. Lets it choose: call one of the real tools (file_controller,
     file_processor, dev_agent, code_helper), or give a Final Answer
  3. If it chose a tool, actually runs it and feeds the real result back
  4. Repeats, up to max_iterations, until it gives a Final Answer

This is the classic ReAct pattern (Reason + Act), implemented as a plain
text loop so it doesn't depend on any particular SDK's function-calling
schema — just Gemini's ability to follow a response format.
"""

import json
import re

from .llm_client import get_model, LLMError
from .registry import TOOL_REGISTRY, run_tool

DEFAULT_MODEL = "auto"
DEFAULT_MAX_ITERATIONS = 6

FINAL_RE = re.compile(r"Final Answer:\s*(.*)", re.DOTALL)
ACTION_RE = re.compile(r"Action:\s*([a-zA-Z_]+)\s*\nAction Input:\s*(\{.*\})", re.DOTALL)
THOUGHT_RE = re.compile(r"Thought:\s*(.*?)(?=\nAction:|\nFinal Answer:|\Z)", re.DOTALL)


def _tool_descriptions(allowed_tools):
    lines = []
    for tool_id in allowed_tools:
        meta = TOOL_REGISTRY.get(tool_id)
        if not meta:
            continue
        field_list = ", ".join(f'"{f["name"]}"' for f in meta["fields"])
        lines.append(f"- {tool_id}: {meta['description']} Parameters (JSON keys): {field_list}")
    return "\n".join(lines)


def _build_prompt(goal: str, allowed_tools: list, scratchpad: str) -> str:
    return f"""You are an autonomous agent working step by step toward a goal, deciding your own actions.

Available tools — call at most one per step:
{_tool_descriptions(allowed_tools)}

Respond with EXACTLY one of these two formats, nothing else:

Thought: <your reasoning about what to do next>
Action: <one tool id from the list above, exactly as written>
Action Input: <a single JSON object with that tool's parameters>

OR, once you have enough information to fully answer the goal:

Thought: <your reasoning>
Final Answer: <the complete answer to the goal>

Goal: {goal}
{scratchpad}"""


def _extract_json_object(text: str):
    if not text:
        return None
    idx = text.find("{")
    if idx == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[idx:])
        return obj
    except Exception:
        return None


def _parse_step(text: str) -> dict:
    thought_match = THOUGHT_RE.search(text)
    thought = thought_match.group(1).strip() if thought_match else ""

    final_idx = text.find("Final Answer:")
    action_idx = text.find("Action:")
    if final_idx != -1 and (action_idx == -1 or final_idx < action_idx):
        final_match = FINAL_RE.search(text)
        if final_match:
            return {"thought": thought, "final_answer": final_match.group(1).strip()}

    action_match = ACTION_RE.search(text)
    if action_match:
        return {
            "thought": thought,
            "action": action_match.group(1).strip(),
            "action_input": _extract_json_object(action_match.group(2)),
            "raw_input": action_match.group(2).strip(),
        }

    return {"thought": thought or text.strip(), "unparsed": text.strip()}


def autonomous_agent(parameters: dict = None, response=None, player=None,
                      session_memory=None, speak=None) -> dict:
    """
    parameters:
        goal            : what the agent should accomplish
        model           : model name (default: "auto" = Ollama first,
                          Gemini backup). Use "ollama:<model>" or
                          "gemini-2.5-flash" to force a provider.
        max_iterations  : max tool calls before giving up (default: 6)
        allowed_tools   : list of tool ids it may use (default: all real tools)

    Returns {"final_answer": str, "transcript": [ {step, thought, action?,
    action_input?, observation?, final_answer?, error?}, ... ]}
    — a dict, not a plain string, since callers (the engine) want both the
    answer and the reasoning trail for display.
    """
    params = parameters or {}
    goal = (params.get("goal") or params.get("input") or "").strip()
    if not goal:
        return {"final_answer": "No goal provided.", "transcript": []}

    model_name = params.get("model") or DEFAULT_MODEL
    try:
        max_iterations = int(params.get("max_iterations") or DEFAULT_MAX_ITERATIONS)
    except (TypeError, ValueError):
        max_iterations = DEFAULT_MAX_ITERATIONS
    max_iterations = max(1, min(max_iterations, 15))

    real_tools = [t for t in TOOL_REGISTRY.keys() if t != "gemini_agent"]
    allowed_tools = params.get("allowed_tools") or real_tools
    allowed_tools = [t for t in allowed_tools if t in real_tools] or real_tools

    model = get_model(model_name)

    scratchpad = ""
    transcript = []
    seen_calls = set()

    for step_num in range(1, max_iterations + 1):
        prompt = _build_prompt(goal, allowed_tools, scratchpad)

        try:
            resp = model.generate_content(prompt)
            text = (resp.text or "").strip()
        except LLMError as e:
            transcript.append({"step": step_num, "error": str(e)})
            return {"final_answer": f"Autonomous agent stopped (model error): {e}", "transcript": transcript}
        except Exception as e:
            transcript.append({"step": step_num, "error": str(e)})
            return {"final_answer": f"Autonomous agent stopped (model error): {e}", "transcript": transcript}

        if player:
            player.write_log(f"[AutoAgent] step {step_num}")

        parsed = _parse_step(text)

        if "final_answer" in parsed:
            transcript.append({"step": step_num, "thought": parsed["thought"], "final_answer": parsed["final_answer"]})
            return {"final_answer": parsed["final_answer"], "transcript": transcript}

        action = parsed.get("action")
        if not action:
            scratchpad += (
                f"\n{text}\nObservation: Your last response didn't follow the required format "
                f"(Thought/Action/Action Input, or Thought/Final Answer). Try again.\n"
            )
            transcript.append({"step": step_num, "raw": text, "error": "unparsed response"})
            continue

        if action not in allowed_tools:
            observation = f"Unknown or disallowed tool '{action}'. Available: {', '.join(allowed_tools)}"
            scratchpad += f"\nThought: {parsed['thought']}\nAction: {action}\nAction Input: {parsed.get('raw_input','')}\nObservation: {observation}\n"
            transcript.append({"step": step_num, "thought": parsed["thought"], "action": action, "observation": observation})
            continue

        action_input = parsed.get("action_input") or {}
        call_signature = (action, json.dumps(action_input, sort_keys=True))
        if call_signature in seen_calls:
            observation = "You already made this exact call and got a result. Use a different action or give your Final Answer now."
        else:
            seen_calls.add(call_signature)
            observation = run_tool(action, dict(action_input))

        observation_short = (observation or "")[:1500]
        scratchpad += f"\nThought: {parsed['thought']}\nAction: {action}\nAction Input: {json.dumps(action_input)}\nObservation: {observation_short}\n"
        transcript.append({
            "step": step_num, "thought": parsed["thought"], "action": action,
            "action_input": action_input, "observation": observation_short,
        })

    last_obs = next((s.get("observation") for s in reversed(transcript) if "observation" in s), "(none)")
    return {
        "final_answer": f"Stopped after {max_iterations} steps without a Final Answer. Last observation: {last_obs}",
        "transcript": transcript,
    }
