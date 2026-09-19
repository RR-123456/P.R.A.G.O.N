"""
registry.py — maps Agent Builder canvas node configs to real Python callables.

Each entry describes:
  id          : stable identifier used in saved workflow JSON
  label       : shown in the UI
  description : shown in the UI
  func        : the actual function to call, signature (parameters: dict, **kw) -> str
  fields      : form fields the UI renders for configuring this node
  input_field : which field receives the upstream node's output by default
                (the user can still edit it before running)
"""

from . import file_controller as _fc
from . import file_processor as _fp
from . import dev_agent as _da
from . import code_helper as _ch
from . import gemini_agent as _ga
from . import agent_builder as _ab

TOOL_REGISTRY = {
    "gemini_agent": {
        "label": "Gemini Agent",
        "description": "Runs a prompt through Gemini. Use for reasoning, drafting, or deciding next steps.",
        "func": _ga.gemini_agent,
        "input_field": "input",
        "fields": [
            {"name": "prompt", "label": "Instructions", "type": "textarea", "default": ""},
            {"name": "model", "label": "Model (auto = Ollama first, Gemini backup)", "type": "text", "default": "auto"},
        ],
    },
    "file_controller": {
        "label": "File Controller",
        "description": "Filesystem operations: list, create, delete, move, copy, rename, read, write, find, disk usage, organize desktop.",
        "func": _fc.file_controller,
        "input_field": "content",
        "fields": [
            {"name": "action", "label": "Action", "type": "select", "default": "list",
             "options": ["list", "create_file", "create_folder", "delete", "move", "copy",
                         "rename", "read", "write", "find", "largest", "disk_usage",
                         "organize_desktop", "info"]},
            {"name": "path", "label": "Path / shortcut", "type": "text", "default": "desktop"},
            {"name": "name", "label": "File / folder name", "type": "text", "default": ""},
            {"name": "content", "label": "Content (create_file / write)", "type": "textarea", "default": ""},
            {"name": "destination", "label": "Destination (move / copy)", "type": "text", "default": ""},
            {"name": "new_name", "label": "New name (rename)", "type": "text", "default": ""},
            {"name": "extension", "label": "Extension (find)", "type": "text", "default": ""},
            {"name": "max_results", "label": "Max results (find)", "type": "number", "default": 20},
            {"name": "count", "label": "Count (largest)", "type": "number", "default": 10},
        ],
    },
    "file_processor": {
        "label": "File Processor",
        "description": "Processes an existing file: images, PDFs, docx, csv/xlsx, json, code, audio, video, archives, pptx.",
        "func": _fp.file_processor,
        "input_field": "instruction",
        "fields": [
            {"name": "file_path", "label": "File path", "type": "text", "default": ""},
            {"name": "action", "label": "Action (blank = auto)", "type": "text", "default": ""},
            {"name": "instruction", "label": "Instruction", "type": "textarea", "default": ""},
            {"name": "extra_params_json", "label": "Extra params (JSON, e.g. width/height/format)",
             "type": "textarea", "default": ""},
        ],
    },
    "dev_agent": {
        "label": "Dev Agent",
        "description": "Plans, writes, installs deps for, runs, and auto-fixes a whole small project from a description.",
        "func": _da.dev_agent,
        "input_field": "description",
        "fields": [
            {"name": "description", "label": "Project description", "type": "textarea", "default": ""},
            {"name": "language", "label": "Language", "type": "text", "default": "python"},
            {"name": "project_name", "label": "Project name (blank = auto)", "type": "text", "default": ""},
            {"name": "timeout", "label": "Run timeout (s)", "type": "number", "default": 30},
        ],
    },
    "code_helper": {
        "label": "Code Helper",
        "description": "Write, edit, explain, run, build, or optimize a single file of code.",
        "func": _ch.code_helper,
        "input_field": "description",
        "fields": [
            {"name": "action", "label": "Action", "type": "select", "default": "auto",
             "options": ["auto", "write", "edit", "explain", "run", "build", "optimize", "screen_debug"]},
            {"name": "description", "label": "Description / instruction", "type": "textarea", "default": ""},
            {"name": "language", "label": "Language", "type": "text", "default": "python"},
            {"name": "output_path", "label": "Output path (blank = Desktop)", "type": "text", "default": ""},
            {"name": "file_path", "label": "Existing file path", "type": "text", "default": ""},
            {"name": "timeout", "label": "Timeout (s)", "type": "number", "default": 30},
        ],
    },
    "agent_builder": {
        "label": "Build Agent",
        "description": "Generates a complete, standalone autonomous agent .py file with its own ReAct loop, tool imports, and memory. Run it with `python agent_generated.py`.",
        "func": _ab.build_agent,
        "input_field": "goal",
        "fields": [
            {"name": "goal", "label": "Agent Goal", "type": "textarea", "default": "Research the latest AI trends and write a summary report"},
            {"name": "model", "label": "Model (auto = Ollama first, Gemini backup)", "type": "text", "default": "auto"},
            {"name": "max_iterations", "label": "Max Steps", "type": "number", "default": 10},
            {"name": "output_path", "label": "Output Path (blank = Desktop/agent_generated.py)", "type": "text", "default": ""},
        ],
    },
}


def get_tool_metadata() -> dict:
    """JSON-safe metadata for the frontend (no function objects)."""
    return {
        tool_id: {
            "label": meta["label"],
            "description": meta["description"],
            "input_field": meta["input_field"],
            "fields": meta["fields"],
        }
        for tool_id, meta in TOOL_REGISTRY.items()
    }


def run_tool(tool_id: str, params: dict) -> str:
    meta = TOOL_REGISTRY.get(tool_id)
    if not meta:
        return f"Unknown tool: '{tool_id}'"

    # file_processor keeps most of its huge parameter surface in a JSON blob
    # so the UI doesn't need one field per possible action; merge it in here.
    extra_json = params.pop("extra_params_json", "")
    if extra_json:
        import json
        try:
            params.update(json.loads(extra_json))
        except Exception as e:
            return f"Invalid JSON in extra params: {e}"

    try:
        return meta["func"](parameters=params) or "Done."
    except TypeError:
        # gemini_agent / dev_agent / code_helper accept extra kwargs; retry
        # with just parameters in case a signature doesn't take **kwargs.
        try:
            return meta["func"](params) or "Done."
        except Exception as e:
            return f"Tool '{tool_id}' failed: {e}"
    except Exception as e:
        return f"Tool '{tool_id}' failed: {e}"
