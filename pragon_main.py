import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None) # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import atexit
import os
import re
import socket
import threading
import queue
import time
import json
import base64
import sys
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# ── Force IPv4-only DNS resolution ────────────────────────────────────────
# Root cause of the common "TimeoutError: timed out during opening handshake"
# when connecting to the Gemini Live WebSocket: many Windows machines have a
# broken/misconfigured IPv6 route (common with certain VPNs, routers, or ISPs).
# asyncio's default resolver will happily hand back an IPv6 address for
# generativelanguage.googleapis.com, the OS then blackholes the connection
# attempt, and the whole handshake sits until it times out — even though a
# plain IPv4 connection would work instantly. Forcing every getaddrinfo() call
# in this process to IPv4 sidesteps that class of failure entirely.
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

import sounddevice as sd
from google import genai
from google.genai import types
from pragon_ui import PragonUI
from consciousness.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt, search_memory,
)
from consciousness.sqlite_memory import SQLiteMemory
from consciousness.unified_memory import UnifiedMemory
from consciousness.config_manager import (
    get_brief_enabled, save_plugin_enabled,
    get_input_device_name, get_output_device_name, save_audio_devices,
    get_wake_word_enabled, save_wake_word_enabled,
)
from pragon_brain.pragon_library_rag import LibraryStore
# -- plugin system (ported from Mark-LI) -------------------------------------
from pragoncore.plugin_loader import discover_plugins, save_uploaded_plugin, delete_plugin_file

# -- utils/ integration ------------------------------------------------------
from features.ultifeature.logger import JarvisLogger
from features.ultifeature.language_detect import LanguageDetector
from features.ultifeature.screen_vision import ScreenVision

# -- agentic/ integration -----------------------------------------------------
from features.agentic_feature.confirmation_gate import ConfirmationGate
from features.agentic_feature.morning_brief import MorningBrief
from features.agentic_feature.nightly_recap import NightlyRecap
from features.agentic_feature.deadline_manager import DeadlineManager
from features.agentic_feature.calendar_integration import CalendarIntegration
from features.agentic_feature.github_monitor import GitHubMonitor
from features.agentic_feature.n8n_integration import N8NIntegration
from features.agentic_feature.crew_orchestrator import CrewOrchestrator
from features.agentic_feature.action_agent import ActionAgent

from features.feature.file_processor import file_processor
from features.feature.file_generator import file_generator
from features.feature.flight_finder import flight_finder
from features.feature.open_app import open_app
from features.feature.weather_report import weather_action
from features.feature.code_helper import code_helper_action
from features.agentic_feature.pragon_agent import pragon_agent_action
from features.agentic_feature.agent.task_queue import get_queue as _get_agent_queue, TaskPriority as _AgentTaskPriority
from features.feature.send_message import send_message
from features.feature.reminder import reminder
from features.feature.computer_settings import computer_settings
from features.feature.screen_processor import _capture_camera, _capture_screen
from features.feature.youtube_video import youtube_video
from features.feature.desktop import desktop_control
from features.feature.browser_control import browser_control
from features.feature.file_controller import file_controller
from features.feature.compass import compass
from features.feature.pragon_builder import pragon_builder
from features.feature.web_search import web_search as web_search_action
from features.feature.computer_control import computer_control
from features.feature.game_updater import game_updater
from features.feature.custom_forge import custom_forge
from features.feature.pragon_compass import pragon_compass
from features.feature.system_monitor import SystemMonitor, get_system_status
from features.feature.cpu_balancer import CPUBalancer, cpu_balancer_tool, get_cpu_breakdown
from pragoncore.confirm_gate import ConfirmGate
from pragoncore.undo import undo_last
from pragoncore import audio_devices
from pragoncore import wake_word
from features.feature.firewall_control import firewall_control
from features.feature.network_firewall import network_firewall
from features.feature.waf_proxy import waf_proxy
from features.feature.proactive import ProactiveEngine
from features.feature.macro_action import (
    macro_action, list_macro_names, ui_start_recording, ui_stop_and_save,
    ui_rename, ui_delete, ui_play,
)


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def agent_task_action(parameters: dict, player=None, speak=None, session_memory=None) -> str:
    """
    Runs an open-ended, multi-step goal through the planner/executor
    orchestrator (features/agentic_feature/agent), instead of a single
    fixed tool call. It plans a sequence of steps across every existing
    tool (open_app, web_search, file_controller, pragon_agent,
    code_helper, etc.), runs them one at a time, and retries/replans on
    failure. Runs in the background task queue so this call returns
    immediately with an acknowledgement.
    """
    p = parameters or {}
    goal = (p.get("goal") or "").strip()
    if not goal:
        return "Please tell me the goal, sir."

    queue = _get_agent_queue()
    task_id = queue.submit(goal, priority=_AgentTaskPriority.NORMAL, speak=speak)
    print(f"[AgentTask] Queued: [{task_id}] {goal[:60]}")
    return f"On it, sir. Working through that now (task {task_id})."


async def _diagnose_live_connectivity(host: str = "generativelanguage.googleapis.com", port: int = 443) -> str:
    """Best-effort probe to turn a bare 'timed out' into an actionable reason.

    Distinguishes: DNS resolution failure, TCP-level block (firewall/VPN/
    proxy silently dropping the connection), vs. a slow-but-working path
    (in which case the Live API handshake itself is the bottleneck, not
    basic connectivity).
    """
    loop = asyncio.get_event_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP), timeout=5
        )
    except Exception as e:
        return f"DNS resolution failed for {host} ({e}). Check your internet/DNS settings."
    if not infos:
        return f"DNS resolution returned no results for {host}."
    try:
        fut = asyncio.open_connection(host=host, port=port)
        reader, writer = await asyncio.wait_for(fut, timeout=6)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return (
            f"Basic TCP/TLS reachability to {host}:{port} is OK — the plain "
            "network path works, so the failure is specific to the Live API "
            "WebSocket handshake itself. This usually means a proxy, deep "
            "packet inspection firewall, or antivirus 'network shield' is "
            "intercepting/blocking wss:// traffic specifically, or the "
            "region/account doesn't have Live API access yet."
        )
    except asyncio.TimeoutError:
        return (
            f"TCP connection to {host}:{port} itself timed out — something "
            "on this network (firewall, VPN, restrictive router/ISP, or "
            "antivirus) is silently blocking outbound HTTPS to Google's "
            "servers on this path. Try disabling VPN/proxy, or a different "
            "network, and test again."
        )
    except Exception as e:
        return f"TCP connection to {host}:{port} failed outright: {e}"


BASE_DIR = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "api" / "api_keys.json"
PROMPT_PATH = BASE_DIR / "pragoncore" / "protocol.txt"
LIVE_MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 1024

# -- agentic/utils singletons (stateless / self-contained; safe at module scope) --
log = JarvisLogger(str(BASE_DIR / "pragon_brain" / "pragon_log.log"))
language_detector = LanguageDetector()
calendar_integration = CalendarIntegration()
github_monitor = GitHubMonitor()
n8n = N8NIntegration()
crew = CrewOrchestrator()
screen_vision_local = ScreenVision()
sqlite_mem = SQLiteMemory(str(BASE_DIR / "pragon_brain" / "pragon_brain.db"))
unified_mem = UnifiedMemory(str(BASE_DIR / "consciousness" / "unified"))


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


# -- Universal RAG System bridge (standalone FastAPI service, see ragsystem/) --
# Complements library_store below: library_store uses Gemini's own multimodal
# understanding, while ragsystem runs fully offline via Ollama + ChromaDB and
# additionally handles audio/video transcription and image OCR. Purely
# additive and fails silently if the service isn't running -- nothing above
# this depends on it.
RAG_HOST = os.environ.get("RAG_HOST", "http://localhost:8000")

# -- Auto-start: launches the ragsystem FastAPI service itself if it isn't
# already running, so nobody has to remember to run `uvicorn` by hand.
# Only attempted when RAG_HOST points at this machine -- if it's remote,
# that server has to be started over there. Set RAG_SYSTEM_DIR if the
# ragsystem folder isn't a sibling of this file (e.g. ragsystem/).
RAG_SYSTEM_DIR = Path(os.environ.get("RAG_SYSTEM_DIR", str(BASE_DIR / "ragsystem")))
_rag_process = None


def _rag_health_ok(timeout: float = 2.0) -> bool:
    try:
        import requests
        r = requests.get(f"{RAG_HOST}/health", timeout=timeout)
        return r.ok
    except Exception:
        return False


def _rag_host_is_local(host: str) -> bool:
    try:
        return urlparse(host).hostname in ("localhost", "127.0.0.1", "0.0.0.0", None)
    except Exception:
        return False


def _rag_python_executable() -> str:
    """Prefer the ragsystem's own venv (per its README) if one exists next
    to it, so its dependencies don't have to be installed into whatever
    environment is running JARVIS. Falls back to this same interpreter."""
    venv_dir = RAG_SYSTEM_DIR / "venv"
    candidate = venv_dir / "Scripts" / "python.exe" if _platform.system() == "Windows" else venv_dir / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def _ensure_rag_service():
    """Best-effort auto-start, run from a background thread at launch so it
    never blocks JARVIS coming online. If anything here fails, knowledge_base
    / FRIDAY's RAG grounding just fall back to their existing 'service
    unreachable' messaging -- nothing else depends on this succeeding."""
    global _rag_process
    if _rag_health_ok():
        log.log("RAG", "Knowledge base service already running.")
        return
    if not _rag_host_is_local(RAG_HOST):
        log.log("RAG", f"RAG_HOST ({RAG_HOST}) isn't this machine -- start it there manually. Not auto-starting.")
        return
    if not RAG_SYSTEM_DIR.is_dir():
        log.log("RAG", f"ragsystem folder not found at {RAG_SYSTEM_DIR} -- set RAG_SYSTEM_DIR if it lives elsewhere. Skipping auto-start.")
        return

    port = urlparse(RAG_HOST).port or 8000
    python_exe = _rag_python_executable()
    log.log("RAG", f"Starting knowledge base service on port {port} (using {python_exe})...")
    try:
        _rag_process = _subprocess.Popen(
            [python_exe, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)],
            cwd=str(RAG_SYSTEM_DIR),
            stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
        )
    except Exception as e:
        log.log("RAG", f"Failed to launch knowledge base service: {e}")
        return

    # Cold start can be slow (embedding model download on first run) -- give
    # it real time before giving up, polling rather than a fixed sleep.
    for _ in range(40):
        time.sleep(1.5)
        if _rag_health_ok(timeout=1.5):
            log.log("RAG", "Knowledge base service is up.")
            return
        if _rag_process.poll() is not None:
            log.log("RAG", f"Knowledge base service exited early (code {_rag_process.returncode}) -- check its dependencies (pip install -r ragsystem/requirements.txt) and that Ollama is running.")
            return
    log.log("RAG", "Knowledge base service still isn't responding after 60s -- it may still be downloading models. It'll pick up on its own once ready.")


atexit.register(lambda: _rag_process and _rag_process.poll() is None and _rag_process.terminate())


def _rag_upload(filepath: str, filename: str) -> dict | None:
    """Pushes an already-saved file into the RAG system's vector store.
    Returns the ingestion result dict, or None if the service is unreachable
    or the file type isn't supported (e.g. plain library-only formats)."""
    try:
        import requests
        with open(filepath, "rb") as f:
            r = requests.post(f"{RAG_HOST}/upload", files={"file": (filename, f)}, timeout=60)
        if r.ok:
            return r.json()
        return None
    except Exception as e:
        log.log("RAG", f"Upload skipped for '{filename}': {e}")
        return None


def _rag_query(question: str, top_k: int = 5) -> str:
    """Asks the RAG system's own local model (Qwen via Ollama) a question
    grounded in whatever's been ingested. Returns a JARVIS-tool-friendly
    string, or a clear 'unavailable' message the model can relay verbatim
    instead of guessing."""
    try:
        import requests
        r = requests.post(f"{RAG_HOST}/query", json={"question": question, "top_k": top_k}, timeout=60)
        if not r.ok:
            return "The local knowledge base service returned an error -- it may still be starting up."
        d = r.json()
        answer = d.get("answer", "").strip()
        sources = d.get("sources", [])
        if not answer:
            return "The knowledge base has nothing relevant for that."
        return f"{answer}\n\n(Source: {', '.join(sources)})" if sources else answer
    except Exception:
        return (
            "The local knowledge base (ragsystem) isn't running or isn't reachable at "
            f"{RAG_HOST}. Tell the user to start it with: uvicorn app.main:app --port 8000"
        )


# -- Library RAG (files added via drag-and-drop / upload / Library controls) --
# Same singleton pattern as sqlite_mem/unified_mem above; must be created after
# _get_api_key() exists since it's passed in as a callable, not called yet.
library_store = LibraryStore(str(BASE_DIR / "library_store"), _get_api_key)


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)
_UPLOAD_MARKER_RE = re.compile(r"📎 File uploaded: (.+?) \(saved at (.+?)\)")

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

# ── Instant acknowledgment ───────────────────────────────────────────────────
# Tools that genuinely take a beat (a web search, reading a file, generating
# code) get an immediate, natural-language filler spoken *before* they run, so
# there's never a silent gap. Fast/instant tools (open_app, volume, system
# status) are deliberately excluded — chatter before a one-frame action just
# adds noise. `agent_task` has its own dedicated ack built into its handler,
# so it's excluded here too to avoid saying it twice.
INSTANT_ACK_TOOLS = {
    "web_search", "file_processor", "file_generator", "code_helper",
    "screen_process", "pragon_agent", "library_ask", "knowledge_base",
    "flight_finder", "game_updater", "github_status", "calendar_check",
    "n8n_workflow", "crew_task", "action_agent", "pragon_builder",
    "custom_forge",
}


def _instant_ack_prompt(name: str) -> str:
    """
    Instructional text sent through the live session (same channel used for
    [SYSTEM_ALERT] messages) — tells the model to say one short natural line,
    in the user's own language, immediately, without waiting for the tool.
    """
    return (
        f"[INSTANT_ACK] The user's request just started running in the background "
        f"({name}) and will take a moment. Say ONE short, natural sentence right now, "
        f"in the user's own language, acknowledging that you're on it "
        f"(e.g. \"On it -- going through that now.\"). Do not wait for the result "
        f"before speaking, and do not repeat this instruction back."
    )


TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "custom_forge",
        "description": (
            "Opens C.U.S.T.O.M FORGE, the drawing-pad app builder embedded in P.R.A.G.O.N's "
            "web UI. Use when the user asks to open/launch FORGE, the drawing pad, the app "
            "builder, or CustomDraw. Takes no parameters."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "pragon_compass",
        "description": (
            "Opens P.R.A.G.O.N COMPASS, the live interactive map / 3D globe / "
            "satellite tracker embedded in P.R.A.G.O.N's web UI. Use when the "
            "user asks to open/launch COMPASS, see a map, track satellites, "
            "or view the globe. Takes no parameters."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web. Use for ANY question about current facts, events, prices, "
            "or topics — always prefer this over guessing. "
            "Modes: 'search' (default), 'news' (latest headlines on a topic), "
            "'research' (deep comprehensive answer), 'price' (product cost lookup), "
            "'compare' (side-by-side comparison of items)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Search query or topic"},
                "mode": {"type": "STRING", "description": "search | news | research | price | compare"},
                "items": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | features"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "cpu_balancer",
        "description": (
            "Balances CPU usage across cores by lowering the priority and re-spreading "
            "the core affinity of processes that are hogging the CPU. Use when the user "
            "asks to balance, spread out, even out, or fix uneven CPU load, or reports "
            "the computer feeling laggy/hot due to one heavy process. "
            "Modes: 'balance' (default, actually rebalances), 'status' (just reports "
            "the current per-core breakdown without changing anything)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "mode": {"type": "STRING", "description": "balance | status"},
                "hog_threshold": {"type": "NUMBER", "description": "CPU%% above which a process is considered a hog (default 15)"},
                "max_rebalance": {"type": "NUMBER", "description": "Max number of processes to touch in one pass (default 6)"},
            },
        }
    },
    {
        "name": "undo_action",
        "description": (
            "Reverses the most recent reversible action PRAGON took — a file it moved or "
            "renamed, or a setting it changed. Use when the user says 'undo', 'undo that', "
            "'put it back', or 'that's not what I meant, reverse it'. Only affects "
            "reversible actions; does not touch a deleted file already in the trash."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Searches the FULL long-term memory store on demand — including facts that are "
            "too old or too numerous to fit in the standing prompt context. Use when the "
            "user references something from further back than what's already in view "
            "('what did I tell you about my sister', 'do you remember my flight dates')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "What to search for, e.g. a name, topic, or keyword"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "code_helper",
        "description": (
            "Reviews, debugs, fixes, explains, optimizes, or generates code. "
            "Use when the user pastes code and asks for help with it, or "
            "asks to write/generate a piece of code."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "code": {"type": "STRING", "description": "The code to work with (omit for pure generation requests)"},
                "instruction": {"type": "STRING", "description": "What the user wants done, in their own words"},
                "mode": {"type": "STRING", "description": "One of: review, debug, fix, generate, explain, optimize"},
                "language": {"type": "STRING", "description": "Programming language, if known"}
            },
            "required": ["mode"]
        }
    },
    {
        "name": "pragon_agent",
        "description": (
            "Autonomously plans and builds a complete, multi-file coding project from a "
            "high-level goal: designs the file structure, writes every file, installs "
            "dependencies, runs the project, and automatically debugs/fixes errors in a "
            "retry loop until it runs cleanly (or reports what's still broken). "
            "Use for requests like 'build me a script that...' or 'create a small "
            "tool/app that...' -- multi-step, hands-off coding work, not quick snippets."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {"type": "STRING", "description": "What to build, in plain language"},
                "language": {"type": "STRING", "description": "Preferred programming language, if any (default python)"},
                "project_name": {"type": "STRING", "description": "Optional folder name for the project"},
                "timeout": {"type": "STRING", "description": "Optional run timeout in seconds (default 30)"}
            },
            "required": ["goal"]
        }
    },
    {
        "name": "agent_task",
        "description": (
            "Autonomously plans and executes an open-ended goal that spans MULTIPLE "
            "tools/steps (e.g. 'research X and save a report', 'find a flight and email "
            "me the details', 'organize my desktop and set a reminder'). Breaks the goal "
            "into steps, runs each one, and retries or replans on failure. Runs in the "
            "background and replies immediately with an acknowledgement -- use this "
            "instead of chaining several single-purpose tools yourself, and instead of "
            "pragon_agent when the goal isn't purely 'build a coding project'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {"type": "STRING", "description": "The overall goal, in plain language"}
            },
            "required": ["goal"]
        }
    },
    {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver": {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform": {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date": {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time": {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query": {"type": "STRING", "description": "Search query for play action"},
                "save": {"type": "BOOLEAN", "description": "Save summary to Library (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url": {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text": {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when user says: close camera, stop camera, turn off camera, "
            "shut the camera, that's creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value": {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "firewall_control",
        "description": (
            "Controls the computer's OS firewall: check status, enable/disable it, "
            "or block/allow specific ports and IP addresses. "
            "Use for ANY request to check, turn on/off, or manage the firewall."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "status | enable | disable | block_port | allow_port | block_ip | allow_ip"},
                "value": {"type": "STRING", "description": "Port number or IP address, depending on the action"},
                "protocol": {"type": "STRING", "description": "TCP or UDP, only used for port actions. Default TCP"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "network_firewall",
        "description": (
            "Controls Pragon's stateful inspection firewall (SIF): tracks connection state "
            "(NEW/ESTABLISHED/RELATED/INVALID), auto-blocks IPs showing port-scan/flood behavior, "
            "and enforces an allow-list of ports for brand-new inbound connections. "
            "Use for requests about network-level or stateful firewall monitoring."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "start | stop | status | log | allow_port | disallow_port"},
                "value": {"type": "STRING", "description": "Port number, comma-separated ports for start, or log line count"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "waf_proxy",
        "description": (
            "Controls Pragon's Web Application Firewall (WAF) reverse proxy: inspects HTTP requests "
            "for SQL injection, XSS, path traversal, command injection, and template/JNDI injection "
            "before forwarding clean traffic to a local upstream service (e.g. PhoneView). "
            "Use for requests about a proxy WAF, request inspection, or protecting a local web service."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "start | stop | status | log | block_ip | unblock_ip"},
                "value": {"type": "STRING", "description": "For start: 'upstream_port:listen_port' e.g. '8000:9090' (do not use 8080 \u2014 that's Pragon's own UI port). For block_ip/unblock_ip: an IP address. For log: line count."}
            },
            "required": ["action"]
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, any web-based task. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser": {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url": {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query": {"type": "STRING", "description": "Search query for search action"},
                "engine": {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector": {"type": "STRING", "description": "CSS selector for click/type"},
                "text": {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction": {"type": "STRING", "description": "up | down for scroll"},
                "amount": {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key": {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path": {"type": "STRING", "description": "Save path for screenshot"},
                "incognito": {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path": {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name": {"type": "STRING", "description": "New name for rename"},
                "content": {"type": "STRING", "description": "Content for create_file/write"},
                "name": {"type": "STRING", "description": "File name to search for"},
                "extension": {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count": {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path": {"type": "STRING", "description": "Image path for wallpaper"},
                "url": {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode": {"type": "STRING", "description": "by_type or by_date for organize"},
                "task": {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "compass",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language": {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path": {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code": {"type": "STRING", "description": "Raw code string for explain"},
                "args": {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout": {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "pragon_builder",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description": {"type": "STRING", "description": "What the project should do"},
                "language": {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout": {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text": {"type": "STRING", "description": "Text to type or paste"},
                "x": {"type": "INTEGER", "description": "X coordinate"},
                "y": {"type": "INTEGER", "description": "Y coordinate"},
                "keys": {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key": {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction": {"type": "STRING", "description": "up | down | left | right"},
                "amount": {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds": {"type": "NUMBER", "description": "Seconds to wait"},
                "title": {"type": "STRING", "description": "Window title for focus_window"},
                "description": {"type": "STRING", "description": "Element description for screen_find/screen_click"},
                "type": {"type": "STRING", "description": "Data type for random_data"},
                "field": {"type": "STRING", "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path": {"type": "STRING", "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use browser_control or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform": {"type": "STRING", "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING", "description": "Game name (partial match supported)"},
                "app_id": {"type": "STRING", "description": "Steam AppID for install (optional)"},
                "hour": {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute": {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin": {"type": "STRING", "description": "Departure city or airport code"},
                "destination": {"type": "STRING", "description": "Arrival city or airport code"},
                "date": {"type": "STRING", "description": "Departure date (any format)"},
                "return_date": {"type": "STRING", "description": "Return date for round trips"},
                "passengers": {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin": {"type": "STRING", "description": "economy | premium | business | first"},
                "save": {"type": "BOOLEAN", "description": "Save results to Library"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width": {"type": "INTEGER", "description": "Target width for image resize"},
            "height": {"type": "INTEGER", "description": "Target height for image resize"},
            "scale": {"type": "NUMBER", "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality": {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start": {"type": "STRING", "description": "Start time for trim: seconds or HH:MM:SS"},
            "end": {"type": "STRING", "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING", "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column": {"type": "STRING", "description": "Column name for CSV filter/sort"},
            "value": {"type": "STRING", "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING", "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save": {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "file_generator",
        "description": (
            "Generates a brand-new file from a natural-language description and saves it "
            "to disk — the counterpart to file_processor, which only handles files the user "
            "already has. Use this whenever the user asks to create, write, draft, generate, "
            "make, build, compose, or design a document, spreadsheet, presentation, image, "
            "code file, or any other file that doesn't exist yet. "
            "Supports Word docs, PDFs, plain text/Markdown, JSON/XML, CSV, Excel spreadsheets, "
            "PowerPoint decks, images (AI-generated), and source code in any language. "
            "ALWAYS call this tool to actually produce the file — never just describe what "
            "the file would contain."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "file_type": {
                    "type": "STRING",
                    "description": "docx | pdf | txt | md | json | xml | csv | xlsx | pptx | image | code | auto (default: auto — inferred from the description)"
                },
                "description": {
                    "type": "STRING",
                    "description": "Full, detailed description of what the file should contain — pass through everything the user said about it, the more detail the better the result."
                },
                "file_name": {
                    "type": "STRING",
                    "description": "Base file name without extension (optional — a sensible one is generated if omitted)"
                },
                "save_location": {
                    "type": "STRING",
                    "description": "desktop | downloads | documents | library | home | an explicit folder path (default: desktop)"
                },
                "language": {
                    "type": "STRING",
                    "description": "Programming language, only used when file_type is 'code' or is inferred as code (e.g. 'python', 'javascript')"
                }
            },
            "required": ["description"]
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key": {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "nightly_recap",
        "description": (
            "Triggers the nightly recap flow. Call this when the user says goodnight, "
            "is going to sleep, or wants to wrap up the day. Recaps today's activity, "
            "then asks the user for tomorrow's goals and general to-dos. "
            "Runs in the background and speaks to the user directly -- just call it and stop."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "github_status",
        "description": (
            "Checks GitHub: unread notifications/mentions/review requests, open PRs for a repo, "
            "or the latest commit + CI status for a repo. Requires GITHUB_TOKEN to be configured; "
            "if not configured, says so."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "mode": {"type": "STRING", "description": "notifications | pr_status | repo_status (default: notifications)"},
                "owner": {"type": "STRING", "description": "Repo owner/org, required for pr_status/repo_status"},
                "repo": {"type": "STRING", "description": "Repo name, required for pr_status/repo_status"},
            },
            "required": []
        }
    },
    {
        "name": "calendar_check",
        "description": (
            "Reads the user's Google Calendar. Use for questions about today's schedule "
            "or upcoming events over the next several days. Requires one-time Google OAuth "
            "setup (credentials.json); if not configured, says so."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "range": {"type": "STRING", "description": "today | upcoming (default: today)"},
                "days": {"type": "INTEGER", "description": "Number of days ahead for 'upcoming' (default: 7)"},
            },
            "required": []
        }
    },
    {
        "name": "n8n_workflow",
        "description": (
            "Triggers an n8n automation workflow for cross-app actions: sending Gmail, sending "
            "WhatsApp (via Twilio), creating a Notion/Todoist task, or any custom webhook. "
            "Requires a running n8n instance. Email/WhatsApp sends ask the user to confirm first."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "workflow": {"type": "STRING", "description": "send_gmail | send_whatsapp | create_task | custom"},
                "to": {"type": "STRING", "description": "Recipient email or phone number (send_gmail/send_whatsapp)"},
                "subject": {"type": "STRING", "description": "Email subject (send_gmail)"},
                "message": {"type": "STRING", "description": "Message/body text (send_gmail/send_whatsapp)"},
                "title": {"type": "STRING", "description": "Task title (create_task)"},
                "due_date": {"type": "STRING", "description": "Task due date (create_task)"},
                "notes": {"type": "STRING", "description": "Task notes (create_task)"},
                "workflow_name": {"type": "STRING", "description": "Custom webhook name (custom)"},
                "data": {"type": "OBJECT", "description": "Arbitrary JSON payload (custom)"},
            },
            "required": ["workflow"]
        }
    },
    {
        "name": "crew_task",
        "description": (
            "For genuinely complex, multi-step requests that benefit from planning before acting "
            "(e.g. 'research X and draft a summary email', 'plan out how I should approach Y'). "
            "Runs a local Planner -> Executor -> Reviewer agent pipeline (via Ollama) and returns a "
            "reviewed final result. Slower than normal tools -- do not use for simple one-step requests."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {"type": "STRING", "description": "The complex task to plan and execute"},
                "context": {"type": "STRING", "description": "Any extra context to give the agents"},
            },
            "required": ["task"]
        }
    },
    {
        "name": "action_agent",
        "description": (
            "Handles India-centric life-task automations by opening the right site and walking "
            "the user through it: booking a train (IRCTC), flight, cab (Uber), hotel; ordering food "
            "(Swiggy/Zomato); paying a bill; mobile recharge; shopping on Amazon/Flipkart; video editing. "
            "Call with the user's original phrasing so intent detection can classify it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "The user's original request, verbatim"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "macro_action",
        "description": (
            "A.C.T.I.O.N — records and replays real mouse-click / keystroke macros on this "
            "computer. Use mode='start' to begin recording (captures clicks and key presses "
            "until told to stop, or the user presses ESC), mode='stop' to stop recording and "
            "save it under 'name', mode='play' to replay a saved macro (optionally at a "
            "different 'speed' multiplier), mode='stop_play' to abort a macro currently "
            "playing, mode='list' to list saved macros, mode='delete' to remove one, and "
            "mode='status' to check whether it's currently recording or playing. "
            "Playback takes over the real mouse and keyboard, so always confirm with the "
            "user before calling mode='play'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "mode": {"type": "STRING", "description": "start | stop | play | stop_play | list | delete | status"},
                "name": {"type": "STRING", "description": "Macro name (required for stop/play/delete)"},
                "speed": {"type": "NUMBER", "description": "Playback speed multiplier, default 1.0 (play only)"},
            },
            "required": ["mode"]
        }
    },
    {
        "name": "library_ask",
        "description": (
            "Answers a question using files the user has added to the P.R.A.G.O.N "
            "Library — PDFs, Word docs, spreadsheets, images, code, zips, etc, "
            "uploaded via drag-and-drop, the file picker, or Library import. Use "
            "this whenever the user asks about the contents of a file they've "
            "shared, or references 'the document', 'that PDF', 'the file I sent', "
            "'what I just uploaded', or similar. Always call this instead of "
            "guessing at file contents you can't actually see."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "The user's question about the uploaded file(s)"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "knowledge_base",
        "description": (
            "Searches Pragon's local offline knowledge base (the standalone 'ragsystem' "
            "service, running via Ollama + ChromaDB — no API key needed). Complements "
            "library_ask: try this too if library_ask comes up short, and prefer this "
            "one for audio/video content (it transcribes with timestamps) and images "
            "(it runs OCR plus a vision model), since those are its strengths. Also use "
            "it for anything the user explicitly wants answered fully offline/locally. "
            "Use action 'query' to ask a question, 'ingest' to add an already-uploaded "
            "file so future questions can find it, or 'list' to see what's stored."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "query | ingest | list (default: query)"},
                "question": {"type": "STRING", "description": "The question to ask (query action)"},
                "file_path": {"type": "STRING", "description": "Path to the file to ingest (ingest action). Leave empty to use the currently uploaded file."},
            },
            "required": []
        }
    },
]

# ── GHOST tool-calling support ────────────────────────────────────────────
# GHOST runs on a local Ollama model instead of Gemini, so TOOL_DECLARATIONS
# (Gemini's schema: "OBJECT"/"STRING"/"ARRAY"/"BOOLEAN") needs converting to
# Ollama's OpenAI-style schema ("object"/"string"/"array"/"boolean") before
# it can be passed to /api/chat as `tools=[...]`.
#
# Not every local model actually honors tool_calls reliably -- only pick a
# GHOST model from this list (or verify with `ollama show <model>` that
# "tools" appears under Capabilities):
GHOST_TOOL_CAPABLE_MODELS = [
    "qwen3", "qwen2.5", "qwen2.5-coder",           # most reliable locally
    "llama3.3", "llama3.2", "llama3.1",
    "llama3-groq-tool-use",                          # tool-use finetune, best BFCL score
    "mistral", "mistral-nemo", "mixtral",
    "command-r", "command-r-plus",
    "firefunction-v2", "hermes3", "hermes4",
    "granite3-dense", "granite3.2",
    "nemotron-mini",
]
# NOTE: gemma3 (GHOST's historical default) does NOT support tool calling.

_GEMINI_TO_JSON_TYPE = {
    "OBJECT": "object", "STRING": "string", "ARRAY": "array",
    "BOOLEAN": "boolean", "NUMBER": "number", "INTEGER": "integer",
}

def _convert_schema_types(node):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, str):
                out[k] = _GEMINI_TO_JSON_TYPE.get(v.upper(), v.lower())
            else:
                out[k] = _convert_schema_types(v)
        return out
    if isinstance(node, list):
        return [_convert_schema_types(v) for v in node]
    return node

def gemini_tools_to_ollama(declarations):
    """Convert TOOL_DECLARATIONS (Gemini schema) into Ollama's tools=[...] format."""
    tools = []
    for d in declarations:
        tools.append({
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d.get("description", ""),
                "parameters": _convert_schema_types(d.get("parameters", {"type": "OBJECT", "properties": {}})),
            },
        })
    return tools

GHOST_TOOLS = gemini_tools_to_ollama(TOOL_DECLARATIONS)

GHOST_AGENT_SYS = (
    "You are GHOST, an AI engine inside P.R.A.G.O.N. with real access to tools "
    "that control this computer. When the user asks you to do something actionable "
    "(open an app, search the web, check the weather, control the browser/files/desktop, "
    "manage the firewall, etc.), you MUST call the matching tool -- never claim you did "
    "something without calling the tool for it. After a tool returns a result, summarize "
    "it for the user in one or two sentences."
)

# --- Plugin system ---


class JarvisLive:

    def __init__(self, ui: PragonUI):
        self.ui = ui
        self.session = None
        self.audio_in_queue = None
        self.out_queue = None
        self._loop = None
        self._is_speaking = False
        self._speaking_lock = threading.Lock()
        self._phone_active = False # True while phone mic is streaming; pauses PC mic
        self._pending_vision = None # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active = False # True if camera was opened for vision auto-close after response
        self._vision_close_pending = False # True after vision injected; next turn_complete closes camera
        self._vision_last_time = 0.0 # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy = False # True while a vision capture/inject cycle is in flight
        self._interrupted = False # True while draining audio after user interrupt
        self.ui.on_text_command = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_whatsapp_connect_clicked = self._whatsapp_connect
        self.ui.on_whatsapp_set_model = self._whatsapp_set_model
        # WhatsApp replies default to the same env-var fallbacks as before;
        # the Settings panel's Host/Model toggle (via on_whatsapp_set_model)
        # overrides these live, without needing a reconnect.
        self._whatsapp_host = os.environ.get("PRAGON_WHATSUP_HOST", "http://localhost:11434")
        self._whatsapp_model = os.environ.get("PRAGON_WHATSUP_MODEL", "qwen3:8b")
        self.ui.on_create_shortcut_clicked = self._create_desktop_shortcut
        self.ui.on_toggle_autostart_clicked = self._toggle_autostart
        # Integration tab — Settings > Integration. Dock any .py plugin file
        # straight from the UI; toggling/uploading never requires a restart.
        self.ui.on_plugin_list = self._plugin_list
        self.ui.on_plugin_toggle = self._plugin_toggle
        self.ui.on_plugin_upload = self._plugin_upload
        self.ui.on_plugin_delete = self._plugin_delete
        self._whatsapp_bridge = None

        from features.agentic_feature.background_monitor import BackgroundMonitor
        self._bg_monitor = BackgroundMonitor(
            on_alert=lambda msg: self.ui._broadcast({"type": "bg_monitor_alert", "message": msg})
        )
        self._bg_monitor.start()
        self.ui.on_bg_monitor_set_topics = self._bg_monitor.set_topics
        self.ui.on_bg_monitor_get_topics = self._bg_monitor.get_topics
        self.ui.on_bg_monitor_check_now = lambda: threading.Thread(
            target=self._bg_monitor.check_now, daemon=True).start()

        self._clipboard_watch_enabled = False
        self._clipboard_last_text = ""
        self.ui.on_toggle_clipboard_watch = self._toggle_clipboard_watch
        threading.Thread(target=self._clipboard_watch_loop, daemon=True).start()
        self.ui.on_interrupt = self.interrupt

        # A.C.T.I.O.N — Settings > Action panel (direct calls, no LLM in the loop)
        self.ui.on_macro_start = lambda: ui_start_recording(player=self.ui)
        self.ui.on_macro_stop = lambda: ui_stop_and_save(player=self.ui)
        self.ui.on_macro_list = list_macro_names
        self.ui.on_macro_rename = lambda old, new: ui_rename(old, new, player=self.ui)
        self.ui.on_macro_delete = lambda name: ui_delete(name, player=self.ui)
        self.ui.on_macro_play = lambda name: ui_play(name, player=self.ui)
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard = None
        self._phone_cmd_queue = asyncio.Queue()
        self._phone_audio_queue = asyncio.Queue(maxsize=200)
        self._briefing_sent = False # morning briefing fires once per process
        self._sys_monitor = SystemMonitor() # persistent cooldown state
        self._cpu_balancer = CPUBalancer() # option: auto-balances CPU across cores
        self._confirm_gate = ConfirmGate(broadcast_fn=self.ui.broadcast) # UI-issued confirmation, not model-issued
        self.ui.on_confirm_response = self._confirm_gate.resolve
        self.ui.on_list_audio_devices = self._on_list_audio_devices
        self.ui.on_set_audio_device = self._on_set_audio_device
        audio_devices.warm_cache_async() # kick off the (slow) device query now, off the hot path

        # -- wake word ("Hey Jarvis") — ported from Mark-LIII ----------------
        # Off by default (opt-in, matches Mark-LIII's stance since it needs the
        # optional openwakeword package). The topbar "WAKE WORD" button flips
        # this via self.ui.on_wake_word_toggle; state survives restarts via
        # consciousness.config_manager.
        self._wake_detector = wake_word.WakeWordDetector(
            on_detect=self._on_wake_word_detected,
            logger=lambda msg: self.ui.write_log(f"SYS: {msg}"),
        )
        self.ui.on_wake_word_toggle = self._handle_wake_word_toggle
        self.ui.wake_word_enabled = get_wake_word_enabled()
        if self.ui.wake_word_enabled:
            # Fire-and-forget: don't block startup on model install/loading.
            threading.Thread(target=self._start_wake_word, daemon=True).start()

        self._proactive = ProactiveEngine()
        self._last_user_speech = time.monotonic() # updated on every user utterance

        # -- plugin system (ported from Mark-LI) -----------------------------
        # Drop a .py file into plugins/ and Pragon learns it on next launch —
        # no core file needs to change. Discovery never raises: a broken
        # plugin shows up as rejected while everything else keeps working.
        self._plugin_registry = discover_plugins(
            BASE_DIR / "plugins",
            core_tool_names={d["name"] for d in TOOL_DECLARATIONS},
            logger=lambda msg: self.ui.write_log(f"SYS: {msg}"),
        )

        # -- integration system (Settings > Integration, "Full Projects") ----
        # Docks a WHOLE uploaded .zip project (any language) into its own
        # isolated venv/node_modules and runs it as a subprocess Pragon can
        # start/stop/query — unlike a plugin, it never runs in-process.
        from pragoncore.integration_manager import IntegrationManager
        self._integration_manager = IntegrationManager(
            BASE_DIR / "integrations",
            logger=lambda msg: self.ui.write_log(f"SYS: {msg}"),
        )
        self.ui.on_integration_list = self._integration_manager.list_for_ui
        self.ui.on_integration_upload = self._integration_upload
        self.ui.on_integration_start = self._integration_manager.start
        self.ui.on_integration_stop = self._integration_manager.stop
        self.ui.on_integration_delete = self._integration_manager.delete
        self.ui.on_integration_logs = self._integration_manager.logs

        # Enhanced Live audio (ported from Mark-LI): affective dialog (Pragon
        # hears tone/emotion and adapts its voice) + proactive audio (stays
        # silent when speech isn't addressed to it). Auto-disabled for the
        # rest of the process if the Live API rejects them.
        self._enhanced_live = True

        # -- agentic/utils integration --------------------------------------
        # Bridges a blocking listen_fn() call (used by agentic modules written
        # for a synchronous voice loop) onto the async Gemini Live session:
        # the next user transcript is pushed onto this queue instead of (or
        # in addition to) being logged normally.
        self._blocking_input_queue = queue.Queue()
        self._awaiting_blocking_input = False

        self._confirmation_gate = ConfirmationGate(
            speak_fn=self.speak, listen_fn=self._blocking_listen
        )
        self._nightly_recap = NightlyRecap(
            config={"user_name": "RR"},
            speak_fn=self.speak, listen_fn=self._blocking_listen
        )
        self._deadline_manager = DeadlineManager(db=sqlite_mem, speak_fn=self.speak)
        self._morning_brief = MorningBrief(config={"user_name": "RR"})
        self._action_agent = ActionAgent(
            speak_fn=self.speak, listen_fn=self._blocking_listen,
            confirm_gate=self._confirmation_gate
        )

        # G.H.O.S.T bridge -- gives GHOST (local Ollama model) the same
        # feature/ultifeature/agentic_feature tool access JARVIS has, via
        # /ghost/act. See _ghost_agent_turn / _ghost_execute_tool below.
        self.ui.set_ghost_processor(self._ghost_agent_turn)

        # F.R.I.D.A.Y bridge -- deliberately NOT wired to any tool/action
        # dispatch (no open_app, browser_control, firewall_control, etc.).
        # FRIDAY is a pure RAG/generation engine: plain Ollama chat, grounded
        # by whatever ragRetrieve() already folded into the incoming text on
        # the frontend. Also backs POST /api/generate-file's "friday" branch
        # (the chat's "create me a <file>" auto-detect), which previously
        # raised "backend not ready" because nothing registered this.
        self.ui.set_friday_processor(self._friday_processor)

    # ── PRAGON WhatsUp (WhatsApp bot feature, replies via FRIDAY) ────────
    def _whatsapp_connect(self):
        """Called from the UI's 'whatsapp_connect' ws handler when the user
        clicks 'Connect WhatsApp'. Starts the Node sidecar (if not already
        running) and wires its events to UI broadcasts + FRIDAY replies."""
        from pragon_whatsapp import WhatsUpBridge

        if self._whatsapp_bridge and self._whatsapp_bridge.is_running():
            # Already running -- if we have a cached QR/ready state the UI
            # will pick it up from the next event; nothing else to do.
            return True, None

        def on_qr(data_uri):
            self.ui._broadcast({"type": "whatsapp_pairing", "ok": True, "qr": data_uri})

        def on_ready():
            self.ui._broadcast({"type": "whatsapp_status", "connected": True})

        def on_disconnected(reason):
            self.ui._broadcast({"type": "whatsapp_status", "connected": False, "reason": reason})

        def on_error(message):
            self.ui._broadcast({"type": "whatsapp_pairing", "ok": False, "error": message})

        def on_message(frm, text):
            self._whatsapp_handle_message(frm, text)

        bridge = WhatsUpBridge(
            on_qr=on_qr, on_ready=on_ready, on_message=on_message,
            on_disconnected=on_disconnected, on_error=on_error,
        )
        ok, err = bridge.start()
        if ok:
            self._whatsapp_bridge = bridge
        return ok, err

    def _whatsapp_set_model(self, host: str | None, model: str):
        """Called from the UI's Settings > PRAGON WhatsUp panel whenever the
        person toggles the Host/Model fields or picks a Quick Model pill.
        Takes effect on the next incoming message -- no reconnect needed."""
        if host:
            self._whatsapp_host = host
        self._whatsapp_model = model
        print(f"[WhatsUp] model toggle -> host={self._whatsapp_host!r} model={model!r}", flush=True)

    def _whatsapp_handle_message(self, frm: str, text: str):
        """Runs on the bridge's reader thread: forward the incoming WhatsApp
        message straight to FRIDAY (RAG-only, no tool access -- same
        restriction FRIDAY has everywhere else) and send her reply back."""
        print(f"[WhatsUp] got message from {frm!r}: {text!r}", flush=True)
        try:
            # Host/model come from the Settings > PRAGON WhatsUp panel (sent
            # over the whatsapp_set_model ws message and cached on self);
            # they default to the PRAGON_WHATSUP_HOST / PRAGON_WHATSUP_MODEL
            # env vars (or qwen3:8b on localhost) until the person toggles
            # them at least once.
            reply = self._friday_processor(text, self._whatsapp_host, self._whatsapp_model)
            print(f"[WhatsUp] FRIDAY reply: {reply!r}", flush=True)
        except Exception as e:
            reply = f"(PRAGON WhatsUp: FRIDAY couldn't generate a reply -- {e})"
            print(f"[WhatsUp] ERROR generating reply: {e}", flush=True)

        if self._whatsapp_bridge:
            sent = self._whatsapp_bridge.send(frm, reply)
            print(f"[WhatsUp] send() returned {sent}", flush=True)
        else:
            print("[WhatsUp] ERROR: no bridge instance to send through", flush=True)

    # ── Desktop shortcut (native, avoids the .vbs-gets-blocked problem) ──
    def _real_desktop_path(self) -> str:
        """Resolve the actual Desktop folder, honoring OneDrive Known
        Folder Move redirection (~\\OneDrive\\Desktop) instead of assuming
        the classic ~\\Desktop -- that assumption silently writes the
        shortcut somewhere the user's Explorer window never shows."""
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            value, _ = winreg.QueryValueEx(key, "Desktop")
            path = os.path.expandvars(value)
            if os.path.isdir(path):
                return path
        except Exception:
            pass
        # Fallback: classic path, if the registry lookup failed for any reason.
        return os.path.join(os.path.expanduser("~"), "Desktop")

    def _create_desktop_shortcut(self):
        """Creates the P.R.A.G.O.N desktop shortcut directly via COM
        automation (comtypes), since standalone downloaded .vbs/.exe files
        get flagged by Windows Smart App Control. Returns (ok, message)."""
        if sys.platform != "win32":
            return False, "Desktop shortcuts are only supported on Windows."

        try:
            import comtypes.client
        except ImportError:
            return False, "comtypes isn't installed. Run: pip install comtypes"

        try:
            project_dir = os.path.dirname(os.path.abspath(__file__))
            launcher = os.path.join(project_dir, "Launch_Pragon.pyw")
            icon_path = os.path.join(project_dir, "assets", "pragon_icon.ico")

            # sys.executable is the currently-running interpreter's exact
            # path -- far more reliable than searching PATH/registry for
            # pythonw.exe. Swap python.exe -> pythonw.exe for a console-free
            # launch; fall back to the running interpreter itself if this
            # was already started with pythonw.
            exe_dir = os.path.dirname(sys.executable)
            pythonw = os.path.join(exe_dir, "pythonw.exe")
            if not os.path.isfile(pythonw):
                pythonw = sys.executable  # already pythonw, or no windowed variant

            desktop = self._real_desktop_path()
            shortcut_path = os.path.join(desktop, "P.R.A.G.O.N.lnk")

            shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
            shortcut = shell.CreateShortCut(shortcut_path)
            shortcut.TargetPath = pythonw
            shortcut.Arguments = f'"{launcher}"'
            shortcut.WorkingDirectory = project_dir
            if os.path.isfile(icon_path):
                shortcut.IconLocation = f"{icon_path},0"
            shortcut.WindowStyle = 1
            shortcut.Description = "P.R.A.G.O.N - We build. You grow."
            shortcut.Save()

            return True, f"Shortcut created at {shortcut_path}"
        except Exception as e:
            return False, f"Couldn't create shortcut: {e}"

    # ── Auto-Start on Boot (ported from Mark-L, Windows Registry based) ──
    _AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _AUTOSTART_NAME = "PRAGON_AI"

    def _check_autostart(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._AUTOSTART_KEY, 0, winreg.KEY_READ)
            try:
                winreg.QueryValueEx(key, self._AUTOSTART_NAME)
                return True
            except FileNotFoundError:
                return False
            finally:
                winreg.CloseKey(key)
        except Exception:
            return False

    def _rescan_plugins(self):
        """Re-run discover_plugins() so a just-docked or just-removed .py file is
        picked up immediately — no restart needed. Swapping self._plugin_registry
        is safe: it's only read at Live-config build time and in _execute_tool's
        fallback branch, both of which just grab whatever registry is current."""
        self._plugin_registry = discover_plugins(
            BASE_DIR / "plugins",
            core_tool_names={d["name"] for d in TOOL_DECLARATIONS},
            logger=lambda msg: self.ui.write_log(f"SYS: {msg}"),
        )

    def _plugin_list(self) -> list[dict]:
        """Settings > Integration — full list (valid + rejected) for the UI table."""
        return self._plugin_registry.list_for_ui()

    def _plugin_toggle(self, name: str, enabled: bool) -> str:
        """Settings > Integration — flip a docked plugin's enabled switch."""
        save_plugin_enabled(name, bool(enabled))
        return f"'{name}' {'enabled' if enabled else 'disabled'}."

    def _plugin_upload(self, filename: str, content_b64: str) -> tuple[bool, str]:
        """Settings > Integration — dock a .py file uploaded from the browser.
        content_b64 is the raw file bytes, base64-encoded, sent over the WS.

        SAFETY NOTE: discover_plugins() has to import (exec) a .py file to even
        read its PLUGIN dict, so the file's top-level code already ran once by
        the time we get a result here — there's no way to inspect a plugin's
        contract without running it first, short of a much heavier sandboxing
        layer this module doesn't have. What we CAN do, and do here, is make
        sure that import doesn't also hand the plugin a live, voice-callable
        tool: every freshly uploaded plugin is force-disabled immediately after
        the scan, so run() refuses to fire (see PluginRegistry.run's
        get_plugin_enabled check) until a human reviews it in Settings >
        Integration and flips it on themselves. Pre-existing/shipped plugins
        are unaffected — only the file that was JUST uploaded gets disabled.
        """
        try:
            data = base64.b64decode(content_b64)
        except Exception as e:
            return False, f"Bad file data: {e}"
        ok, msg = save_uploaded_plugin(BASE_DIR / "plugins", filename, data)
        if ok:
            saved_filename = msg
            self._rescan_plugins()
            rec = self._plugin_registry.find_by_file(saved_filename)
            if rec is not None and rec.valid:
                save_plugin_enabled(rec.name, False)
                msg = (f"'{rec.name}' docked but left DISABLED for your review — "
                       f"it ran once during validation, same as any Python import. "
                       f"Check what it does, then enable it yourself in "
                       f"Settings > Integration if you trust it.")
            elif rec is not None:
                msg = f"'{saved_filename}' was rejected: {rec.error}"
            else:
                msg = f"'{saved_filename}' docked, but could not be found after rescan."
        return ok, msg

    def _plugin_delete(self, filename: str) -> str:
        """Settings > Integration — undock a plugin file."""
        ok, msg = delete_plugin_file(BASE_DIR / "plugins", filename)
        if ok:
            self._rescan_plugins()
        return msg

    def _integration_upload(self, filename: str, content_b64: str) -> tuple[bool, str]:
        """Settings > Integration ("Full Projects") — dock an uploaded .zip
        project. Decoding is the only thing done here; everything else
        (extraction, runtime detection, isolated install) is IntegrationManager's job."""
        try:
            data = base64.b64decode(content_b64)
        except Exception as e:
            return False, f"Bad file data: {e}"
        return self._integration_manager.install_zip(filename, data)

    def _toggle_autostart(self):
        """Enable/disable launching Pragon automatically at Windows login.
        Returns (ok, enabled, message)."""
        if sys.platform != "win32":
            return False, False, "Auto-start is only supported on Windows."

        try:
            import winreg
            currently_on = self._check_autostart()
            project_dir = os.path.dirname(os.path.abspath(__file__))
            launcher = os.path.join(project_dir, "Launch_Pragon.pyw")

            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._AUTOSTART_KEY,
                                  0, winreg.KEY_ALL_ACCESS)
            try:
                if currently_on:
                    winreg.DeleteValue(key, self._AUTOSTART_NAME)
                else:
                    exe_dir = os.path.dirname(sys.executable)
                    pythonw = os.path.join(exe_dir, "pythonw.exe")
                    if not os.path.isfile(pythonw):
                        pythonw = sys.executable
                    winreg.SetValueEx(key, self._AUTOSTART_NAME, 0, winreg.REG_SZ,
                                       f'"{pythonw}" "{launcher}"')
            finally:
                winreg.CloseKey(key)

            enabled = not currently_on
            return True, enabled, f"Auto-start {'enabled' if enabled else 'disabled'}."
        except Exception as e:
            return False, self._check_autostart(), f"Couldn't toggle auto-start: {e}"

    # ── Clipboard Intelligence (opt-in; off by default for privacy) ──────
    def _toggle_clipboard_watch(self):
        self._clipboard_watch_enabled = not self._clipboard_watch_enabled
        if self._clipboard_watch_enabled:
            try:
                import pyperclip
                self._clipboard_last_text = pyperclip.paste() or ""
            except Exception:
                self._clipboard_last_text = ""
        return self._clipboard_watch_enabled

    def _clipboard_watch_loop(self):
        """Polls the clipboard every ~1.2s while watching is enabled and
        broadcasts new copies to the UI, which shows the floating panel.
        Off by default -- clipboard contents can be sensitive, so this
        only runs at all once the user explicitly flips it on."""
        try:
            import pyperclip
        except ImportError:
            return  # pyperclip not installed -- feature quietly unavailable

        while True:
            time.sleep(1.2)
            if not getattr(self, "_clipboard_watch_enabled", False):
                continue
            try:
                current = pyperclip.paste() or ""
            except Exception:
                continue
            if current and current != self._clipboard_last_text and current.strip():
                self._clipboard_last_text = current
                # Cap what we broadcast -- long clipboard contents (e.g. a
                # pasted file) aren't useful in a floating snippet panel.
                snippet = current if len(current) <= 2000 else current[:2000] + "…"
                self.ui._broadcast({"type": "clipboard_update", "text": snippet})

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key = self._dashboard.new_key()
        url = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return

        # The web UI's attach ("+") button uploads binary files (pdf/docx/xlsx/
        # zip/etc) to disk and sends a marker line like:
        #   "📎 File uploaded: report.pdf (saved at /path/to/uploads/xxx_report.pdf)"
        # Ingest it into the Library in the background the moment it arrives,
        # so it's already searchable by the time the user (or the model, via
        # the library_ask tool) asks a question about it.
        m = _UPLOAD_MARKER_RE.search(text)
        if m:
            filename, filepath = m.group(1).strip(), m.group(2).strip()

            def _ingest():
                try:
                    raw = Path(filepath).read_bytes()
                    library_store.add_item(raw, filename)
                    log.log("LIBRARY", f"Ingested '{filename}' from {filepath}")
                    self.ui.write_log(f"SYS: '{filename}' added to Library — ready to answer questions about it.")
                except Exception as e:
                    log.log("LIBRARY", f"Ingest failed for '{filename}': {e}")
                    self.ui.write_log(f"ERR: Could not add '{filename}' to Library: {e}")

                # Also push it to the offline ragsystem knowledge base, if that
                # service happens to be running. Best-effort: silently skipped
                # if it's not up, or if the file type isn't one it supports.
                r = _rag_upload(filepath, filename)
                if r:
                    log.log("RAG", f"Ingested '{filename}' into knowledge base ({r.get('num_chunks', '?')} chunks)")

            threading.Thread(target=_ingest, daemon=True).start()

        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _blocking_listen(self, timeout: float = 15.0) -> str | None:
        """
        Synchronous listen_fn for agentic modules (ConfirmationGate, NightlyRecap,
        ActionAgent) that expect a blocking Q&A loop. MUST be called from a worker
        thread (e.g. via loop.run_in_executor or a daemon Thread), never from the
        asyncio event loop itself, since it blocks.

        Captures the user's next full transcript from the live session and returns
        it as plain text, or None on timeout/no response.
        """
        while not self._blocking_input_queue.empty():
            try:
                self._blocking_input_queue.get_nowait()
            except queue.Empty:
                break
        self._awaiting_blocking_input = True
        try:
            return self._blocking_input_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self._awaiting_blocking_input = False

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        memory = load_memory()
        mem_str = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        parts = [time_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{
                "function_declarations":
                    TOOL_DECLARATIONS + self._plugin_registry.get_tool_declarations()
                    + self._integration_manager.get_tool_declarations()
            }],
            session_resumption=types.SessionResumptionConfig(),
            # Sliding-window compression (ported from Mark-LI): the session
            # never dies from a full context window — Pragon can stay in one
            # conversation for hours.
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Charon"
                    )
                )
            ),
        )
        if self._enhanced_live:
            # Affective dialog: Pragon hears tone/emotion and adapts its voice.
            # Proactive audio: Pragon stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            cfg["enable_affective_dialog"] = True
            cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)
        return types.LiveConnectConfig(**cfg)

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        log.log("AGENTIC", f"{name} {args}")
        self.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key = args.get("key", "")
            value = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                unified_mem.remember(category, key, value)
                print(f"[Memory] save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop = asyncio.get_event_loop()
        result = "Done."

        if name in INSTANT_ACK_TOOLS:
            self.speak(_instant_ack_prompt(name))

        try:
            if name == "open_app":
                r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "custom_forge":
                r = await loop.run_in_executor(None, lambda: custom_forge(parameters=args, player=self.ui))
                result = r or "C.U.S.T.O.M FORGE opened."

            elif name == "pragon_compass":
                r = await loop.run_in_executor(None, lambda: pragon_compass(parameters=args, player=self.ui))
                result = r or "P.R.A.G.O.N COMPASS opened."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper_action(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "pragon_agent":
                r = await loop.run_in_executor(None, lambda: pragon_agent_action(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "agent_task":
                r = await loop.run_in_executor(
                    None, lambda: agent_task_action(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                def _gated_send():
                    approved = self._confirmation_gate.ask_for_whatsapp(
                        args.get("receiver", "the recipient"),
                        args.get("message_text", "")
                    )
                    if not approved:
                        return "Cancelled -- user did not confirm sending the message."
                    return send_message(parameters=args, response=None, player=self.ui, session_memory=None)
                r = await loop.run_in_executor(None, _gated_send)
                result = r or f"Message sent to {args.get('receiver')}."
                sqlite_mem.log("send_message", str(result)[:200])
                unified_mem.log_task("send_message", str(result)[:200])

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "screen_process":
                if args.get("mode", "gemini").lower() == "local":
                    # Offline/private fallback path -- analyzed locally via Ollama LLaVA,
                    # no image sent to Gemini. Use when the user wants privacy/offline
                    # analysis, or as a fallback if the Gemini vision path is unavailable.
                    context = args.get("angle", "general")
                    r = await loop.run_in_executor(
                        None, lambda: screen_vision_local.capture_and_analyze(context)
                    )
                    result = r or "Local screen analysis unavailable."
                    if not self.ui.muted:
                        self.ui.set_state("LISTENING")
                    return types.FunctionResponse(id=fc.id, name=name, response={"result": result})

                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0 # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy = True
                    self._vision_last_time = _now
                    angle = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                        f"Immediately say ONE natural sentence in the user's language "
                        f"(e.g. 'Looking at your {_stall} now, sir'). "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui, confirm_gate=self._confirm_gate))
                result = r or "Done."

            elif name == "firewall_control":
                r = await loop.run_in_executor(None, lambda: firewall_control(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "network_firewall":
                r = await loop.run_in_executor(None, lambda: network_firewall(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "waf_proxy":
                r = await loop.run_in_executor(None, lambda: waf_proxy(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "compass":
                r = await loop.run_in_executor(None, lambda: compass(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "pragon_builder":
                r = await loop.run_in_executor(None, lambda: pragon_builder(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."
                # Mirror results to the on-screen content panel
                _mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "file_generator":
                r = await loop.run_in_executor(
                    None,
                    lambda: file_generator(
                        parameters=args, player=self.ui, speak=self.speak,
                        api_key_fn=_get_api_key, base_dir=BASE_DIR,
                    )
                )
                result = r or "Done."
                sqlite_mem.log("file_generator", str(result)[:200])
                unified_mem.log_task("file_generator", str(result)[:200])

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "cpu_balancer":
                r = await loop.run_in_executor(None, lambda: cpu_balancer_tool(parameters=args))
                result = r or "Done."

            elif name == "undo_action":
                r = await loop.run_in_executor(None, undo_last)
                result = r or "Nothing to undo."

            elif name == "recall_memory":
                query = args.get("query", "")
                hits = await loop.run_in_executor(None, lambda: search_memory(query))
                if not hits:
                    result = f"Nothing found in memory for '{query}'."
                else:
                    result = "; ".join(f"{h['category']}/{h['key']}: {h['value']}" for h in hits)

            elif name == "nightly_recap":
                self._nightly_recap.trigger(sqlite_mem=sqlite_mem)
                result = "Nightly recap started."

            elif name == "github_status":
                mode = args.get("mode", "notifications")
                def _github():
                    if not github_monitor.is_connected:
                        return "GitHub monitoring isn't configured -- set the GITHUB_TOKEN environment variable first."
                    if mode == "pr_status":
                        prs = github_monitor.get_pr_status(args.get("owner", ""), args.get("repo", ""))
                        return "No open PRs." if not prs else "; ".join(
                            f"#{p['number']} {p['title']} by {p['author']}" for p in prs
                        )
                    if mode == "repo_status":
                        return github_monitor.get_repo_status(args.get("owner", ""), args.get("repo", "")).get("summary", "No status available.")
                    alerts = github_monitor.get_urgent_alerts() or github_monitor.get_notifications()
                    if not alerts:
                        return "No pending GitHub notifications."
                    return "; ".join(alerts) if isinstance(alerts[0], str) else str(alerts)
                r = await loop.run_in_executor(None, _github)
                result = r or "Done."

            elif name == "calendar_check":
                def _cal():
                    if not calendar_integration.is_connected:
                        return "Google Calendar isn't connected -- set up credentials.json first."
                    if args.get("range", "today") == "upcoming":
                        events = calendar_integration.get_upcoming_events(days=args.get("days", 7))
                    else:
                        events = calendar_integration.get_today_events()
                    if not events:
                        return "No events found."
                    return "; ".join(f"{e['title']} at {e['time']}" for e in events)
                r = await loop.run_in_executor(None, _cal)
                result = r or "Done."

            elif name == "n8n_workflow":
                workflow = args.get("workflow", "")
                def _n8n():
                    if not n8n.is_connected:
                        return "n8n isn't running -- start it with 'n8n start' first."
                    if workflow == "send_gmail":
                        approved = self._confirmation_gate.ask_for_email(args.get("to", ""), args.get("subject", ""))
                        if not approved:
                            return "Cancelled -- user did not confirm sending the email."
                        ok = n8n.send_gmail(args.get("to", ""), args.get("subject", ""), args.get("message", ""))
                        return "Email sent." if ok else "Failed to send email via n8n."
                    if workflow == "send_whatsapp":
                        approved = self._confirmation_gate.ask_for_whatsapp(args.get("to", ""), args.get("message", ""))
                        if not approved:
                            return "Cancelled -- user did not confirm sending the message."
                        ok = n8n.send_whatsapp(args.get("to", ""), args.get("message", ""))
                        return "WhatsApp message sent." if ok else "Failed to send WhatsApp via n8n."
                    if workflow == "create_task":
                        ok = n8n.create_task(args.get("title", ""), args.get("due_date", ""), args.get("notes", ""))
                        return "Task created." if ok else "Failed to create task via n8n."
                    if workflow == "custom":
                        r = n8n.trigger_custom(args.get("workflow_name", "custom"), args.get("data", {}) or {})
                        return f"Custom workflow triggered: {r}" if r else "Custom workflow failed or returned nothing."
                    return f"Unknown n8n workflow: {workflow}"
                r = await loop.run_in_executor(None, _n8n)
                result = r or "Done."
                sqlite_mem.log("n8n_workflow", f"{workflow}: {str(result)[:150]}")
                unified_mem.log_task("n8n_workflow", f"{workflow}: {str(result)[:150]}")

            elif name == "crew_task":
                r = await loop.run_in_executor(
                    None, lambda: crew.run(args.get("task", ""), args.get("context", ""))
                )
                result = r or "Done."
                sqlite_mem.log("crew_task", str(result)[:200])
                unified_mem.log_task("crew_task", str(result)[:200])

            elif name == "action_agent":
                query = args.get("query", "")
                def _run_action_agent():
                    action = self._action_agent.detect_action_intent(query)
                    if not action:
                        return "I couldn't match that to a specific action -- try rephrasing."
                    return self._action_agent.execute(action, query)
                r = await loop.run_in_executor(None, _run_action_agent)
                result = r or "Done."
                sqlite_mem.log("action_agent", str(result)[:200])
                unified_mem.log_task("action_agent", str(result)[:200])

            elif name == "macro_action":
                mode = (args.get("mode") or "").strip().lower()
                if mode == "play":
                    def _gated_play():
                        macro_name = args.get("name", "this macro")
                        approved = self._confirmation_gate.ask_for_action(
                            f"I'm about to play back the '{macro_name}' macro, "
                            f"which will take over your mouse and keyboard"
                        )
                        if not approved:
                            return "Cancelled -- user did not confirm macro playback."
                        return macro_action(parameters=args, player=self.ui, speak=self.speak)
                    r = await loop.run_in_executor(None, _gated_play)
                else:
                    r = await loop.run_in_executor(None, lambda: macro_action(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."
                sqlite_mem.log("macro_action", f"{mode}: {str(result)[:150]}")
                unified_mem.log_task("macro_action", f"{mode}: {str(result)[:150]}")

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                self.speak("Goodbye, sir.")
                def _shutdown():
                    import time, os
                    time.sleep(1)
                    os._exit(0)
                threading.Thread(target=_shutdown, daemon=True).start()

            elif name == "library_ask":
                query = args.get("query", "")
                r = await loop.run_in_executor(None, lambda: library_store.ask(query))
                answer = r.get("answer", "") if isinstance(r, dict) else str(r)
                sources = r.get("sources", []) if isinstance(r, dict) else []
                result = f"{answer}\n\n(Source: {', '.join(s['name'] for s in sources)})" if sources else answer
                sqlite_mem.log("library_ask", f"{query} -> {str(result)[:150]}")

            elif name == "knowledge_base":
                kb_action = (args.get("action") or "query").strip().lower()
                if kb_action == "query":
                    question = args.get("question", "")
                    r = await loop.run_in_executor(None, lambda: _rag_query(question))
                    result = r or "Done."
                    sqlite_mem.log("knowledge_base", f"{question} -> {str(result)[:150]}")
                elif kb_action == "ingest":
                    fpath = args.get("file_path") or self.ui.current_file
                    if not fpath:
                        result = "No file to ingest -- upload one first or specify file_path."
                    else:
                        fname = Path(fpath).name
                        r = await loop.run_in_executor(None, lambda: _rag_upload(fpath, fname))
                        if r:
                            result = f"'{fname}' added to the knowledge base ({r.get('num_chunks', '?')} chunks)."
                        else:
                            result = f"Couldn't ingest '{fname}' -- the knowledge base service may not be running at {RAG_HOST}."
                elif kb_action == "list":
                    def _list_docs():
                        import requests
                        r = requests.get(f"{RAG_HOST}/documents", timeout=10)
                        r.raise_for_status()
                        return r.json()
                    try:
                        docs = await loop.run_in_executor(None, _list_docs)
                        result = "No documents in the knowledge base yet." if not docs else \
                            "; ".join(f"{d['source_file']} ({d['chunks']} chunks)" for d in docs)
                    except Exception:
                        result = f"Couldn't reach the knowledge base service at {RAG_HOST}."
                else:
                    result = f"Unknown knowledge_base action: {kb_action}"

            elif self._plugin_registry.has(name):
                # Drop-in plugin (ported from Mark-LI's plugin system) — a
                # .py file in plugins/ that was auto-discovered at startup.
                result = await loop.run_in_executor(
                    None,
                    lambda: self._plugin_registry.run(name, args, player=self.ui),
                )

            elif name.startswith("integration_"):
                # Docked full-project integration (integrations/<slug>) —
                # forwards to its running HTTP process via IntegrationManager.
                integ_result = await loop.run_in_executor(
                    None, lambda: self._integration_manager.run_tool(name, args)
                )
                result = integ_result if integ_result is not None else f"Unknown tool: {name}"

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] {name} {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    # ── G.H.O.S.T tool dispatch ──────────────────────────────────────────
    # Synchronous twin of _execute_tool for GHOST's plain HTTP request/response
    # cycle (called from the HTTP handler thread, no asyncio event loop, no
    # live audio/vision session). Covers every actionable tool in
    # TOOL_DECLARATIONS. Deliberately excludes three that are structurally
    # tied to the JARVIS live-audio session and can't run over a stateless
    # HTTP call: screen_process (injects the captured image into the *next*
    # Gemini turn), close_camera (controls that same live camera stream),
    # and nightly_recap (drives a blocking voice conversation).
    def _ghost_execute_tool(self, name: str, args: dict) -> str:
        log.log("AGENTIC", f"[GHOST] {name} {args}")
        if name in INSTANT_ACK_TOOLS:
            self.ui.write_log(f"SYS: On it — running {name} now…")
        try:
            if name == "save_memory":
                category, key, value = args.get("category", "notes"), args.get("key", ""), args.get("value", "")
                if key and value:
                    update_memory({category: {key: {"value": value}}})
                    unified_mem.remember(category, key, value)
                return "ok"

            if name == "open_app":
                return open_app(parameters=args, response=None, player=self.ui) or f"Opened {args.get('app_name')}."
            if name == "custom_forge":
                return custom_forge(parameters=args, player=self.ui) or "C.U.S.T.O.M FORGE opened."
            if name == "pragon_compass":
                return pragon_compass(parameters=args, player=self.ui) or "P.R.A.G.O.N COMPASS opened."
            if name == "weather_report":
                return weather_action(parameters=args, player=self.ui) or "Weather delivered."
            if name == "code_helper":
                return code_helper_action(parameters=args, player=self.ui) or "Done."
            if name == "pragon_agent":
                return pragon_agent_action(parameters=args, player=self.ui) or "Done."
            if name == "agent_task":
                return agent_task_action(parameters=args, player=self.ui, speak=self.speak) or "Done."
            if name == "browser_control":
                return browser_control(parameters=args, player=self.ui) or "Done."
            if name == "file_controller":
                return file_controller(parameters=args, player=self.ui) or "Done."

            if name == "send_message":
                approved = self._confirmation_gate.ask_for_whatsapp(
                    args.get("receiver", "the recipient"), args.get("message_text", "")
                )
                if not approved:
                    return "Cancelled -- user did not confirm sending the message."
                r = send_message(parameters=args, response=None, player=self.ui, session_memory=None)
                result = r or f"Message sent to {args.get('receiver')}."
                sqlite_mem.log("send_message", str(result)[:200])
                unified_mem.log_task("send_message", str(result)[:200])
                return result

            if name == "reminder":
                return reminder(parameters=args, response=None, player=self.ui) or "Reminder set."
            if name == "youtube_video":
                return youtube_video(parameters=args, response=None, player=self.ui) or "Done."
            if name == "computer_settings":
                return computer_settings(parameters=args, response=None, player=self.ui, confirm_gate=self._confirm_gate) or "Done."
            if name == "firewall_control":
                return firewall_control(parameters=args, response=None, player=self.ui) or "Done."
            if name == "network_firewall":
                return network_firewall(parameters=args, response=None, player=self.ui) or "Done."
            if name == "waf_proxy":
                return waf_proxy(parameters=args, response=None, player=self.ui) or "Done."
            if name == "desktop_control":
                return desktop_control(parameters=args, player=self.ui) or "Done."
            if name == "compass":
                return compass(parameters=args, player=self.ui, speak=self.speak) or "Done."
            if name == "pragon_builder":
                return pragon_builder(parameters=args, player=self.ui, speak=self.speak) or "Done."

            if name == "web_search":
                r = web_search_action(parameters=args, player=self.ui)
                result = r or "Done."
                mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    query = args.get("query") or ", ".join(args.get("items", []))
                    label = f"{mode.upper()} — {query[:38]}" if query else mode.upper()
                    self.ui.show_content(label, r)
                return result

            if name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                return file_processor(parameters=args, player=self.ui, speak=self.speak) or "Done."

            if name == "file_generator":
                r = file_generator(
                    parameters=args, player=self.ui, speak=self.speak,
                    api_key_fn=_get_api_key, base_dir=BASE_DIR,
                )
                result = r or "Done."
                sqlite_mem.log("file_generator", str(result)[:200])
                unified_mem.log_task("file_generator", str(result)[:200])
                return result

            if name == "computer_control":
                return computer_control(parameters=args, player=self.ui) or "Done."
            if name == "game_updater":
                return game_updater(parameters=args, player=self.ui, speak=self.speak) or "Done."
            if name == "flight_finder":
                return flight_finder(parameters=args, player=self.ui) or "Done."
            if name == "system_status":
                return str(get_system_status())
            if name == "cpu_balancer":
                return cpu_balancer_tool(parameters=args) or "Done."

            if name == "undo_action":
                return undo_last() or "Nothing to undo."

            if name == "recall_memory":
                query = args.get("query", "")
                hits = search_memory(query)
                if not hits:
                    return f"Nothing found in memory for '{query}'."
                return "; ".join(f"{h['category']}/{h['key']}: {h['value']}" for h in hits)

            if name == "github_status":
                if not github_monitor.is_connected:
                    return "GitHub monitoring isn't configured -- set the GITHUB_TOKEN environment variable first."
                mode = args.get("mode", "notifications")
                if mode == "pr_status":
                    prs = github_monitor.get_pr_status(args.get("owner", ""), args.get("repo", ""))
                    return "No open PRs." if not prs else "; ".join(
                        f"#{p['number']} {p['title']} by {p['author']}" for p in prs
                    )
                if mode == "repo_status":
                    return github_monitor.get_repo_status(args.get("owner", ""), args.get("repo", "")).get("summary", "No status available.")
                alerts = github_monitor.get_urgent_alerts() or github_monitor.get_notifications()
                if not alerts:
                    return "No pending GitHub notifications."
                return "; ".join(alerts) if isinstance(alerts[0], str) else str(alerts)

            if name == "calendar_check":
                if not calendar_integration.is_connected:
                    return "Google Calendar isn't connected -- set up credentials.json first."
                if args.get("range", "today") == "upcoming":
                    events = calendar_integration.get_upcoming_events(days=args.get("days", 7))
                else:
                    events = calendar_integration.get_today_events()
                if not events:
                    return "No events found."
                return "; ".join(f"{e['title']} at {e['time']}" for e in events)

            if name == "n8n_workflow":
                workflow = args.get("workflow", "")
                if not n8n.is_connected:
                    return "n8n isn't running -- start it with 'n8n start' first."
                if workflow == "send_gmail":
                    approved = self._confirmation_gate.ask_for_email(args.get("to", ""), args.get("subject", ""))
                    if not approved:
                        return "Cancelled -- user did not confirm sending the email."
                    ok = n8n.send_gmail(args.get("to", ""), args.get("subject", ""), args.get("message", ""))
                    result = "Email sent." if ok else "Failed to send email via n8n."
                elif workflow == "send_whatsapp":
                    approved = self._confirmation_gate.ask_for_whatsapp(args.get("to", ""), args.get("message", ""))
                    if not approved:
                        return "Cancelled -- user did not confirm sending the message."
                    ok = n8n.send_whatsapp(args.get("to", ""), args.get("message", ""))
                    result = "WhatsApp message sent." if ok else "Failed to send WhatsApp via n8n."
                elif workflow == "create_task":
                    ok = n8n.create_task(args.get("title", ""), args.get("due_date", ""), args.get("notes", ""))
                    result = "Task created." if ok else "Failed to create task via n8n."
                elif workflow == "custom":
                    r = n8n.trigger_custom(args.get("workflow_name", "custom"), args.get("data", {}) or {})
                    result = f"Custom workflow triggered: {r}" if r else "Custom workflow failed or returned nothing."
                else:
                    result = f"Unknown n8n workflow: {workflow}"
                sqlite_mem.log("n8n_workflow", f"{workflow}: {str(result)[:150]}")
                unified_mem.log_task("n8n_workflow", f"{workflow}: {str(result)[:150]}")
                return result

            if name == "crew_task":
                result = crew.run(args.get("task", ""), args.get("context", "")) or "Done."
                sqlite_mem.log("crew_task", str(result)[:200])
                unified_mem.log_task("crew_task", str(result)[:200])
                return result

            if name == "action_agent":
                query = args.get("query", "")
                action = self._action_agent.detect_action_intent(query)
                result = "I couldn't match that to a specific action -- try rephrasing." if not action \
                    else self._action_agent.execute(action, query)
                sqlite_mem.log("action_agent", str(result)[:200])
                unified_mem.log_task("action_agent", str(result)[:200])
                return result or "Done."

            if name == "macro_action":
                mode = (args.get("mode") or "").strip().lower()
                if mode == "play":
                    macro_name = args.get("name", "this macro")
                    approved = self._confirmation_gate.ask_for_action(
                        f"I'm about to play back the '{macro_name}' macro, "
                        f"which will take over your mouse and keyboard"
                    )
                    if not approved:
                        return "Cancelled -- user did not confirm macro playback."
                result = macro_action(parameters=args, player=self.ui, speak=self.speak) or "Done."
                sqlite_mem.log("macro_action", f"{mode}: {str(result)[:150]}")
                unified_mem.log_task("macro_action", f"{mode}: {str(result)[:150]}")
                return result

            if name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested (via GHOST).")
                def _shutdown():
                    time.sleep(1)
                    os._exit(0)
                threading.Thread(target=_shutdown, daemon=True).start()
                return "Shutting down."

            if name == "library_ask":
                query = args.get("query", "")
                r = library_store.ask(query)
                answer = r.get("answer", "") if isinstance(r, dict) else str(r)
                sources = r.get("sources", []) if isinstance(r, dict) else []
                result = f"{answer}\n\n(Source: {', '.join(s['name'] for s in sources)})" if sources else answer
                sqlite_mem.log("library_ask", f"{query} -> {str(result)[:150]}")
                return result

            if name == "knowledge_base":
                kb_action = (args.get("action") or "query").strip().lower()
                if kb_action == "query":
                    question = args.get("question", "")
                    result = _rag_query(question) or "Done."
                    sqlite_mem.log("knowledge_base", f"{question} -> {str(result)[:150]}")
                    return result
                if kb_action == "ingest":
                    fpath = args.get("file_path") or self.ui.current_file
                    if not fpath:
                        return "No file to ingest -- upload one first or specify file_path."
                    fname = Path(fpath).name
                    r = _rag_upload(fpath, fname)
                    return f"'{fname}' added to the knowledge base ({r.get('num_chunks', '?')} chunks)." if r \
                        else f"Couldn't ingest '{fname}' -- the knowledge base service may not be running at {RAG_HOST}."
                if kb_action == "list":
                    import requests
                    try:
                        r = requests.get(f"{RAG_HOST}/documents", timeout=10)
                        r.raise_for_status()
                        docs = r.json()
                        return "No documents in the knowledge base yet." if not docs else \
                            "; ".join(f"{d['source_file']} ({d['chunks']} chunks)" for d in docs)
                    except Exception:
                        return f"Couldn't reach the knowledge base service at {RAG_HOST}."
                return f"Unknown knowledge_base action: {kb_action}"

            return f"Unknown tool: {name}"

        except Exception as e:
            traceback.print_exc()
            return f"Tool '{name}' failed: {e}"

    def _ghost_agent_turn(self, text: str, host: str, model: str) -> str:
        """fn(text, host, model) -> str, registered via ui.set_ghost_processor().
        Runs GHOST's message through an Ollama tool-calling loop backed by the
        same tool set as JARVIS (see _ghost_execute_tool above), then returns
        the final natural-language reply. Requires a tool-capable model --
        see GHOST_TOOL_CAPABLE_MODELS.
        """
        import requests
        host = (host or "http://localhost:11434").rstrip("/")
        model = model or "qwen2.5"

        messages = [
            {"role": "system", "content": GHOST_AGENT_SYS},
            {"role": "user", "content": text},
        ]

        base_model = model.split(":")[0].lower()
        tool_capable = any(base_model.startswith(m) for m in GHOST_TOOL_CAPABLE_MODELS)
        if not tool_capable:
            self.ui.write_ghost_log(
                f"System: '{model}' isn't on the confirmed tool-calling list -- "
                f"actions likely won't fire. Try qwen2.5, qwen3, llama3.1+, or "
                f"mistral-nemo. (Run 'ollama show {model}' to check for yourself --"
                f" if 'tools' shows under Capabilities, ignore this.)"
            )

        # Per-round Ollama request timeout. 37 tool schemas are sent on every
        # round and stream=False means Ollama must finish generating before
        # anything comes back, so a cold/CPU-bound model can legitimately
        # need more than 120s for the first round. Override via env var if
        # your hardware needs even more headroom.
        ollama_timeout = float(os.environ.get("GHOST_OLLAMA_TIMEOUT", "240"))

        self.ui.set_ghost_state("THINKING")
        try:
            final_text = ""
            for _ in range(5):  # cap chained tool-call rounds
                try:
                    resp = requests.post(
                        f"{host}/api/chat",
                        json={"model": model, "messages": messages, "tools": GHOST_TOOLS, "stream": False},
                        timeout=ollama_timeout,
                    )
                except requests.exceptions.ConnectionError as e:
                    # Nothing listening at all -- Ollama isn't running / wrong host.
                    raise RuntimeError(
                        f"GHOST couldn't connect to Ollama at {host} -- is `ollama serve` "
                        f"running and reachable at that address? ({e})"
                    ) from e
                except requests.exceptions.ReadTimeout as e:
                    # Connection was fine; the model just didn't finish in time.
                    raise RuntimeError(
                        f"GHOST reached Ollama at {host} but the model '{model}' didn't "
                        f"respond within {int(ollama_timeout)}s. It may still be loading "
                        f"into memory (try warming it up with `ollama run {model}` first) "
                        f"or may be too slow for your hardware. You can raise the limit "
                        f"with the GHOST_OLLAMA_TIMEOUT env var. ({e})"
                    ) from e

                try:
                    resp.raise_for_status()
                except requests.exceptions.HTTPError as e:
                    body = ""
                    try:
                        body = resp.text[:300]
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"Ollama at {host} returned HTTP {resp.status_code} for model "
                        f"'{model}': {body or e}"
                    ) from e

                data = resp.json()
                msg = data.get("message", {}) or {}
                tool_calls = msg.get("tool_calls") or []

                if not tool_calls:
                    final_text = msg.get("content", "") or ""
                    break

                messages.append(msg)
                for call in tool_calls:
                    fn = call.get("function", {}) or {}
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", {}) or {}
                    if isinstance(fn_args, str):
                        try:
                            fn_args = json.loads(fn_args)
                        except Exception:
                            fn_args = {}
                    self.ui.write_ghost_log(f"Ghost: [calling {fn_name}]")
                    result = self._ghost_execute_tool(fn_name, fn_args)
                    messages.append({"role": "tool", "content": str(result)})
            return final_text or "(GHOST returned an empty response.)"
        finally:
            self.ui.set_ghost_state("ACTIVE")

    # ── F.R.I.D.A.Y bridge (RAG-only, no tools) ──────────────────────────
    def _friday_processor(self, text: str, host: str, model: str) -> str:
        """fn(text, host, model) -> str, registered via ui.set_friday_processor().
        Plain Ollama chat call -- deliberately no tool loop, no TOOL_DECLARATIONS,
        no _execute_tool dispatch of any kind. FRIDAY only ever answers using
        whatever RAG context the frontend already folded into `text` via
        ragRetrieve()/ragAugment() before this was called; she has no way to
        take an action on the computer.

        Also backs POST /api/generate-file's "friday" branch (host/model come
        through as None there -- defaults below cover that case), so FRIDAY's
        file/zip generation (via the browser's jsPDF/SheetJS/JSZip pipeline)
        actually has a model to write content instead of raising
        'F.R.I.D.A.Y backend not ready yet'.
        """
        import requests
        host = (host or "http://localhost:11434").rstrip("/")
        model = model or "gemma3"

        messages = [
            {"role": "system", "content": (
                "You are FRIDAY, a retrieval-grounded assistant inside P.R.A.G.O.N. "
                "You have no ability to control the computer or take any action -- "
                "you only read the context you're given and answer or generate text/"
                "content from it."
            )},
            {"role": "user", "content": text},
        ]
        ollama_timeout = float(os.environ.get("GHOST_OLLAMA_TIMEOUT", "240"))
        try:
            resp = requests.post(
                f"{host}/api/chat",
                json={"model": model, "messages": messages, "stream": False},
                timeout=ollama_timeout,
            )
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                f"FRIDAY couldn't connect to Ollama at {host} -- is `ollama serve` "
                f"running and reachable at that address? ({e})"
            ) from e
        except requests.exceptions.ReadTimeout as e:
            raise RuntimeError(
                f"FRIDAY reached Ollama at {host} but the model '{model}' didn't "
                f"respond within {int(ollama_timeout)}s. It may still be loading "
                f"into memory (try warming it up with `ollama run {model}` first) "
                f"or may be too slow for your hardware. You can raise the limit "
                f"with the GHOST_OLLAMA_TIMEOUT env var. ({e})"
            ) from e

        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            body = ""
            try:
                body = resp.text[:300]
            except Exception:
                pass
            raise RuntimeError(
                f"Ollama at {host} returned HTTP {resp.status_code} for model "
                f"'{model}': {body or e}"
            ) from e

        data = resp.json()
        return (data.get("message", {}) or {}).get("content", "") or "(FRIDAY returned an empty response.)"

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        print("[JARVIS] Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if not jarvis_speaking and not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )
            elif self.ui.muted and self.ui.wake_word_enabled and self._wake_detector.ready:
                # Mic is muted but the user asked to be woken by voice -- run the
                # (cheap, local, offline) wake-word model on the frames instead of
                # streaming them to Gemini. feed() is non-blocking, matches the
                # real-time constraint of this callback.
                self._wake_detector.feed(indata)

        try:
            input_idx = audio_devices.resolve_input(get_input_device_name())
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=callback,
                device=input_idx,
            ):
                print("[JARVIS] Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] Mic: {e}")
            raise

    async def _receive_audio(self):
        print("[JARVIS] Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        if self._interrupted:
                            pass # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))

                                # Bridge to any agentic module blocking on _blocking_listen()
                                if self._awaiting_blocking_input:
                                    self._blocking_input_queue.put(full_in)

                                # Auto language detection (PRD 4.4) -- log + silently
                                # remember a consistently-used non-English language.
                                try:
                                    lang_code = language_detector.detect(full_in)
                                    if lang_code != "en":
                                        lang_name = language_detector.get_language_name(lang_code)
                                        log.log("ROUTER", f"Detected language: {lang_name} ({lang_code})")
                                        update_memory({"identity": {"language": {"value": lang_name}}})
                                except Exception as e:
                                    log.error("language_detect", e)
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"Jarvis: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            # Vision injection: model finished tool-response turn now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] {len(img_b):,} bytes (angle={angle}) main session")
                                await self.session.send_client_content(
                                    turns={"parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until JARVIS finishes speaking the answer
                                    self._vision_cam_active = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[JARVIS] Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] Play started")

        output_idx = audio_devices.resolve_output(get_output_device_name())
        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
            device=output_idx,
        )
        stream.start()

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                if self.ui.muted:
                    # Drop audio silently while muted — don't play it, and don't
                    # flip into the SPEAKING state.
                    continue

                self.set_speaking(True)
                try:
                    await asyncio.to_thread(stream.write, chunk)
                except (RuntimeError, asyncio.CancelledError):
                    break # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[JARVIS] Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Startup briefing: instant greeting, followed by the agentic
        deterministic brief (calendar + deadlines + GitHub + goals).
        World-news fetching/reading on startup has been removed.
        """
        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── memory ───────────────────────────────────────────────────────────
        memory = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")

        from datetime import datetime
        time_str = datetime.now().strftime("%I:%M %p").lstrip("0")

        # ── Greeting — one simple sentence ────────────────────────────────────
        lang_clause = f" Respond in {lang}." if lang else ""
        name_clause = f" Address the user as {name}." if name else ""
        p1 = (
            f"Greet the user and mention it is {time_str}. "
            f"One short sentence only. Do not call any tools.{lang_clause}{name_clause}"
        )

        await self.session.send_client_content(
            turns={"parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing greeting sent.")

        # ── Agentic deterministic brief (calendar + deadlines + GitHub + goals) ──
        async def _guarded_agentic_brief():
            try:
                await self._briefing_agentic_phase(lang)
            except Exception as e:
                print(f"[Briefing] Agentic phase error: {e}")
                self.ui.write_log(f"SYS: Briefing agentic phase failed: {e}")
        asyncio.create_task(_guarded_agentic_brief())

    async def _briefing_agentic_phase(self, lang: str) -> None:
        """
        Runs the agentic morning brief (calendar/deadlines/GitHub/goals) a
        couple seconds after the greeting so its audio doesn't overlap.
        """
        await asyncio.sleep(2.0)

        if not self.session:
            return

        # -- Agentic deterministic brief (calendar + deadlines + GitHub + goals) --
        try:
            brief_text = await asyncio.to_thread(
                self._morning_brief.generate,
                sqlite_mem, calendar_integration, github_monitor, crew
            )
            if brief_text and self.session:
                self.speak(brief_text)
                log.log("AGENTIC", "Morning brief delivered.")
        except Exception as e:
            log.error("morning_brief", e)

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if alert and self.session:
                try:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": alert}]},
                        turn_complete=True,
                    )
                except Exception as e:
                    print(f"[Monitor] Could not send alert: {e}")

    # ── CPU balancer (option) ────────────────────────────────────────────────────

    async def _run_cpu_balancer(self) -> None:
        """Background task: quietly rebalances CPU load across cores/processes."""
        while True:
            await asyncio.sleep(15)
            note = await asyncio.to_thread(self._cpu_balancer.check)
            if note:
                print(f"[CPUBalancer] {note}")

    # ── Audio device picker ──────────────────────────────────────────────────────

    def _on_list_audio_devices(self) -> dict:
        """Called by PragonUI when the settings drawer opens the audio panel."""
        devices = audio_devices.list_devices()
        return {
            "inputs": devices["inputs"],
            "outputs": devices["outputs"],
            "current_input": get_input_device_name() or audio_devices.DEFAULT_LABEL,
            "current_output": get_output_device_name() or audio_devices.DEFAULT_LABEL,
        }

    def _on_set_audio_device(self, kind: str, name: str) -> str:
        """Called by PragonUI when the user picks a mic/speaker from the dropdown.

        Takes effect on the next stream open (next reconnect / next _listen_audio
        or _play_audio start) — we don't tear down a live stream mid-session.
        """
        input_name = get_input_device_name()
        output_name = get_output_device_name()
        if kind == "input":
            input_name = name
        elif kind == "output":
            output_name = name
        else:
            return f"Unknown device kind: {kind}"
        save_audio_devices(input_name, output_name)
        label = name or audio_devices.DEFAULT_LABEL
        return f"{kind.title()} device set to {label}. Takes effect on next reconnect."

    # -- wake word ("Hey Jarvis") — ported from Mark-LIII --------------------

    def _start_wake_word(self) -> None:
        """Installs openwakeword + downloads its model on first use (blocking,
        so always run this off the main thread), then starts the detector.
        Safe to call repeatedly -- both install_and_download and start() are no-ops
        once already done."""
        if not wake_word.is_ready():
            ok, msg = wake_word.install_and_download(
                logger=lambda m: self.ui.write_log(f"SYS: {m}")
            )
            if not ok:
                self.ui._broadcast({"type": "toast", "text": f"Wake word setup failed: {msg}"})
                self.ui.wake_word_enabled = False
                save_wake_word_enabled(False)
                self.ui._broadcast({"type": "wake_word_status", "enabled": False})
                return
        self._wake_detector.start()

    def _handle_wake_word_toggle(self, enabled: bool) -> None:
        """Called by PragonUI when the topbar WAKE WORD button is clicked."""
        save_wake_word_enabled(enabled)
        if enabled:
            threading.Thread(target=self._start_wake_word, daemon=True).start()
        else:
            self._wake_detector.stop()

    def _on_wake_word_detected(self) -> None:
        """Runs on the wake-word thread (not the asyncio loop) -- keep this cheap
        and thread-safe. Unmutes the mic so the next utterance is heard normally,
        exactly as if the user had clicked the mute button off."""
        self.ui.muted = False
        self.ui.set_state("LISTENING")
        self.ui._broadcast({"type": "mute_state", "muted": False})
        self.ui._broadcast({"type": "toast", "text": "Wake word detected -- listening"})

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_deadline_check(self) -> None:
        """Background task: periodic Smart Deadline Manager alerts (PRD 5.3)."""
        while True:
            await asyncio.sleep(1800) # every 30 minutes
            try:
                alerts = await asyncio.to_thread(self._deadline_manager.check_upcoming)
                for alert in alerts:
                    if self.session:
                        await self.session.send_client_content(
                            turns={"parts": [{"text": alert}]},
                            turn_complete=True,
                        )
                        log.log("AGENTIC", f"Deadline alert: {alert}")
            except Exception as e:
                log.error("deadline_manager", e)

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60) # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory = await asyncio.to_thread(load_memory)
                prompt = self._proactive.build_prompt(memory)
                await self.session.send_client_content(
                    turns={"parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    def _on_phone_audio_frame(self, chunk: bytes) -> None:
        """Called by PhoneViewServer (in the asyncio loop) for each mic frame."""
        try:
            self._phone_audio_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            pass

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from the phone-audio queue into the Gemini Live session."""
        q = self._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via PhoneView.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._phone_cmd_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()

        # Start PhoneView (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from pragon_phoneview import PhoneViewServer
            self._dashboard = PhoneViewServer(app_name="Pragon")
            self._dashboard.on_connect(self._on_phone_connected)
            self._dashboard.on_command(lambda text: self._phone_cmd_queue.put_nowait(text))
            self._dashboard.on_audio_frame(self._on_phone_audio_frame)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state.
                # v1alpha carries the enhanced audio features (affective dialog,
                # proactive audio); if they get rejected we fall back to v1beta.
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1alpha" if self._enhanced_live else "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session = session
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision = None
                    self._vision_cam_active = False
                    self._vision_close_pending = False
                    self._vision_busy = False
                    self._vision_last_time = 0.0
                    self._interrupted = False

                    print("[JARVIS] Connected.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: JARVIS online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_cpu_balancer())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_deadline_check())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled)
                    if not self._briefing_sent and get_brief_enabled():
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                err_str = str(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Enhanced audio features rejected by the server (preview API
                # drift) — drop them and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "affective" in err_str.lower()
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Advanced audio features unavailable — reconnecting without them."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    if hasattr(self.ui, "prompt_reconfig") and hasattr(self.ui, "_win"):
                        # Legacy desktop UI: blocks until the user re-enters a key in-app.
                        self.ui.prompt_reconfig()
                        while not self.ui._win._ready:
                            await asyncio.sleep(1)
                        print("[JARVIS] New API key saved — reconnecting...")
                    else:
                        # Web UI: no in-app key entry — edit the config file and retry.
                        self.ui.write_log(
                            "SYS: Update api/api_keys.json with a valid gemini_api_key, "
                            "then it will retry automatically."
                        )
                        await asyncio.sleep(30)
                    _conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    net_msg = (
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(VPN may be required)"
                    )
                    self.ui.write_log(net_msg)
                    print(f"[JARVIS] {net_msg}")
                    try:
                        diagnosis = await _diagnose_live_connectivity()
                        print(f"[JARVIS] Diagnosis: {diagnosis}")
                        self.ui.write_log(f"SYS: {diagnosis}")
                    except Exception as diag_err:
                        print(f"[JARVIS] (diagnostic probe itself failed: {diag_err})")
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    ui = PragonUI("face.png")
    threading.Thread(target=_ensure_rag_service, daemon=True).start()

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root_mainloop()

if __name__ == "__main__":
    main()