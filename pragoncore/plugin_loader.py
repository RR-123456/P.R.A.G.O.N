"""
Plugin discovery, validation, collision detection, and dispatch.

Ported from the Mark-LI project's `core/plugin_loader.py` and adapted to Pragon:
drop a single `.py` file into the top-level `plugins/` folder and Pragon learns
a new voice-callable skill on the next launch — no changes to pragon_main.py.

Discovery runs once (JarvisLive.__init__ calls discover_plugins()); the resulting
PluginRegistry is cached for the process lifetime. Enable/disable state is re-read
from config on every call to get_tool_declarations() / run() / list_for_ui(), so
toggling a plugin does not require restarting the app or re-importing anything.

Safety: a broken or badly written plugin can never crash Pragon — it is reported
as invalid (with the error) and every other tool/plugin keeps working. Name
collisions with core tools (or other plugins) are detected and rejected.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from consciousness.config_manager import get_plugin_enabled, get_plugin_config

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}


@dataclass
class PluginRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    run: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""
    settings: Optional[dict] = None  # optional PLUGIN_SETTINGS schema (config fields) — ported from Mark-LIII


class PluginRegistry:
    def __init__(self, plugins: dict[str, PluginRecord], logger: Callable[[str], None]):
        self._plugins = plugins          # name -> PluginRecord, VALID entries only
        self._all_records: list[PluginRecord] = []   # valid + invalid, for UI listing
        self._logger = logger

    # -- called at LiveConnectConfig build time --
    def get_tool_declarations(self) -> list[dict]:
        decls = []
        for name, rec in self._plugins.items():
            if get_plugin_enabled(name):
                decls.append({
                    "name": rec.name,
                    "description": rec.description,
                    "parameters": rec.parameters,
                })
        return decls

    def has(self, name: str) -> bool:
        return name in self._plugins

    # -- called from _execute_tool's fallback branch --
    def run(self, name: str, parameters: dict, player=None, session_memory=None) -> str:
        rec = self._plugins.get(name)
        if rec is None or not rec.valid:
            return f"Plugin '{name}' is not available."
        if not get_plugin_enabled(name):
            return f"The '{name}' plugin is currently disabled."
        try:
            return _call_run(rec.run, parameters, player, session_memory) or "Done."
        except Exception as e:
            self._logger(f"Plugin '{name}' crashed during run(): {e}")
            traceback.print_exc()
            return f"Sir, the '{name}' plugin failed: {e}"

    # -- used right after an upload, to find the record for the file just
    # written to disk so the caller can force it disabled-by-default; see
    # _plugin_upload in pragon_main.py --
    def find_by_file(self, filename: str) -> Optional[PluginRecord]:
        for rec in self._all_records:
            if rec.file == filename:
                return rec
        return None

    # -- for a future Plugin Manager panel in the UI --
    def list_for_ui(self) -> list[dict]:
        out = []
        for rec in self._all_records:
            out.append({
                "name": rec.name,
                "description": rec.description,
                "file": rec.file,
                "valid": rec.valid,
                "error": rec.error,
                "enabled": get_plugin_enabled(rec.name) if rec.valid else False,
            })
        return out

    # -- ported from Mark-LIII: called by the Settings > Integration panel to
    # render per-plugin config forms --
    def settings_schemas(self) -> list[dict]:
        """One entry per settings SECTION, for enabled plugins that declare a
        PLUGIN_SETTINGS schema. Sections are deduped by namespace so a suite of
        plugins sharing one namespace (e.g. a printer trio) shows a single
        form. Current stored values are merged in so the UI can pre-fill fields.
        """
        seen: set[str] = set()
        out: list[dict] = []
        for name, rec in self._plugins.items():
            if not rec.settings or not get_plugin_enabled(name):
                continue
            ns = rec.settings.get("namespace") or rec.name
            if ns in seen:
                continue
            seen.add(ns)
            out.append({
                "plugin": rec.name,
                "namespace": ns,
                "title": rec.settings.get("title") or rec.name,
                "fields": rec.settings.get("fields", []),
                "values": get_plugin_config(ns),
                "action": rec.settings.get("action"),  # optional test/connect button
            })
        return out


def _call_run(run_fn, parameters, player, session_memory):
    """Invoke run() passing only the kwargs it actually declares (or all of them
    if it has **kwargs), so a minimal `def run(parameters):` plugin still works."""
    sig = inspect.signature(run_fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    if has_var_kw or "player" in sig.parameters:
        kwargs["player"] = player
    if has_var_kw or "session_memory" in sig.parameters:
        kwargs["session_memory"] = session_memory
    return run_fn(parameters, **kwargs)


def _validate(module, filename: str) -> PluginRecord:
    """Returns a PluginRecord; .valid=False + .error set on any problem. Never raises."""
    plugin_meta = getattr(module, "PLUGIN", None)
    if not isinstance(plugin_meta, dict):
        return PluginRecord(name=Path(filename).stem, file=filename,
                             error="Missing PLUGIN dict constant.")

    name = plugin_meta.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return PluginRecord(name=str(name or Path(filename).stem), file=filename,
                             error="PLUGIN['name'] missing or not a valid identifier "
                                   "(letters/digits/underscore, must start with letter/underscore).")

    description = plugin_meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['description'] missing or empty.")

    parameters = plugin_meta.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['parameters'] must be a dict with \"type\": \"OBJECT\".")

    run_fn = getattr(module, "run", None)
    if not callable(run_fn):
        return PluginRecord(name=name, file=filename,
                             error="Missing callable run(parameters, ...) function.")

    # Optional PLUGIN_SETTINGS dict — ported from Mark-LIII. Lets a plugin expose
    # a config form (e.g. an API key field) in Settings > Integration without any
    # core file changes. Malformed schemas are ignored, never rejected outright.
    settings_schema = getattr(module, "PLUGIN_SETTINGS", None)
    if not isinstance(settings_schema, dict):
        settings_schema = None

    return PluginRecord(name=name, description=description.strip(), parameters=parameters,
                         run=run_fn, file=filename, valid=True, error="", settings=settings_schema)


def save_uploaded_plugin(plugins_dir: Path, filename: str, data: bytes) -> tuple[bool, str]:
    """
    Persist an uploaded plugin file into plugins_dir so the NEXT discover_plugins()
    call (the Integration tab always triggers a rescan right after a successful
    upload) will pick it up. Never touches anything outside plugins_dir.

    Returns (True, saved_filename) on success, (False, error_message) on rejection.
    Validation only checks the things that would make the file unsafe or
    un-discoverable — the full PLUGIN dict/run() contract is still checked by
    _validate() during the following discover_plugins() call, so a file that
    fails that check simply shows up as "rejected" in list_for_ui(), same as
    any hand-written plugin with a mistake in it.
    """
    name = Path(filename).name  # strips any directory component / traversal (../)
    if not name or name != filename.replace("\\", "/").rsplit("/", 1)[-1]:
        return False, "Invalid filename."
    if not name.lower().endswith(".py"):
        return False, "Only .py files can be docked as plugins."
    if name.startswith("_"):
        return False, "Plugin filenames can't start with '_' (reserved for templates/helpers)."
    if not _NAME_RE.match(Path(name).stem):
        return False, "Filename must be a valid identifier (letters/digits/underscore)."
    if len(data) > 2_000_000:
        return False, "File is too large for a plugin (2MB limit)."

    plugins_dir.mkdir(parents=True, exist_ok=True)
    dest = plugins_dir / name
    try:
        dest.write_bytes(data)
    except Exception as e:
        return False, f"Failed to save file: {e}"
    return True, name


def delete_plugin_file(plugins_dir: Path, filename: str) -> tuple[bool, str]:
    """Remove a previously-docked plugin file. Refuses anything outside plugins_dir
    or template/helper files (leading underscore)."""
    name = Path(filename).name
    if not name or name != filename.replace("\\", "/").rsplit("/", 1)[-1]:
        return False, "Invalid filename."
    if name.startswith("_"):
        return False, "Refusing to delete a template/helper file."
    target = plugins_dir / name
    try:
        if not target.exists():
            return False, f"'{name}' not found."
        target.unlink()
        return True, f"'{name}' removed."
    except Exception as e:
        return False, f"Failed to delete '{name}': {e}"


def discover_plugins(plugins_dir: Path, core_tool_names: set[str],
                      logger: Callable[[str], None] = print) -> PluginRegistry:
    """
    Scans plugins_dir for *.py files (skips files starting with '_', e.g. __init__.py,
    _template.py, and any shared-helper modules an author prefixes with '_').
    Import errors, validation errors, and name collisions are logged and the offending
    file is skipped — they NEVER raise out of this function and never abort the scan
    of remaining files.
    """
    plugins_dir.mkdir(parents=True, exist_ok=True)
    valid: dict[str, PluginRecord] = {}
    all_records: list[PluginRecord] = []

    files = sorted(plugins_dir.glob("*.py"), key=lambda p: p.name)  # deterministic order
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            module_name = f"plugins.{path.stem}"
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("could not build import spec")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(module_name, None)
                raise

            rec = _validate(module, path.name)

            if rec.valid and rec.name in core_tool_names:
                rec = PluginRecord(name=rec.name, file=path.name,
                                    error=f"Name '{rec.name}' collides with a core tool — rejected.")
            elif rec.valid and rec.name in valid:
                other = valid[rec.name].file
                rec = PluginRecord(name=rec.name, file=path.name,
                                    error=f"Name '{rec.name}' already used by plugin '{other}' — rejected.")

        except Exception as e:
            rec = PluginRecord(name=path.stem, file=path.name,
                                error=f"Failed to load: {e}")
            traceback.print_exc()

        all_records.append(rec)
        if rec.valid:
            valid[rec.name] = rec
            logger(f"Plugin loaded: {rec.name} ({path.name})")
        else:
            logger(f"Plugin rejected: {path.name} — {rec.error}")

    registry = PluginRegistry(valid, logger)
    registry._all_records = all_records
    logger(f"Plugin discovery complete: {len(valid)} active, "
           f"{len(all_records) - len(valid)} rejected, {len(all_records)} total.")
    return registry
