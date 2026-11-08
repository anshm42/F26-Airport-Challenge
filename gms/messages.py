"""
Flight-message lifecycle (the simplified AODB feed).

A ``FlightStore`` holds the current canonical state of every flight and applies
incoming messages. Unlike the original evaluator, this implements the full
message set correctly, including the paths that were previously non-functional:

  - UpdateEquipment (AllLegs / SpecificLeg) -- the old parser only read
    ``msg["legs"]`` and so silently wiped the flight on these messages.
  - CancelFlight SpecificLeg               -- previously ignored entirely.
  - UpdateTiming                            -- now merges times onto the stored
                                               legs instead of replacing them.

State per flight::

    {
        "flight_id": str,
        "carrier", "flight_number", "suffix", "originating_date": str,
        "legs": [ {legType, aircraftType, departureStation, arrivalStation,
                   scheduledDepartureTime, scheduledArrivalTime}, ... ],
        "cancelled": bool,                 # whole-flight cancellation
        "cancelled_legs": set[(dep, arr)], # specific-leg cancellations
        "info_time": int,                  # minute this state last changed
        "action": str,                     # last action applied
    }
"""

from .timeutil import hhmm_str_to_min, absolutize

_LEG_FIELDS = (
    "legType", "aircraftType", "departureStation", "arrivalStation",
    "scheduledDepartureTime", "scheduledArrivalTime",
)

# A gate outage with no end time is treated as lasting the rest of the horizon.
OUTAGE_OPEN_END = 7 * 24 * 60


def make_flight_id(carrier: str, flight_number: str, originating_date: str, suffix=None) -> str:
    fn = flight_number + (suffix if suffix else "")
    return f"{carrier}-{fn}-{originating_date}"


def parse_gate_outage(msg: dict):
    """
    Parse a ``GateOutage`` resource message into (gate_id, (start_min, end_min)).

    This is not a flight message and never touches the FlightStore; it marks a
    gate unavailable for a time window (overnight windows roll over). A missing
    end time means "until further notice".
    """
    gid = msg.get("gate")
    start = hhmm_str_to_min(msg["scheduledStartTime"])
    end_raw = msg.get("scheduledEndTime")
    if end_raw is None:
        return gid, (start, OUTAGE_OPEN_END)
    start_abs, end_abs = absolutize([start, hhmm_str_to_min(end_raw)])
    return gid, (start_abs, end_abs)


def _norm_leg(leg: dict) -> dict:
    return {k: leg.get(k) for k in _LEG_FIELDS}


class FlightStore:
    def __init__(self):
        self.flights: dict[str, dict] = {}

    # -- helpers -----------------------------------------------------------
    def _new_state(self, fid, meta, legs, info_time, action) -> dict:
        return {
            "flight_id": fid,
            "carrier": meta.get("carrier", ""),
            "flight_number": meta.get("flightNumber", ""),
            "suffix": meta.get("flightSuffix"),
            "originating_date": meta.get("originatingDate", ""),
            "legs": [_norm_leg(l) for l in legs],
            "cancelled": False,
            "cancelled_legs": set(),
            "diversion": False,
            "reason": None,
            "info_time": info_time,
            "action": action,
        }

    # -- entry point -------------------------------------------------------
    def apply(self, msg: dict, info_time: int):
        """
        Apply one message. Returns (flight_id | None, warning | None).
        flight_id is None when the message was ignored.
        """
        action = msg.get("action")
        meta = msg.get("flight", {})
        fid = make_flight_id(
            meta.get("carrier", ""), meta.get("flightNumber", ""),
            meta.get("originatingDate", ""), meta.get("flightSuffix"),
        )

        if action == "CreateFlight":
            warn = None
            if fid in self.flights:
                warn = f"CreateFlight for already-known {fid}; treated as replace."
            self.flights[fid] = self._new_state(fid, meta, msg.get("legs", []), info_time, action)
            return fid, warn

        if action == "ReplaceFlight":
            self.flights[fid] = self._new_state(fid, meta, msg.get("legs", []), info_time, action)
            return fid, None

        if action == "DivertFlight":
            # An unscheduled inbound (medical diversion, air turnback, ...). Created
            # like a flight but flagged so the solution can prioritise it and the
            # scorer can penalise leaving it unplaced more heavily.
            state = self._new_state(fid, meta, msg.get("legs", []), info_time, action)
            state["diversion"] = True
            state["reason"] = msg.get("reason", "diversion")
            self.flights[fid] = state
            return fid, None

        if action == "UpdateTiming":
            return self._update_timing(fid, msg, info_time)

        if action == "CancelFlight":
            return self._cancel(fid, msg, info_time)

        if action == "ReinstateFlight":
            return self._reinstate(fid, info_time)

        if action == "UpdateEquipment":
            return self._update_equipment(fid, msg, info_time)

        return None, f"Unknown action '{action}' (ignored)."

    # -- per-action handlers ----------------------------------------------
    def _update_timing(self, fid, msg, info_time):
        state = self.flights.get(fid)
        if state is None:
            return None, f"UpdateTiming for unknown flight {fid} (ignored)."

        # Update only times, matching provided legs to stored legs by route.
        for new_leg in msg.get("legs", []):
            route = (new_leg.get("departureStation"), new_leg.get("arrivalStation"))
            match = next(
                (l for l in state["legs"]
                 if (l["departureStation"], l["arrivalStation"]) == route),
                None,
            )
            if match is not None:
                match["scheduledDepartureTime"] = new_leg.get("scheduledDepartureTime", match["scheduledDepartureTime"])
                match["scheduledArrivalTime"] = new_leg.get("scheduledArrivalTime", match["scheduledArrivalTime"])
        state["info_time"] = info_time
        state["action"] = "UpdateTiming"
        return fid, None

    def _cancel(self, fid, msg, info_time):
        state = self.flights.get(fid)
        if state is None:
            return None, f"CancelFlight for unknown flight {fid} (ignored)."

        scope = msg.get("cancelScope", {}) or {}
        state["info_time"] = info_time
        state["action"] = "CancelFlight"

        if scope.get("type") == "SpecificLeg":
            route = (scope.get("departureStation"), scope.get("arrivalStation"))
            state["cancelled_legs"].add(route)
            remaining = [
                l for l in state["legs"]
                if (l["departureStation"], l["arrivalStation"]) not in state["cancelled_legs"]
            ]
            if not remaining:
                state["cancelled"] = True
        else:  # AllLegs (default)
            state["cancelled"] = True
        return fid, None

    def _reinstate(self, fid, info_time):
        state = self.flights.get(fid)
        if state is None:
            return None, f"ReinstateFlight for unknown flight {fid} (ignored)."
        state["cancelled"] = False
        state["cancelled_legs"] = set()
        state["info_time"] = info_time
        state["action"] = "ReinstateFlight"
        return fid, None

    def _update_equipment(self, fid, msg, info_time):
        state = self.flights.get(fid)
        if state is None:
            return None, f"UpdateEquipment for unknown flight {fid} (ignored)."

        upd = msg.get("equipmentUpdate", {}) or {}
        new_ac = upd.get("aircraftType")
        if not new_ac:
            return fid, "UpdateEquipment with no aircraftType (ignored)."

        if upd.get("scope") == "SpecificLeg":
            route = (upd.get("departureStation"), upd.get("arrivalStation"))
            for leg in state["legs"]:
                if (leg["departureStation"], leg["arrivalStation"]) == route:
                    leg["aircraftType"] = new_ac
        else:  # AllLegs (default)
            for leg in state["legs"]:
                leg["aircraftType"] = new_ac

        state["info_time"] = info_time
        state["action"] = "UpdateEquipment"
        return fid, None
