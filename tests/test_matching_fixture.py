import json
import unittest
from datetime import datetime
from pathlib import Path


def flight_key(message):
    leg = message["legs"][0]
    return (message["flight"]["carrier"], leg["flightNumber"], leg.get("flightSuffix"),
            leg["originatingDate"], leg["departureStation"], leg["arrivalStation"])


class MatchingFixtureTest(unittest.TestCase):
    def test_initial_schedule_and_updates_are_consistent(self):
        path = Path(__file__).resolve().parents[1] / "fixtures" / "pearson-400-flights-seed42.json"
        scenario = json.loads(path.read_text())
        events = scenario["events"]
        initial = events[0]["message"]
        self.assertEqual(initial["action"], "InitializeAirport")
        self.assertEqual(scenario["metadata"]["flight_count"], 400)
        self.assertEqual(len(initial["flights"]), 400)
        baseline = {flight_key(message): message for message in initial["flights"]}
        self.assertEqual(len(baseline), 400)
        self.assertEqual({m["action"] for m in initial["flights"]}, {"CreateFlight"})
        self.assertEqual(initial["gateOutages"], [])
        parked = {a["registration"] for a in initial["parkedAircraft"]}
        self.assertEqual(len(parked), len(initial["parkedAircraft"]))
        self.assertEqual(len(parked), 25)
        timestamps = [datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")) for event in events]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual({time.utcoffset().total_seconds() for time in timestamps}, {0})
        cancelled, early, delayed = set(), 0, 0
        for event in events[1:]:
            message = event["message"]
            action = message["action"]
            if action == "GateOutage":
                continue
            self.assertIn(action, {"UpdateTiming", "ReplaceFlight", "UpdateEquipment", "CancelFlight", "ReinstateFlight"})
            key = flight_key(message)
            self.assertIn(key, baseline)
            if action == "UpdateEquipment":
                self.assertIn(message["flight"]["registration"], parked)
            elif action == "CancelFlight":
                self.assertNotIn(key, cancelled)
                cancelled.add(key)
            elif action == "ReinstateFlight":
                self.assertIn(key, cancelled)
                cancelled.remove(key)
            elif action == "UpdateTiming":
                old, new = baseline[key]["legs"][0], message["legs"][0]
                early += new["scheduledArrivalTime"] < old["scheduledArrivalTime"]
                delayed += new["scheduledDepartureTime"] > old["scheduledDepartureTime"]
        self.assertGreater(early, 0)
        self.assertGreater(delayed, 0)


if __name__ == "__main__":
    unittest.main()
