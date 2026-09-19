# The full build-a-whole-project logic already lives in
# features/agentic_feature/pragon_agent.py (wired into pragon_main.py as
# the "pragon_agent" tool, with `pragon_fileforger` kept as a backwards
# compatible alias there). No need to duplicate it — just re-export it
# under the name the planner/executor expect.
from features.agentic_feature.pragon_agent import pragon_agent_action as pragon_fileforger

__all__ = ["pragon_fileforger"]
