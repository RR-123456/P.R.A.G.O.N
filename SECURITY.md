# PRAGON — Security & Capability Notes

PRAGON is a local-first AI assistant that can see your screen, hear your mic,
move your mouse/keyboard, run generated code, and talk to your OS. Those are
also the exact behaviors antivirus heuristics are trained to flag as a RAT
(remote access trojan). This document exists so that:

1. **You** know what's actually running and why, before you trust it.
2. **You** can paste this into an AV false-positive report if a scanner flags
   the app, since most vendors ask "what does this software actually do?"

Nothing below was found to phone home, exfiltrate data, or hide its own
behavior from you — see the audit summary at the bottom. That doesn't make
these capabilities risk-free; it means the risk is the *intended* feature,
not a hidden one. Review anything you're not comfortable with before enabling
it.

## Capabilities and how each is gated

| Capability | Where | Gate |
|---|---|---|
| Hides subprocess console windows | `pragon_main.py` (global `subprocess.Popen` patch) | Always on, Windows only. Cosmetic (stops console flashes from ~200 internal shell-outs), not a hiding-from-you mechanism — every action it wraps is one you triggered. |
| Mouse/keyboard control | `pyautogui`, `pynput` throughout `features/` | Only invoked in response to a request you made (voice/text/agent), never in the background. |
| Screen capture | `mss`, `opencv-python` | Same — request-triggered, not continuous/background. |
| Microphone / wake word | `pragoncore/wake_word.py` | **Opt-in.** Off by default; you enable it explicitly from the topbar. |
| Camera (Pulse module) | `pragon_ui.py`, browser `getUserMedia` | Browser-level permission prompt, per-origin. |
| Location (Compass module) | `pragon_ui.py`, browser `navigator.geolocation` | Browser-level permission prompt, per-origin. |
| Clipboard read | `pragon_main.py::_clipboard_watch_loop` | **Opt-in**, off by default. Read-only — broadcasts to PRAGON's own local UI only, never written back, never sent externally. |
| Run-at-Windows-login | `pragon_main.py::_toggle_autostart` | **Opt-in.** Writes one named (`PRAGON_AI`) value to your own `HKCU` registry hive — never `HKLM`, never disguised. |
| Generated-code execution | `features/feature/desktop.py::_execute_generated_code` | Runs through a restricted builtins sandbox (no `import`, no `subprocess`, no file deletion, no `exec`/`eval` inside the generated code itself — enforced by the prompt AND the sandbox's available names). Classified **high-risk** by `dry_run.py` and previewed before execution when a confirmation callback is wired in. |
| Shell commands (`cmd_control`) | `features/agentic_feature/` | Classified **high-risk** by `dry_run.py`, previewed before execution. |
| Destructive file ops (delete/overwrite/move) | `file_controller` | Classified **medium-risk**, previewed before execution; low-risk ops (read/list/open) stay unguarded on purpose so the assistant isn't annoying for normal use. |
| Shutdown/restart/Wi-Fi-off | `pragoncore/confirm_gate.py` | Requires a UI-issued confirmation token the model itself cannot forge — see that file's docstring for why the older `confirmed=yes` parameter pattern was insufficient. |
| Plugin loading (`plugins/*.py`) | `pragoncore/plugin_loader.py` | **Known limitation, partially mitigated (see below).** |
| "Full Projects" zip install | `pragoncore/integration_manager.py` | Each installed project gets its own isolated `venv`/`node_modules` and runs as a separate OS process — a crash or bug there can't take down PRAGON itself. It is still, by design, a general-purpose project runner: only install zips from sources you trust. |

## Known limitation: plugin upload

`discover_plugins()` has to `import` (i.e. execute) every `.py` file in
`plugins/` just to read its `PLUGIN` metadata dict — Python doesn't offer a
way to inspect a module's top-level contract without running its top-level
code first. That means **a plugin's own module-level code always runs once**,
the moment it's discovered, before anything checks whether it's well-formed.

What this build does about it: a plugin uploaded through Settings →
Integration is now **forced disabled immediately after that one-time scan**
(see `pragon_main.py::_plugin_upload`), so even though its import already
ran, its `run()` function — the actual voice-callable capability — cannot
fire until you've reviewed the file and switched it on yourself in Settings.
Previously, uploaded plugins were enabled automatically the moment they
validated.

This does **not** fully close the gap — a plugin file can still do damage
in whatever ran at import time, before you ever see the "review it" message.
**Only drop `.py` files into `plugins/`, or upload them through the UI, if
you wrote them yourself or trust the source as much as you'd trust any other
Python script you're about to run.**

## What was checked and found clean

A manual source review (not a signature scan) found:
- No `eval`/`exec` on untrusted or remote input outside the one documented
  sandbox above.
- No hardcoded C2-style destinations, Discord/Telegram webhooks, pastebin,
  or tunneling services — every outbound URL is a named, purpose-matched
  public service (search engines, YouTube, weather/satellite/earthquake
  APIs, the LLM providers you configure, etc.).
- No clipboard-hijacking (nothing ever *writes* to the clipboard except in
  direct response to an explicit "copy this" action you triggered).
- No disguised or silent persistence — the one autostart mechanism is
  opt-in, per-user, and clearly named.

This is a manual code review, not a substitute for actual antivirus/AV
signature scanning or a sandboxed dynamic analysis — it can't see inside
compiled/packaged binaries or catch something a human reviewer missed. If
you build this into a `.exe` (e.g. via PyInstaller), expect AV false
positives regardless of the above — unsigned, freshly-built Python
executables are commonly flagged industry-wide, independent of what the
code actually does. Code-signing the build is the real long-term fix for
that specific problem.
