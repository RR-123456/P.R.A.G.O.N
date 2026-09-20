IMPORTANT NOTE:

1.Gemini key format — added a check in the Installation section and the Troubleshooting table: valid keys start with AIza...; a key starting with AQ... is a different Google credential type and won't authenticate.

2.Zen Mode awareness — TYPE: "PRAGON DADDY HOME" TO TURN OFF ZEN MODE

3.WhatsApp bot npm install/run — already in place (cd pragon_whatsapp && npm install && npm start, with the QR-pairing note).


[COPILOT ARCHITECTURE.pdf](https://github.com/user-attachments/files/32413875/COPILOT.ARCHITECTURE.pdf) <--CLICK THIS TO VIEW HI DEVS COPILOT ARCHITECTURE
<img width="1536" height="1024" alt="ChatGPT Image Sep 19, 2026, 04_31_42 PM" src="https://github.com/user-attachments/assets/ec829cc3-428f-4570-b851-8377da20799c" />
<div align="center">

# P.R.A.G.O.N
### Personalized Real-time Agentic Global Operating Network

**The AI operating system that thinks at the speed of your work — not the speed of your Wi-Fi.**

`Local-First AI` · `Sub-10ms Memory` · `Sub-500ms Voice` · `Zero Cloud Dependency Required`

*Built for the people who don't get a second chance to hear "please wait" — field workers,developers,content creators,tech,enthusiasist,students,dispatchers, medics, field engineers and anyone else whose job doesn't pause for buffering.*

</div>

---

## Table of Contents

1. [The Problem With "Smart" Assistants Today](#the-problem-with-smart-assistants-today)
2. [PRAGON — The 4W](#pragon--the-4w)
3. [Executive Summary](#executive-summary)
4. [Problem Statement](#problem-statement)
5. [Goals & Objectives](#goals--objectives)
6. [Target Users & Stakeholders](#target-users--stakeholders)
7. [Core Components](#core-components)
8. [Full Feature List](#full-feature-list)
9. [Functional Requirements](#functional-requirements)
10. [Non-Functional Requirements](#non-functional-requirements)
11. [User Manual](#user-manual)
12. [Architecture](#architecture)
13. [Tech Stack](#tech-stack)
14. [API Specification](#api-specification)
15. [Performance Targets — By the Numbers](#performance-targets--by-the-numbers)
16. [Model Temperature Guide](#model-temperature-guide)
17. [Security & Compliance](#security--compliance)
18. [Data & Persistence](#data--persistence)
19. [Deployment & Infrastructure](#deployment--infrastructure)
20. [Success Metrics](#success-metrics)
21. [Why PRAGON Wins](#why-pragon-wins)
22. [Advantages & Positives](#advantages--positives)
23. [Roadmap & Milestones](#roadmap--milestones)
24. [Open Questions & Risks](#open-questions--risks)
25. [Repository Structure](#repository-structure)
26. [Quick Start](#quick-start)
27. [Environment Configuration](#environment-configuration)
28. [Documentation Index](#documentation-index)

---

## The Problem With "Smart" Assistants Today

Every mainstream AI assistant makes the same bet: send everything to the cloud and hope the network holds up. In a hospital hallway, a basement server room, a job site with two bars of signal, or a dispatch center at three in the morning, that bet loses.

- **Too slow** — round-trip cloud inference kills the rhythm of natural conversation.
- **Too dependent** — no signal, no assistant.
- **Too fragmented** — ten different apps for messages, maps, notes, code, and scheduling.
- **Too risky** — a single unvalidated AI-triggered action can corrupt a system or a record.

PRAGON exists to make all four of those problems disappear, by moving intelligence, memory, and control as close to the user as physically possible.

---

## PRAGON — The 4W

**What:**
PRAGON solves the problem of multiple disconnected AI tools and slow manual workflows by combining understanding, planning, context retrieval, tool usage, and task execution into a single AI agent system. MOSS, the memory layer underneath it, provides sub-10ms context retrieval so that relevant knowledge is available before the conversation even slows down.

**Why:**
To make AI interaction faster, context-aware, and autonomous — reducing repeated manual work and removing the tax of waiting on a network that field workers, dispatchers, and clinicians simply cannot afford to pay.

**Who:**
Developers, students, creators, businesses, field workers, healthcare teams, dispatch operations, and customer-support teams — anyone whose work happens in real time and cannot be rescheduled around connectivity.

**How:**

| Layer | Technology | Role |
|---|---|---|
| Frontend/UI | Next.js | User-facing interface and desktop shell |
| Backend & API | Python + FastAPI | Orchestration, routing, and service layer |
| Real-time audio | LiveKit | Voice/audio communication pipeline |
| Memory & retrieval | MOSS | Sub-10ms context retrieval |
| Reasoning | PRAGON Agent | Understands, plans, uses tools, executes tasks |

**Flow:** User Input → LiveKit / Next.js → FastAPI / Python → MOSS retrieves context → PRAGON Agent processes → Tools execute → Result is returned.

---

## Executive Summary

**P.R.A.G.O.N (Personalized Real-time Agentic Global Operating Network) + MOSS (Modular Operating Smart System)** is a production-grade, local-first AI operating system designed for high-stakes, real-time environments such as emergency dispatch, healthcare, and field engineering. It delivers a "Jarvis-like" experience by combining ultra-low-latency voice interaction (LiveKit plus a fast-tier language model) with sub-10ms context retrieval (MOSS), while prioritizing data privacy, offline resilience, and safety-critical execution at every layer.

Status: Production-ready track submission, version 1.1.

---

## Problem Statement

Field workers, dispatchers, and healthcare professionals operate in environments where every millisecond counts. Existing AI solutions suffer from four structural weaknesses:

- **High latency** — cloud-based LLMs and standard RAG pipelines are too slow for natural conversation.
- **Cloud dependency** — the absence of offline functionality makes tools useless in remote or secure areas.
- **Context fragmentation** — the assistant forgets immediate context or cannot reach local technical manuals.
- **Safety risk** — unvalidated AI actions can lead to system corruption or incorrect data entry.

## Goals & Objectives

- **Zero-latency interaction** — achieve sub-500ms end-to-end voice latency and sub-10ms context retrieval.
- **Local-first sovereignty** — remain fully functional offline via local LLM (Ollama) fallback.
- **Enterprise hardening** — resolve shared-database anti-patterns and implement robust JWT/TLS security.
- **Proactive assistance** — move from reactive chat to proactive "Zen Mode" and "Master Control" operation.

## Target Users & Stakeholders

| Stakeholder | Primary need |
|---|---|
| Emergency dispatchers | Instant retrieval of protocols and real-time transcription |
| Healthcare workers | Hands-free patient record access and automated charting |
| Field engineers | Real-time technical manual queries and navigation in low-network areas |
| Sales teams | Persona-based rehearsal across tones and languages |
| Admins / company managers | Custom Forge for building purpose-specific agent workflows |
| Developers | A full toolchain: visual builder, code sandbox, GitHub hooks, tracing |
| Non-technical users | Voice-first control with no coding required |

---

## Core Components

| Component | What it does |
|---|---|
| PRAGON Core Runtime | Understands, plans, chooses tools, and executes — the reasoning core of the system |
| MOSS | Sub-10ms semantic memory and context retrieval engine |
| LiveKit Voice Agent | Real-time, wake-word-gated voice interaction |
| Custom Forge | Sketch or describe an app; get working single-file code back, live |
| Agent Builder | Drag-and-drop canvas for building autonomous ReAct-style agent workflows |
| RAG System | Fully offline retrieval over local PDFs and documents via ChromaDB |
| PRAGON C.O.M.P.A.S.S | Maps, routing, location trace, and field data collection |
| PRAGON Gallery | Generated image storage and instant company tool/asset lookup |
| PRAGON Persona | Rehearsal mode for sales and communication, across tones and languages |
| PRAGON Pulse | Three.js particle effects, 3D model generation, face mesh, AR viewing |
| WhatsApp Bridge | Remote notifications and control when the phone can't be picked up |
| Phone Companion | QR-paired notification and clipboard sync |
| Resource Balancer | Dynamic CPU/network throttling that keeps the OS smooth under load |
| Code Sandbox | gVisor-isolated execution — generated code never touches the host system directly |
| Backup Manager | Zero-corruption snapshotting through the Persistence Service |
| OTel Tracing Service | End-to-end tracing of every agentic "thought" and tool call |
| Scheduler Service | Automated Morning Briefs and Nightly Recaps |

---

## Full Feature List

| Category | Highlights |
|---|---|
| Voice & language | Wake word "Hey JARVIS," automatic language detection (including Tamil), no accidental activations |
| Personalities | JARVIS (default), Friday, Ghost, Omnics — switchable mid-conversation |
| Everyday control | Messages, weather, YouTube, system and RAM monitoring, all by voice |
| Field tools | C.O.M.P.A.S.S navigation, location trace, delivery tracking, field data capture |
| Visual memory | PRAGON Gallery for generated images and company asset/tool lookup |
| Rehearsal mode | PRAGON Persona for sales practice in multiple tones and languages |
| Reminders & timers | Medication, breaks, safety gear (helmets, gloves), voice-set timers and stopwatches |
| Workforce operations | Per-worker schedules, live task checklists, automatic company-side sync |
| Memory | MOSS + ChromaDB + SQLite — conversational recall of past sessions |
| Messaging | WhatsApp bridge for hands-off notifications and control |
| Builder tools | Custom Forge (code editor, AI-assisted fixes, export) and Agent Builder (visual workflows) |
| Creative engine | PRAGON Pulse — particles, 3D models, AR viewing, face mesh |
| Automation | Calendar sync, N8N workflows, crew orchestration, deadline management, GitHub monitoring, morning and nightly briefs |
| System control | Desktop and file access, CPU/RAM monitoring, firewall control, one-shot Macro Actions |
| Operating modes | Zen Mode by default; the phrase "PRAGON DADDY HOME" escalates to full administrative control |
| Setup | GitHub-based configuration (`api/api_keys.json`), single-command install and launch |

## Functional Requirements

| ID | Requirement | Description | Component |
|---|---|---|---|
| FR-01 | Real-time voice | Sub-500ms voice-to-voice loop over WebRTC | LiveKit + fast-tier model |
| FR-02 | Persistent memory | Long-term storage of user preferences and history | Persistence Service + SQLite |
| FR-03 | Sub-10ms retrieval | Instant context injection for active conversations | MOSS Context Engine |
| FR-04 | Offline fallback | Automatic switch to local inference when internet is lost | Ollama (Llama 3.1) |
| FR-05 | Resource balancing | Dynamic CPU/network throttling to prevent OS hangs | CPU & Network Balancer |
| FR-06 | Safe code execution | Execution of generated scripts in an isolated container | Code Sandbox (gVisor) |
| FR-07 | WhatsApp bridge | Remote control and notifications via WhatsApp | WhatsApp Node Bridge |
| FR-08 | Deep observability | End-to-end tracing of every agentic thought and tool call | OTel Tracing Service |
| FR-09 | Visual workflows | Drag-and-drop agent logic builder | Custom Forge / Agent Builder |
| FR-10 | Phone sync | QR-based pairing for notification and clipboard sync | Phone Companion |
| FR-11 | RAG intelligence | Local document ingestion (PDF/DOCX) with vector search | RAG System + ChromaDB |
| FR-12 | Maps & navigation | Real-time routing and location-aware assistance | Navigation Service |
| FR-13 | Proactive briefs | Automated Morning Briefs and Nightly Recaps | Scheduler Service |

## Non-Functional Requirements

- **Performance:** MOSS retrieval under 10ms; voice time-to-first-token under 400ms.
- **Security:** no direct service access to database files; every API call JWT-authenticated; TLS 1.3 across the LAN.
- **Reliability:** 99.9% uptime for local services, with graceful degradation to Ollama.
- **Scalability:** support for up to 50 concurrent local tool executions via the Resource Balancer.
- **Usability:** the interface holds 60fps even during heavy inference.

---

## User Manual

### 1. Wake up PRAGON
Say "Hey JARVIS." Nothing runs without the wake word — no exceptions, no accidental activations.

### 2. Choose your mode
- **Zen Mode** (default) — calm, restricted, everyday-safe.
- **"PRAGON DADDY HOME"** — the explicit phrase that unlocks administrative and master controls, so nothing sensitive is ever one accidental word away.

### 3. Pick a voice
Say "Switch to Friday" or "Talk to me as Ghost" to change personality mid-conversation. Use PRAGON Persona specifically when rehearsing a pitch: choose a tone, choose a language, run the scene.

### 4. Run your day by voice
- "What's the weather?" / "Send a message to [name]" / "Play [video]" / "How's my system doing?" — language is auto-detected.
- "Remind me to take my medication at 3pm" / "Remind me to wear my helmet."
- "Set a 20-minute timer" / "Start the stopwatch."

### 5. Navigate the field
Ask C.O.M.P.A.S.S for a route, a location trace, or to log field data on the spot, without switching to a separate app.

### 6. Pull up visuals instantly
"Show me the spanner" or "Find the compressor tool image" — PRAGON Gallery surfaces stored and generated imagery on command.

### 7. Ask PRAGON to remember
"What did I log last Tuesday?" — MOSS and the local memory store answer conversationally, with no manual search and no cloud round-trip.

### 8. Track the whole crew
Workers receive live task checklists; every completed task rolls back to the company dashboard automatically.

### 9. Stay reachable hands-free
Turn on the WhatsApp bridge to receive notifications and issue simple commands when voice or desktop access isn't possible.

### 10. Build without waiting on a dev team
- **Custom Forge** (`http://127.0.0.1:5000`) — sketch or describe an app, get working HTML/JS/CSS back, with AI-assisted bug fixing.
- **Agent Builder** (`http://127.0.0.1:5057`) — wire up autonomous multi-tool agents on a visual canvas, no code required.

### 11. Control your machine
Check CPU/RAM, manage firewall rules, browse files, or record a Macro Action once and replay the whole sequence with a single voice command from then on.

### 12. Let PRAGON run itself
Connect a calendar or N8N flow for deadline tracking and crew orchestration, and receive an automatic Morning Brief each day with a Nightly Recap waiting at the end of it.
# PRAGON User Manual

**Version 1.1 — Local-First AI Operating Layer**

This manual explains how to install, activate, and use every part of PRAGON day to day. It is written for the person actually using the assistant on shift, at a desk, or in the field — not for the engineering team building it.

> **Integration note:** this document is the canonical PRAGON User Guide. Any "User Guide" button, menu item, or help link inside the PRAGON UI (desktop shell, Custom Forge, Agent Builder, or the phone companion app) should open this file — either by bundling it and rendering it in-app, or by linking directly to wherever it's hosted. Keeping every "User Guide" entry point pointed at this single file avoids multiple, drifting copies of the same instructions.

---

## Table of Contents

1. [Before You Start](#1-before-you-start)
2. [Installation](#2-installation)
3. [First Launch & Wake Word Setup](#3-first-launch--wake-word-setup)
4. [Understanding Operating Modes](#4-understanding-operating-modes)
5. [Talking to PRAGON: Personas](#5-talking-to-pragon-personas)
6. [Everyday Voice Commands](#6-everyday-voice-commands)
7. [Reminders, Timers & Safety Alerts](#7-reminders-timers--safety-alerts)
8. [PRAGON C.O.M.P.A.S.S — Navigation & Field Work](#8-pragon-compass--navigation--field-work)
9. [PRAGON Gallery — Visual Lookup](#9-pragon-gallery--visual-lookup)
10. [PRAGON Persona — Rehearsal Mode](#10-pragon-persona--rehearsal-mode)
11. [Memory & Recall](#11-memory--recall)
12. [Scheduling & Work Tracking](#12-scheduling--work-tracking)
13. [WhatsApp Bridge](#13-whatsapp-bridge)
14. [Custom Forge — Build Apps by Sketch or Description](#14-custom-forge--build-apps-by-sketch-or-description)
15. [Agent Builder — Visual Automation Canvas](#15-agent-builder--visual-automation-canvas)
16. [PRAGON Pulse — Visual & Creative Engine](#16-pragon-pulse--visual--creative-engine)
17. [Automation & Daily Briefs](#17-automation--daily-briefs)
18. [Desktop Control & Macro Actions](#18-desktop-control--macro-actions)
19. [Working Offline](#19-working-offline)
20. [Privacy & Safety Guidelines](#20-privacy--safety-guidelines)
21. [Troubleshooting](#21-troubleshooting)
22. [Frequently Asked Questions](#22-frequently-asked-questions)
23. [Quick Reference: Voice Command Cheat Sheet](#23-quick-reference-voice-command-cheat-sheet)
24. [Glossary](#24-glossary)

---

## 1. Before You Start

PRAGON is a local-first AI assistant: most of what it does — remembering context, retrieving your documents, running voice commands — happens on your own machine rather than in the cloud. That means it stays fast and functional even with no internet connection, and your data does not need to leave your device to be useful.

**What you need:**
- A workstation or laptop running the PRAGON platform (Python 3.10+)
- A microphone for voice interaction
- (Recommended, not required) [Ollama](https://ollama.com) installed locally, so PRAGON can fall back to a local model if you lose internet access

**What you get:**
- A single assistant that handles voice conversation, memory, navigation, scheduling, automation, and app building
- A wake-word-gated voice interface, so nothing runs by accident
- Two operating modes — a calm default mode and an explicit administrative mode — so sensitive actions are never one accidental word away

---

## 2. Installation

1. Obtain the PRAGON repository and open a terminal in its root folder.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy the example API key file and fill in your own credentials:
   ```bash
   cp api/api_keys.json.example api/api_keys.json
   ```
   Open `api/api_keys.json` and add your model API key and LiveKit credentials.

   **Key format check:** a valid Gemini API key starts with **`AIza...`**. A key starting with **`AQ...`** is not a Gemini API key — it's a different Google credential type (for example, an OAuth access token) and will not work here. If PRAGON's cloud model calls are failing with an authentication error, the first thing to check is that the string in `api_keys.json` actually begins with `AIza`.
4. If you plan to use Custom Forge's RAG system or the Agent Builder, install their additional dependencies from the `requirements.txt` files inside `ragsystem/` and `customforge/agent_builder/`.

---

## 3. First Launch & Wake Word Setup

1. Start the assistant with memory enabled:
   ```bash
   python run_pragon_moss.py
   ```
2. Wait for the runtime to confirm that MOSS (the memory engine) has initialized.
3. Speak the wake word: **"Hey JARVIS."** PRAGON will not respond to anything you say before this phrase — this is intentional, and it is the single most important safety feature in the system. There is no way to disable wake-word gating from a casual voice command.
4. Once activated, speak naturally. PRAGON automatically detects the language you are speaking in, including Tamil, and will respond in kind.

**A note for first-time users:** the moment PRAGON starts, it is already running in **Zen Mode** — its default, restricted, everyday-safe mode. You don't need to turn this on or configure it; nobody has to know about it in advance for it to protect them. It simply means that, from your very first conversation, PRAGON will handle everyday requests but will not touch your desktop, files, or system settings until you deliberately ask for that with the Master Control phrase covered in [Section 4](#4-understanding-operating-modes). If this is your first session, it's worth reading that section before you go further, so you know what to expect as normal behavior versus what requires an extra step.

If voice activation does not appear to be working, see [Troubleshooting](#21-troubleshooting).

---

## 4. Understanding Operating Modes

PRAGON runs in one of two modes at any time:

### Zen Mode (default)

Zen Mode is the state PRAGON is in from the moment it starts, and the state it returns to unless you deliberately escalate. It exists to keep the assistant calm, predictable, and safe for continuous everyday use, without anyone — including an admin — having to think about it.

**What Zen Mode allows:**
- Voice utilities (weather, messages, media playback, system status checks)
- Reminders, timers, and safety alerts
- Navigation and field data logging through C.O.M.P.A.S.S
- Memory recall through MOSS
- Scheduling and task-checklist updates
- Persona switching and PRAGON Persona rehearsal
- Gallery lookups and WhatsApp bridge notifications

**What Zen Mode blocks:**
- Desktop and file-system control
- Firewall or system-settings changes
- Recording or replaying Macro Actions
- Any action tagged as administrative in PRAGON's tool permissions

**Why it exists:** most of a shift, a call, or a session is made up of low-risk, repeatable actions — not the kind of thing that should ever require a special unlock. By keeping those actions unrestricted while walling off anything that touches the underlying system, PRAGON stays genuinely hands-free for the 95% of interactions that are routine, while making the remaining 5% deliberate. Nobody, including an administrator, can casually drift out of Zen Mode by accident — reaching Master Control always requires the explicit phrase below, spoken on purpose.

**Returning to Zen Mode:** once you finish an administrative task in Master Control, PRAGON returns to Zen Mode automatically after a short idle period, or immediately if you say "Back to Zen Mode." Keeping sessions in Zen Mode by default is the safest posture for day-to-day use.

### Master Control
- Reached only by saying: **"PRAGON DADDY HOME."**
- Unlocks administrative and system-level actions (desktop control, firewall changes, macro recording, deeper automation configuration).
- Exiting back to Zen Mode is recommended once your administrative task is done, to keep day-to-day use as safe as possible.

**Why two modes?** Separating "everyday assistant" from "administrative control" means a misheard word or background noise cannot trigger something sensitive. You always have to mean it.

---

## 5. Talking to PRAGON: Personas

PRAGON ships with four selectable voice personalities:

| Persona | Best for |
|---|---|
| JARVIS (default) | General-purpose daily use |
| Friday | An alternate conversational tone |
| Ghost | A more direct, technical tone |
| Omnics | A distinct alternate personality |

**To switch:** say "Switch to Friday," "Talk to me as Ghost," or name any available persona directly. The change applies immediately, mid-conversation, and stays until you switch again or restart.

---

## 6. Everyday Voice Commands

Once activated with the wake word, you can ask PRAGON to:

- Check the weather: *"What's the weather like today?"*
- Send a message: *"Send a message to [contact name]."*
- Play media: *"Play [video name] on YouTube."*
- Check system health: *"How's my system doing?"* or *"What's my RAM usage?"*

All of the above work in whichever language you speak — PRAGON detects it automatically, so you don't need to set a language preference manually.

---

## 7. Reminders, Timers & Safety Alerts

PRAGON can set voice-triggered reminders and timers for anything time-based:

- Medication: *"Remind me to take my medication at 3pm."*
- Breaks: *"Remind me to take a break in an hour."*
- Safety gear: *"Remind me to wear my helmet before I start"* or *"Remind me to put on my gloves."*
- Timers: *"Set a timer for 20 minutes."*
- Stopwatch: *"Start a stopwatch."*

These reminders persist through PRAGON's memory layer, so they will still fire even if you've moved on to a different task or conversation.

---

## 8. PRAGON C.O.M.P.A.S.S — Navigation & Field Work

C.O.M.P.A.S.S handles anything location- or route-related:

- **Get a route:** *"Route me to the next delivery point."*
- **Trace a location:** *"Log my current location."*
- **Track a delivery:** *"Where's package [ID]?"*
- **Collect field data:** *"Log this site's readings"* or describe the data point you want recorded; PRAGON stores it against the current job/location context.

This is designed for hands-busy situations — you don't need to open a separate maps app or type anything.

---

## 9. PRAGON Gallery — Visual Lookup

Gallery stores both AI-generated images and company tool/asset photos, and lets you retrieve them by voice or text:

- *"Show me the spanner."*
- *"Find the tool image for the compressor."*
- *"Save this image to the gallery"* (after PRAGON generates or receives one).

Anything PRAGON generates elsewhere in the system (Custom Forge previews, PRAGON Pulse renders) is automatically archived here for later lookup.

---

## 10. PRAGON Persona — Rehearsal Mode

Distinct from the four conversational personas in Section 5, **PRAGON Persona** is a dedicated rehearsal environment — most commonly used for sales practice:

1. Say *"Start a Persona rehearsal."*
2. Choose a tone (e.g. formal, friendly, assertive) and a language.
3. Run through your pitch or conversation out loud; PRAGON responds in character to give you a realistic practice partner.
4. Switch tone or language at any point mid-session without restarting.

---

## 11. Memory & Recall

PRAGON's memory is powered by **MOSS**, backed by a local SQLite timeline database and a ChromaDB vector store. This is what lets you ask about anything from a past session conversationally:

- *"What did I log last Tuesday?"*
- *"What did we discuss about the compressor last month?"*
- *"Remind me what my last shift's checklist looked like."*

Retrieval happens on-device, which is why it can return an answer in a fraction of a second rather than waiting on a network round-trip.

---

## 12. Scheduling & Work Tracking

For teams and shift-based work:

- Each worker receives a **per-shift task checklist**.
- Completing or updating a task through voice or the UI syncs automatically back to a company-side dashboard.
- Managers can review live status across a crew without needing to check in individually.

---

## 13. WhatsApp Bridge

When you can't access voice or desktop directly:

1. Enable the WhatsApp bridge from PRAGON's settings.
2. You will begin receiving PRAGON notifications over WhatsApp.
3. You can reply with simple text commands, which PRAGON interprets the same way it would a voice command.

This is designed as a fallback channel, not a replacement for voice — use it when your hands or environment don't allow for either.

### Running the WhatsApp bridge (Node service)

The WhatsApp bridge (`pragon_whatsapp/`) is a Node.js component rather than a Python one, since it's built on the **WhatsApp Node Bridge**. It runs as its own process alongside the main PRAGON runtime rather than through `pragon_main.py`. The typical Node workflow is:

```bash
cd pragon_whatsapp
npm install
npm start
```

If `npm start` isn't defined in that folder's `package.json`, check the `scripts` block for the correct entry — a bridge like this is commonly launched with `npm run start`, `npm run bridge`, or by running its entry file directly, e.g. `node index.js`. On first run, most WhatsApp bridge libraries display a QR code in the terminal that you scan from your phone's WhatsApp app (Linked Devices) to authorize the connection — after that, the pairing persists across restarts.

Once the Node process is running and paired, re-enable the bridge from PRAGON's settings as in step 1 above so the two sides connect.

*Note: the exact npm script name depends on `pragon_whatsapp/package.json`, which isn't included in the architecture document this manual was built from — if `npm start` doesn't work, open that file and use whichever script it defines for launching the bridge.*

---

## 14. Custom Forge — Build Apps by Sketch or Description

Custom Forge turns an idea into a working single-file app (HTML/JS/CSS) without requiring a development environment.

**Steps:**
1. Open the Forge UI at `http://127.0.0.1:5000`.
2. Either sketch a rough layout on the drawing pad, or write a short brief describing the app you want.
3. PRAGON generates the app live. If your primary model provider is unavailable, it automatically falls back across providers, down to a local Ollama model, so generation still completes.
4. Use the built-in code editor to review, edit, save, or export the result. Undo/redo is available at every step.
5. If PRAGON's generated code has an error, ask it to fix the error directly — it will diagnose and patch the code in place.

Custom Forge also supports general website-creation workflows, not just single-purpose tools.

---

## 15. Agent Builder — Visual Automation Canvas

Agent Builder is for wiring up autonomous, multi-step agent behavior without writing code.

**Steps:**
1. Open the Agent Builder UI at `http://127.0.0.1:5057` (it runs independently of the main PRAGON process).
2. Drag tool and agent nodes onto the canvas.
3. Connect them to define a ReAct-style flow: the agent reasons about what to do, calls a tool, observes the result, and decides the next step.
4. Agent Builder runs Ollama-first, with a cloud model as backup, so most workflows can execute fully offline.

---

## 16. PRAGON Pulse — Visual & Creative Engine

Pulse handles generative visuals and 3D content:

- Generate images or 3D models from a description.
- View particle-based visual effects (built on Three.js).
- Use face-mesh tracking and AR-style viewing for design work.
- Anything generated here is automatically saved to PRAGON Gallery (Section 9).

---

## 17. Automation & Daily Briefs

Connect PRAGON to your existing calendar or an N8N workflow to unlock:

- **Deadline management** — PRAGON tracks and reminds you of upcoming deadlines.
- **Crew orchestration** — coordinate multiple workers' schedules against shared deadlines.
- **GitHub monitoring** — get notified of relevant repository activity.
- **Morning Brief** — an automatic daily summary of your schedule, tasks, and relevant updates.
- **Nightly Recap** — an end-of-day summary of what was completed and what's outstanding.

---

## 18. Desktop Control & Macro Actions

Available once you're in Master Control mode (Section 4):

- Check CPU and RAM usage: *"How's my system doing?"*
- Adjust firewall settings or access specific files by voice.
- **Record a Macro Action:** perform a repetitive multi-step task once while PRAGON records it, then say a trigger phrase any time afterward to replay the entire sequence automatically.

Because these actions touch your actual system, they are deliberately gated behind Master Control rather than available in Zen Mode.

---

## 19. Working Offline

If your internet connection drops, PRAGON is designed to keep functioning:

- Voice and conversational features fall back automatically to a local model (Ollama, Llama 3.1) rather than failing outright.
- Memory and retrieval (MOSS) never depended on the cloud in the first place — they run on your local SQLite and ChromaDB stores.
- Custom Forge and Agent Builder both have local-model fallback chains built in, so app generation and automation workflows can continue with reduced (but functional) capability.

You should notice a change in responsiveness, not a hard failure, when connectivity is lost.

---

## 20. Privacy & Safety Guidelines

- PRAGON is local-first by design: sensitive data does not need to leave your device to be useful.
- Wake-word gating means no command executes without deliberate activation — be mindful that background conversations containing the wake word could still trigger a response, so mute or disable the microphone in sensitive settings if needed.
- Master Control actions (desktop/system changes, macros, firewall edits) can affect your machine directly — only escalate to Master Control when you intend to make that kind of change, and return to Zen Mode afterward.
- Generated code (from Custom Forge or Agent Builder) runs in an isolated sandbox rather than directly against your system, but you should still review generated scripts before running them outside that sandbox.

---

## 21. Troubleshooting

| Symptom | Likely cause | What to try |
|---|---|---|
| PRAGON doesn't respond to the wake word | Microphone not detected, or wake-word engine not running | Check microphone permissions; restart `run_pragon_moss.py` |
| Voice responses feel slow | Cloud model provider unreachable and Ollama not installed | Install Ollama locally so PRAGON can fall back automatically |
| Memory recall returns nothing | MOSS database not yet populated, or `PRAGON_MOSS` disabled | Confirm `PRAGON_MOSS=1` is set; give it a session or two to build context |
| Custom Forge generation fails | All model providers (cloud and local) unreachable | Verify API keys in `api/api_keys.json` and that Ollama is running |
| Cloud model calls return an authentication error | Wrong key type in `api_keys.json` | Confirm the key starts with `AIza` (valid Gemini key format) — a key starting with `AQ` is a different credential type and will not authenticate |
| Master Control won't activate | Escalation phrase not recognized | Speak clearly: "PRAGON DADDY HOME," with the wake word first |
| WhatsApp bridge not receiving messages | Bridge not enabled, or WhatsApp node not connected | Re-enable the bridge in settings and confirm the WhatsApp module is running |

---

## 22. Frequently Asked Questions

**Does PRAGON need an internet connection to work?**
No. Voice, memory, and most tools are designed to run locally, with cloud models used only as an optional performance boost when available.

**Can PRAGON accidentally take an action I didn't ask for?**
Every action requires the wake word first, and system-level actions require the additional Master Control escalation phrase. This two-step gate is designed specifically to prevent accidental activation.

**What languages does PRAGON support?**
Language is auto-detected rather than fixed to a single setting, and includes regional languages such as Tamil across voice interaction, personas, and Persona rehearsal mode.

**Where do my reminders, logs, and past conversations live?**
Locally, in PRAGON's SQLite timeline database and ChromaDB vector store, managed exclusively through the Persistence Service.

**Can non-technical team members use Custom Forge and Agent Builder?**
Yes — both are designed as visual, description-driven tools. No coding knowledge is required to get a working result, though the code editor is available if you want to refine it further.

---

## 23. Quick Reference: Voice Command Cheat Sheet

| Say this | To do this |
|---|---|
| "Hey JARVIS" | Activate voice control |
| "PRAGON DADDY HOME" | Escalate to Master Control |
| "Back to Zen Mode" | Return to the default, restricted mode |
| "Switch to Friday / Ghost / Omnics" | Change persona |
| "What's the weather?" | Get a weather update |
| "Send a message to [name]" | Send a message |
| "Remind me to [task] at [time]" | Set a reminder |
| "Set a timer for [duration]" | Start a timer |
| "Route me to [destination]" | Get navigation directions |
| "Show me [tool/item]" | Retrieve a Gallery image |
| "What did I log [timeframe]?" | Recall from memory |
| "Start a Persona rehearsal" | Begin sales/tone rehearsal |
| "How's my system doing?" | Check CPU/RAM status |
| "Record a macro" | Begin recording a Macro Action |

---

## 24. Glossary

- **MOSS** — Modular Operating Smart System; PRAGON's sub-10ms context retrieval and memory engine.
- **Zen Mode** — PRAGON's default, restricted operating mode.
- **Master Control** — the elevated, administrative mode reached via an explicit escalation phrase.
- **Wake word** — the phrase ("Hey JARVIS") required before any command is processed.
- **Persona** — a selectable conversational voice/personality (JARVIS, Friday, Ghost, Omnics).
- **PRAGON Persona** — the dedicated rehearsal mode, distinct from the conversational personas above.
- **RAG (Retrieval-Augmented Generation)** — PRAGON's offline document search system, backed by ChromaDB.
- **Macro Action** — a recorded sequence of system actions that can be replayed with a single voice command.
- **Persistence Service** — the single gatekeeper through which all database and vector-store access happens.
---

## Architecture

```mermaid
flowchart TD
    UI[PRAGON UI / Desktop Interface] --> CORE[PRAGON Core Runtime]
    CORE --> TOOLS[Feature Tools / Plugins / Actions]
    CORE --> MEMORY[MOSS Context Engine]
    MEMORY --> STORE[(SQLite Context Store)]
    MEMORY --> RETRIEVAL[Retrieval + Guardrails]
    CORE --> VOICE[LiveKit Voice Agent]
    VOICE --> LLM[Local Model / Cloud Fallback]
    TOOLS --> SYSTEM[Desktop / Browser / RAG / Integrations]
```

The system is organized into six logical layers:

1. **UI/Client Layer** — Next.js for the shell; Vanilla JS (Web Audio API) for the high-performance audio hot path.
2. **Security & Gateway** — Auth Service (JWT) and Resource Balancer (psutil) protecting the core.
3. **Intelligence Layer** — Orchestrator (LangGraph) managing cloud and local (Ollama) models.
4. **Memory & Data Layer** — Persistence Service acting as the sole gatekeeper for SQLite and ChromaDB.
5. **Comms & Tools** — LiveKit for voice, WhatsApp for messaging, Maps for navigation.
6. **Safety & Ops** — Code Sandbox for isolation, OpenTelemetry for tracing.

### Core Subsystems

**MOSS Memory System** — adds session-aware context and grounding without replacing native PRAGON behavior. Stores session-level context to SQLite, uses `sentence-transformers` for embeddings with graceful offline fallback, and runs guardrail policy checks (`warn` or `enforce`) before risky actions.

**LiveKit Voice Agent** — a real-time voice pipeline that injects MOSS context directly into spoken input, with a fast retrieval budget (default 0.35s) and seamless bridging into the UI via room tokens.

**Custom Forge & Agent Builder** — a visual drawing pad and node-based workflow builder. Custom Forge turns a sketch or a brief into a full single-file app live, falling back across providers to a local Ollama model if needed. Agent Builder is a visual canvas for wiring up autonomous tool/agent nodes using the ReAct pattern, running Ollama-first with cloud backup, independently on port 5057.

**PRAGON Knowledge Base (RAG System)** — a standalone, fully offline retrieval-augmented generation service, powered by local ChromaDB and `sentence-transformers`, with optional local answer generation via Ollama.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js, React, Tailwind CSS, Vanilla JS (Web Audio API) |
| Backend | Python 3.11+, FastAPI, LangGraph, gRPC |
| AI/ML | Fast-tier cloud model, Ollama (Llama 3.1), Moss SDK, sentence-transformers |
| Databases | SQLite (timeline), ChromaDB (vector), Redis (caching/state) |
| Infrastructure | LiveKit (WebRTC), Docker (sandbox), HashiCorp Vault (secrets) |
| Observability | OpenTelemetry, Jaeger, LangSmith, Prometheus |

## API Specification

| Endpoint | Purpose |
|---|---|
| `POST /auth/login` | Returns a JWT |
| `GET /auth/verify` | Verifies a session token |
| `POST /v1/execute` | Streamed agent execution |
| `POST /v1/voice/signal` | Voice channel signaling |
| `GET /data/context` | MOSS-optimized context fetch |
| `POST /data/vector/search` | Vector/semantic search |
| `GET /system/health` | Returns psutil-based resource metrics |

---

## Performance Targets — By the Numbers

These figures are the engineering targets and success criteria defined in PRAGON's own PRD. They describe the bar the system is designed and tested against, not an independently verified third-party benchmark.

| Metric | Target | Component | Why it matters |
|---|---|---|---|
| Context retrieval latency | Under 10 ms | MOSS Context Engine | Feels instant — no "thinking" pause mid-conversation |
| Voice time-to-first-token | Under 400 ms | LiveKit + model pipeline | Natural back-and-forth, not a call-center hold |
| End-to-end voice latency | Under 500 ms | Full voice loop | Fast enough for dialogue, not just dictation |
| Perceived lag (P95) | Under 300 ms in 95% of interactions | Voice pipeline | The overwhelming majority of interactions feel seamless |
| Context/retrieval relevance | Above 90% relevance score | MOSS | The right answer, not just an answer |
| Local service uptime | 99.9% | Persistence + Orchestrator | Reliable enough to depend on during a shift |
| Concurrent local tool executions | Up to 50 | Resource Balancer | Scales with real multi-user, multi-tool load |
| UI frame rate under load | 60fps sustained | Cinematic UI | Smooth even during heavy inference |
| UI freezes at 100% CPU | Zero (target) | Resource Balancer | No visible failure at the worst possible moment |
| Sensitive local data sent to cloud | 0% (WAF/proxy-verified) | Privacy layer | Privacy is architectural, not a checkbox |

*A bar-chart visualization of these targets accompanies this document in the conversation where it was generated.*

## Model Temperature Guide

PRAGON tunes large-language-model sampling temperature per task category, so safety-critical actions stay predictable while creative and conversational modes stay expressive:

| Mode | Temperature | Reasoning |
|---|---|---|
| Tool calls, macros, system actions | 0.0 – 0.2 | Zero room for improvisation on real-world actions |
| MOSS-grounded RAG answers | 0.2 – 0.4 | Stays anchored to retrieved facts, minimizes hallucination |
| Everyday conversation (JARVIS default) | 0.5 – 0.7 | Natural dialogue without drifting off-topic |
| Persona rehearsal | 0.7 – 0.9 | Expressive, human-feeling variation across takes |
| Custom Forge generation | 0.6 – 0.8 | Creative UI ideas while staying syntactically reliable |
| PRAGON Pulse (image/3D generation) | 0.8 – 1.0 | Maximum visual variety and creativity |

Note: "temperature" in this context refers to the LLM sampling parameter that controls output randomness, not device hardware temperature. PRAGON's current PRD does not define a hardware thermal-telemetry component.

---

## Security & Compliance

- **Authentication:** JWT-based service identity for every internal call.
- **Authorization:** role-based access control (RBAC) on tool execution (for example, `tool:filesystem:write`).
- **Input validation:** strict Pydantic schema enforcement on every FastAPI route, to prevent injection.
- **Transport security:** TLS 1.3 enforced on all non-localhost traffic, including Phone Companion to Desktop.
- **Isolation:** the Code Sandbox uses gVisor to prevent container escape during generated-code execution.
- **Activation safety:** wake-word gating ensures no command fires without deliberate voice activation.
- **Mode separation:** Zen Mode by default; an explicit escalation phrase is required for anything administrative.

## Data & Persistence

- **Persistence boundary:** all data access is mediated by the Persistence Service. Direct filesystem access to database or vector folders by application services is strictly prohibited, closing the shared-database anti-pattern identified in architectural review.
- **Timeline DB (SQLite):** stores conversation logs, scheduler tasks, and user personas.
- **Vector store (ChromaDB):** stores embedded document chunks and semantic memory.
- **Backup:** the Backup Manager triggers snapshots through the Persistence Service API, ensuring zero-corruption recovery.

## Deployment & Infrastructure

- **Local-first:** designed to run on high-end workstations (Apple Silicon or NVIDIA RTX-class GPUs).
- **Containerization:** Docker used for the Code Sandbox and OTel backend.
- **CI/CD:** automated "Promptfoo" evaluations for prompt versioning before deployment.

## Success Metrics

- **Response fluidity:** user-perceived lag under 300ms in 95% of voice interactions.
- **Context accuracy:** MOSS retrieval relevance score above 90%.
- **System stability:** zero UI freezes during 100% CPU load, managed by the Resource Balancer.
- **Privacy:** 0% of sensitive local data leaked to cloud providers, verified by WAF/proxy monitoring.

---

## Why PRAGON Wins

| Capability | Typical cloud assistant | PRAGON |
|---|---|---|
| Works fully offline | No | Yes |
| Sub-10ms context retrieval | No | Yes |
| Local-first data privacy | No | Yes |
| Voice, maps, memory, and code builder in one tool | No | Yes |
| Wake-word action gating | Varies | Yes |
| Visual, no-code agent building | Rare | Yes |
| Multi-language support including Tamil | Varies | Yes |
| Safety sandboxing for generated code | Rare | Yes |

The advantage in one sentence: everything an on-the-move professional needs, running close enough to them that it never has to wait for permission from the internet.

## Advantages & Positives

- **Privacy by architecture, not policy** — local-first storage and retrieval mean sensitive field, health, or customer data doesn't have to leave the device to be useful.
- **Built for real-time, not "fast enough"** — sub-10ms retrieval and sub-500ms voice targets are designed for workers who cannot tolerate lag mid-task.
- **Works when the network doesn't** — automatic Ollama fallback keeps the assistant functional in remote or offline areas.
- **One platform instead of ten tools** — voice, memory, navigation, scheduling, automation, and a code/app builder live in a single runtime.
- **Safety-conscious by default** — wake-word gating, Zen Mode, guardrail policy checks, and sandboxed code execution reduce the chance of an unintended or unsafe action.
- **Genuinely multilingual** — automatic language detection, including Tamil, across voice, personas, and rehearsal mode.
- **Approachable for both audiences** — Custom Forge and Agent Builder give non-technical users a visual path and developers a full code/automation path in the same tool.
- **Enterprise-aware roadmap** — the architecture already accounts for JWT/TLS security, OpenTelemetry tracing, and a persistence boundary that avoids shared-database anti-patterns.

---

## Roadmap & Milestones

| Phase | Focus |
|---|---|
| Phase 1 — Core | Persistence Service implementation and database isolation |
| Phase 2 — Voice | LiveKit and fast-tier model integration with the Vanilla JS audio path |
| Phase 3 — Tools | Maps, WhatsApp, and Custom Forge module integration |
| Phase 4 — Hardening | OpenTelemetry tracing, JWT security, and release preparation |

## Open Questions & Risks

- **Hardware variance** — performance on mid-range laptops versus dedicated workstations is still being characterized.
- **gRPC overhead** — monitoring whether gRPC adds jitter to the sub-10ms MOSS target, particularly on Windows systems.
- **Offline parity** — ensuring the transition from a cloud model to Ollama is seamless and unnoticeable to the user.
- **Shared-database boundary** — architecture review flagged direct multi-service database access as a gap; the Persistence Service pattern above is the fix in progress.
- **Traceability matrix** — a formal functional-requirement-to-component mapping table is still to be added as a living document alongside this README.

---

## Repository Structure

```text
.
├── api/                 # API config and secrets
├── assets/              # static UI assets
├── communication/       # communication and voice helper modules
├── consciousness/       # long-term memory and retrieval helpers
├── customforge/         # Custom Forge app and Agent Builder
├── extras/              # diagnostics and helper scripts
├── features/            # app features and agent tools
├── integrations/        # external integration hooks
├── linux/               # Linux integration assets
├── plugins/             # PRAGON plugins and MOSS plugin scripts
├── pragon_brain/        # memory and local knowledge helpers
├── pragon_moss/         # MOSS integration layer
├── pragon_phoneview/    # phone / view integration
├── pragon_whatsapp/     # WhatsApp bridge module
├── pragoncore/          # core runtime utilities and protocol
├── ragsystem/           # offline retrieval / knowledge system
├── vendor/              # vendored frontend libraries
├── customforge_launcher.py
├── livekit_agent.py
├── livekit_agent_moss.py
├── livekit_client.py
├── moss.py
├── pragon_main.py
├── pragon_ui.py
├── run_pragon_moss.py
├── start_livekit_moss.py
├── requirements.txt
├── MOSS_INTEGRATION.md
├── LIVEKIT_MOSS.md
└── README.md
```

---

## Quick Start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure
cp api/api_keys.json.example api/api_keys.json
# edit api/api_keys.json with your credentials

# 3. Launch the assistant with MOSS enabled
python run_pragon_moss.py

# 4. (Optional) Launch Custom Forge & Agent Builder
cd customforge && python main.py
#   Forge UI:        http://127.0.0.1:5000
#   Agent Builder:   http://127.0.0.1:5057

# 5. (Optional) Launch the LiveKit voice agent
python start_livekit_moss.py
```

Install [Ollama](https://ollama.com) locally for free, unlimited, fully offline fallback inference. `ragsystem` and `customforge/agent_builder` maintain their own isolated `requirements.txt` files for advanced features.

## Environment Configuration

| Variable | Purpose |
|---|---|
| `PRAGON_MOSS=1` | Toggle MOSS integration |
| `PRAGON_MOSS_GUARDRAIL=warn` | Safety mode: `off` / `warn` / `enforce` |
| `PRAGON_MOSS_DB=pragon_moss.db` | MOSS database location |
| `PRAGON_MOSS_RAG=1` | Append MOSS hits to RAG queries |

---

## Documentation Index

- [MOSS Integration Guide](./MOSS_INTEGRATION.md)
- [LiveKit Voice Integration](./LIVEKIT_MOSS.md)
- [Custom Forge Documentation](./customforge/README.md)
- [Agent Builder Documentation](./customforge/agent_builder/README.md)
- [RAG System Documentation](./ragsystem/README.md)

---

<div align="center">

### Built for the people who can't afford to wait for the cloud.

**Field workers · Dispatchers · Healthcare teams · Sales teams · Developers · Companies**
One runtime, running close to the people who need it most.

</div>
