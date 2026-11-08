"""
Gate Management System (GMS) evaluator package.

Modules:
- config           : shared constants and the tunable scoring weights
- timeutil         : time-of-day parsing and monotonic absolute-minute helpers
- occupancy        : per-presence gate-occupancy intervals + conflict detection
- compat           : flight classification + asymmetric gate compatibility
- profile          : combine the above into a per-flight profile from raw state
- messages         : full flight-message lifecycle (the AODB feed)
- scoring          : hard-failure model + weighted numeric score
- reference_solver : offline feasibility checker / reference assigner

The public entry point is ``evaluator.py`` at the project root.
"""
