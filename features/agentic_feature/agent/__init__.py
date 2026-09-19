# features/agentic_feature/agent/
#
# Planner → Executor → ErrorHandler → TaskQueue orchestration layer
# ("MARK XXV" multi-step autonomous task agent).
#
# This lets a single high-level goal ("build me a scraper and email me
# the results") be broken into steps across ALL of Pragon's existing
# tools (open_app, web_search, file_controller, pragon_fileforger,
# code_helper, etc. — see actions/), executed one at a time, with
# automatic retry / replanning / skip / abort on failure.
