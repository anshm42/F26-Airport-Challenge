import json
import unittest
from datetime import datetime
from pathlib import Path

from smartgate_naive.planner import NaiveMatcher
from smartgate_stream.gates import GateState
from smartgate_stream.inventory import catalog
from smartgate_stream.validation import load_scenario, parse_utc


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pearson-400-flights-seed42.json"


def closed_airport(start="10:00", end="12:00"):
    """The 400-flight day with every passenger position out of service for two hours."""
    scenario = load_scenario(FIXTURE)
    stands = {}
    for gate, names in catalog()["gate_stands"].items():
        stands.setdefault(catalog()["footprints"][gate], names[0])
    outages = [{"timestamp": scenario["events"][1]["timestamp"],
                "message": {"action": "GateOutage", "gate": name, "scheduledStartTime": start, "scheduledEndTime": end}}
               for name in stands.values()]
    scenario["events"][1:1] = outages
    return scenario


def replay(scenario):
    """Feed every event to the naive matcher and apply its answers with the stream's own validator."""
    stream, naive = GateState(), NaiveMatcher()
    for event in scenario["events"]:
        timestamp, message = parse_utc(event["timestamp"]), event["message"]
        if message["action"] != "InitializeAirport":
            while (when := stream.next_movement()) is not None and when <= timestamp:
                stream.advance(when)
        stream.apply_message(message, timestamp)
        response = naive.handle(json.loads(json.dumps(stream.request(message))), event["timestamp"])
        stream.apply_decisions(response, initializing=message["action"] == "InitializeAirport")
        yield message, response, naive, stream


class NaiveMatcherTest(unittest.TestCase):
    def test_every_decision_passes_stream_validation(self):
        steps = list(replay(load_scenario(FIXTURE)))
        message, response, naive, stream = steps[0]
        self.assertEqual(len(response["assignments"]), 400)
        self.assertEqual(naive.waitlist, [])
        self.assertEqual(len(stream.snapshot()["planned_assignments"]), 400)
        self.assertEqual(len(steps), 159)

    def test_waitlisted_visits_are_retried_until_placed(self):
        waited, peak, moves = {}, 0, 0
        for message, response, naive, stream in replay(closed_airport()):
            peak = max(peak, len(naive.waitlist))
            moves += len(response["moves"])
            for vid in naive.waitlist:
                waited.setdefault(vid, naive.waitlisted_since[vid])
        self.assertGreater(peak, 10)
        self.assertGreater(moves, 0)  # aircraft that landed during the closure are towed in afterwards
        self.assertEqual(naive.waitlist, [])
        self.assertTrue(all(naive.assigned_at[vid] >= since for vid, since in waited.items() if vid in naive.assigned_at))

    def test_board_reports_waitlist_and_assignment_times(self):
        for message, response, naive, stream in replay(closed_airport()):
            if naive.waitlist:
                break
        board = naive.board()
        self.assertEqual(datetime.fromisoformat(board["generatedAt"]), naive.state.now)
        waiting = [flight for flight in board["flights"] if flight["gate"] is None and not flight["cancelled"]]
        self.assertTrue(waiting)
        self.assertTrue(all(flight["waitlistedSince"] for flight in waiting))
        assigned = [flight for flight in board["flights"] if flight["gate"]]
        self.assertTrue(all(flight["assignedAt"] and flight["waitlistedSince"] is None for flight in assigned))

    def test_diversion_policy_sets_priority_or_remote_stands(self):
        message, response, naive, stream = next(replay(load_scenario(FIXTURE)))
        vid = next(vid for vid, visit in naive._visits(naive.state.now).items() if not visit["current"])
        for policy, priority, remote_ok in ((("Turnback", ""), True, False), (("Emergency", ""), False, True),
                                            ((None, "medical"), False, True), ((None, "weather"), True, False)):
            naive.policies[vid] = policy
            visit = naive._visits(naive.state.now)[vid]
            self.assertEqual((visit["priority"], visit["remote_ok"]), (priority, remote_ok), policy)


if __name__ == "__main__":
    unittest.main()
