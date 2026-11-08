"""
Shared constants and tunable configuration for the GMS evaluator.

Everything a staff member is likely to tune lives here:
- the occupancy model timings (PREP / GROUND / buffer)
- the scoring WEIGHTS

Gate-type and flight-category codes are intentionally the same integer space
so the asymmetric compatibility rule can be expressed cleanly:
    0 = cargo, 1 = domestic, 2 = international
"""

HOME = "YYZ"

# Flight-category / gate-type codes (shared space).
CARGO = 0
DOMESTIC = 1
INTERNATIONAL = 2

CATEGORY_NAME = {CARGO: "cargo", DOMESTIC: "domestic", INTERNATIONAL: "international"}

# ---------------------------------------------------------------------------
# Occupancy model (all values in minutes)
# ---------------------------------------------------------------------------
# A flight occupies its gate for each contiguous stretch it is physically at
# YYZ ("presence interval"). These constants size the two open-ended cases:
#
#   - PREP_MINUTES   : how long a gate is held *before* an originating
#                      departure (a flight that starts at YYZ).
#   - GROUND_MINUTES : how long a gate is held *after* a terminating arrival
#                      (an inbound flight with no later YYZ departure).
#
# A turnaround flight (arrive YYZ then later depart YYZ) uses the real
# [arrival, departure] window and ignores these constants.
PREP_MINUTES = 60
GROUND_MINUTES = 60

# Extra buffer required between two *different* aircraft at the same gate.
# 0 means exact back-to-back is allowed (intervals are half-open).
TURNAROUND_BUFFER = 0

# ---------------------------------------------------------------------------
# Scoring weights (lower total score is better; hard failures are separate)
# ---------------------------------------------------------------------------
WEIGHTS = {
    "reassignment": 10,                 # moving an assigned flight (solution's choice)
    "reassignment_after_arrival": 50,   # moving a flight whose gate presence has begun
    "domestic_at_intl_gate": 5,         # allowed, but wastes a premium (customs) gate
    "walking_distance_per_m": 0.01,     # summed gate.dist over final assignments
    "unassigned_flight": 100,           # a YYZ flight left with no gate at sim end
    "unassigned_emergency": 300,        # a diverted/emergency flight left with no gate
}
