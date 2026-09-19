# features/agentic_feature/actions/
#
# Thin adapter layer the agent/executor.py imports from. Most of these
# just re-export the tool that already exists under features/feature/
# (or features/agentic_feature/) so the planner/executor system can
# call every tool Pragon already has, under one consistent import path.
#
# pragon_fileforger.py and cmd_control.py are the two genuinely new
# pieces here — everything else is a re-export shim.
