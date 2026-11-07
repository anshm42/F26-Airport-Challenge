import copy
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from smartgate_stream.__main__ import main
from smartgate_stream.generate import generate
from smartgate_stream.validation import ACTIONS, load_scenario, parse_utc


ROOT = Path(__file__).resolve().parents[1]
TORONTO = ZoneInfo("America/Toronto")


def flight_key(message):
    leg = message["legs"][0]
    return (message["flight"]["carrier"], leg["flightNumber"], leg.get("flightSuffix"), leg["originatingDate"])


class GeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profiles = json.loads((ROOT / "data" / "stream_profiles.json").read_text())
        cls.pearson = json.loads((ROOT / "data" / "pearson.json").read_text())
        cls.fleet = json.loads((ROOT / "data" / "opensky_fleet_2025.json").read_text())
        cls.scenario = generate(42, "2026-11-07")

    def test_reproducibility_and_input_boundaries(self):
        self.assertEqual(generate(42, "2026-11-07"), self.scenario)
        self.assertNotEqual(generate(43, "2026-11-07"), self.scenario)
        for count in (1, 2, 3, 23, 24, 25):
            with self.subTest(count=count):
                scenario = generate(1, "2026-11-07", count)
                self.assertEqual(sum(e["message"]["action"] == "CreateFlight" for e in scenario["events"]), count)
        for seed, date, count in ((True, "2026-11-07", 1), (1, "2026-02-29", 1),
                                  (1, "20261107", 1), (1, None, 1),
                                  (1, "2026-11-07", 0), (1, "2026-11-07", True),
                                  (1, "2026-11-07", 10000)):
            with self.subTest(seed=seed, date=date, count=count), self.assertRaises(ValueError):
                generate(seed, date, count)

    def test_rate_changes_only_replay_metadata_and_rejects_invalid_numbers(self):
        for rate in (0.5, 1, 60, 3600):
            with self.subTest(rate=rate):
                scenario = generate(42, "2026-11-07", rate=rate)
                self.assertEqual(scenario["metadata"]["rate"], rate)
                self.assertEqual(scenario["events"], self.scenario["events"])
        for rate in (0, -1, True, None, "60", float("nan"), float("inf"), 10**400):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                generate(42, "2026-11-07", rate=rate)

    def test_announcements_have_irregular_gaps_second_precision_and_bursts(self):
        events = self.scenario["events"]
        timestamps = [parse_utc(event["timestamp"]) for event in events]
        gaps = [(following - current).total_seconds() for current, following in zip(timestamps, timestamps[1:])]
        self.assertIn(0, gaps)
        self.assertGreater(len(set(gaps)), 10)
        for action in ACTIONS:
            with self.subTest(action=action):
                times = [parse_utc(e["timestamp"]) for e in events if e["message"]["action"] == action]
                self.assertTrue(any(t.second != 0 for t in times))

    def test_profiles_use_pearson_carriers_known_aircraft_and_airports(self):
        carriers = {r["carrier"] for r in self.pearson["airlineTerminals"]["records"]}
        aircraft = json.loads((ROOT / "data" / "aircraft.json").read_text())["aircraftTypes"]["records"]
        types = {r["aircraftType"] for r in aircraft if "Passenger" in r["serviceTypes"]}
        for profile in self.profiles["carriers"]:
            self.assertIn(profile["carrier"], carriers)
            for service in profile["services"]:
                self.assertLessEqual(set(service["aircraft"]), types)
                for station in service["stations"]:
                    self.assertIn(station, self.profiles["airports"])
        for airport in self.profiles["airports"].values():
            ZoneInfo(airport["timeZone"])

    def test_default_has_diverse_traffic_and_no_assignments(self):
        events = self.scenario["events"]
        messages = [e["message"] for e in events]
        created = [m for m in messages if m["action"] == "CreateFlight"]
        self.assertEqual(len(created), 1200)
        self.assertEqual({m["action"] for m in messages}, ACTIONS)
        self.assertEqual([parse_utc(e["timestamp"]) for e in events], sorted(parse_utc(e["timestamp"]) for e in events))
        carriers = Counter(m["flight"]["carrier"] for m in created)
        types = Counter(m["flight"]["aircraftType"] for m in created)
        self.assertEqual(set(carriers), {p["carrier"] for p in self.profiles["carriers"]})
        self.assertGreater(len(types), 15)
        self.assertLess(types["321"], 300)
        self.assertGreater(carriers["AC"], carriers["WS"])
        self.assertEqual(carriers["EK"], 2)
        known_gates = {gate for terminal in self.pearson["passengerGateGroups"]["terminals"].values()
                       for group in terminal["groups"] for gate in group}
        for message in messages:
            if message["action"] == "GateOutage":
                self.assertIn(message["gate"], known_gates)
                self.assertNotIn("flight", message)
            else:
                self.assertNotIn("gate", message)
                self.assertNotIn("gate", message["flight"])
                for leg in message["legs"]:
                    self.assertNotIn("gate", leg)
                    self.assertIn("YYZ", (leg["departureStation"], leg["arrivalStation"]))

    def test_registration_country_fleet_and_tail_consistency(self):
        patterns = {"AC": r"C-[FG][A-Z]{3}", "WS": r"C-[FG][A-Z]{3}", "PD": r"C-[FG][A-Z]{3}",
                    "TS": r"C-[FG][A-Z]{3}", "F8": r"C-[FG][A-Z]{3}",
                    "UA": r"N[1-9][0-9A-HJ-NP-Z]{0,4}", "AA": r"N[1-9][0-9A-HJ-NP-Z]{0,4}",
                    "DL": r"N[1-9][0-9A-HJ-NP-Z]{0,4}", "BA": r"G-[A-Z]{4}",
                    "LH": r"D-A[A-Z]{3}", "AF": r"F-[A-Z]{4}", "EK": r"A6-[A-Z]{3}"}
        profiles = {p["carrier"]: p for p in self.profiles["carriers"]}
        known = {(r["carrier"], r["aircraftType"], r["registration"]) for r in self.fleet["records"]}
        tails = {}
        for event in self.scenario["events"]:
            message = event["message"]
            if "flight" not in message:
                continue
            flight, leg = message["flight"], message["legs"][0]
            carrier, aircraft, tail = flight["carrier"], flight["aircraftType"], flight["registration"]
            self.assertRegex(tail, "^" + patterns[carrier] + "$")
            self.assertIn((carrier, aircraft, tail), known)
            self.assertEqual(tails.setdefault(tail, (carrier, aircraft)), (carrier, aircraft))
            station = leg["arrivalStation"] if leg["departureStation"] == "YYZ" else leg["departureStation"]
            services = [s for s in profiles[carrier]["services"]
                        if station in s["stations"] or message.get("emergencyState") == "Turnback"]
            self.assertTrue(any(aircraft in s["aircraft"] for s in services))

    def test_diversions_cover_turnbacks_and_remote_stand_eligible_reasons(self):
        diversions = [event for scenario in (self.scenario, generate(0, "2026-11-07"),
                                             generate(1, "2026-11-07"))
                      for event in scenario["events"] if event["message"]["action"] == "DivertFlight"]
        self.assertEqual({e["message"]["reason"] for e in diversions}, {"turnback", "medical", "diversion"})
        self.assertEqual({e["message"]["emergencyState"] for e in diversions}, {"Turnback", "Emergency"})
        for event in diversions:
            message = event["message"]
            leg = message["legs"][0]
            self.assertEqual(leg["arrivalStation"], "YYZ")
            self.assertEqual(leg["departureStation"], "YYZ" if message["emergencyState"] == "Turnback" else "YUL")
            self.assertEqual(message["emergencyState"], "Turnback" if message["reason"] == "turnback" else "Emergency")
            if message["emergencyState"] == "Turnback":
                self.assertEqual(leg["flightSuffix"], "R")
            arrival = datetime.fromisoformat(leg["originatingDate"] + "T" + leg["scheduledArrivalTime"]).replace(tzinfo=TORONTO)
            notice = (arrival - parse_utc(event["timestamp"])).total_seconds()
            self.assertGreaterEqual(notice, 15 * 60)
            self.assertLessEqual(notice, 30 * 60)

    def test_created_rotations_have_valid_local_dates_block_times_and_turnaround(self):
        rotations = defaultdict(list)
        day = self.scenario["metadata"]["date"]
        for event in self.scenario["events"]:
            message = event["message"]
            if message["action"] == "CreateFlight":
                rotations[message["flight"]["registration"]].append(message["legs"][0])
        self.assertLessEqual(len(rotations), 600)
        for legs in rotations.values():
            incoming = sorted((l for l in legs if l["arrivalStation"] == "YYZ"), key=lambda l: l["scheduledArrivalTime"])
            outgoing = sorted((l for l in legs if l["departureStation"] == "YYZ"), key=lambda l: l["scheduledDepartureTime"])
            self.assertEqual(len(incoming), len(outgoing))
            previous_end = None
            for inbound, outbound in zip(incoming, outgoing):
                self.assertEqual(inbound["departureStation"], outbound["arrivalStation"])
                self.assertEqual(outbound["originatingDate"], day)
                origin = self.profiles["airports"][inbound["departureStation"]]
                dep = datetime.fromisoformat(inbound["originatingDate"] + "T" + inbound["scheduledDepartureTime"]).replace(tzinfo=ZoneInfo(origin["timeZone"])).astimezone(timezone.utc)
                arr = datetime.fromisoformat(day + "T" + inbound["scheduledArrivalTime"]).replace(tzinfo=TORONTO).astimezone(timezone.utc)
                block = (arr - dep).total_seconds() / 60
                self.assertLessEqual(abs(block - origin["minutes"]), 10)
                following = datetime.fromisoformat(day + "T" + outbound["scheduledDepartureTime"]).replace(tzinfo=TORONTO).astimezone(timezone.utc)
                self.assertGreaterEqual((following - arr).total_seconds(), 60 * 60)
                if previous_end is not None:
                    self.assertGreater((dep - previous_end).total_seconds(), 0)
                previous_end = following + (arr - dep)

    def test_early_inbound_updates_preserve_departure_and_precede_arrival(self):
        for day in ("2026-11-07", "2026-03-08", "2026-11-01"):
            with self.subTest(day=day):
                scenario = self.scenario if day == "2026-11-07" else generate(7, day, 120)
                created, early = {}, []
                for event in scenario["events"]:
                    message = event["message"]
                    if message["action"] == "CreateFlight":
                        created[flight_key(message)] = event
                    elif message["action"] == "UpdateTiming" and message["legs"][0]["arrivalStation"] == "YYZ":
                        original = created[flight_key(message)]
                        original_leg, leg = original["message"]["legs"][0], message["legs"][0]
                        self.assertEqual(message["flight"], original["message"]["flight"])
                        self.assertEqual({k: v for k, v in leg.items() if k != "scheduledArrivalTime"},
                                         {k: v for k, v in original_leg.items() if k != "scheduledArrivalTime"})
                        arrival = datetime.fromisoformat(day + "T" + leg["scheduledArrivalTime"]).replace(tzinfo=TORONTO)
                        scheduled = datetime.fromisoformat(day + "T" + original_leg["scheduledArrivalTime"]).replace(tzinfo=TORONTO)
                        advance = (scheduled - arrival).total_seconds() / 60
                        self.assertGreaterEqual(advance, 1)
                        self.assertLessEqual(advance, 30)
                        origin = self.profiles["airports"][leg["departureStation"]]
                        departure = datetime.fromisoformat(leg["originatingDate"] + "T" + leg["scheduledDepartureTime"]).replace(
                            tzinfo=ZoneInfo(origin["timeZone"]))
                        announced = parse_utc(event["timestamp"])
                        self.assertGreater(announced, parse_utc(original["timestamp"]))
                        self.assertGreater(announced, departure)
                        self.assertLess(announced, arrival)
                        early.append(message)
                self.assertTrue(early, "scenario must exercise early inbound arrivals")

    def test_perturbations_preserve_identity_and_cancel_before_reinstate(self):
        flights, cancelled, aliases = {}, set(), set()
        for event in self.scenario["events"]:
            message = event["message"]
            action = message["action"]
            if action == "GateOutage":
                continue
            key = flight_key(message)
            if action in ("CreateFlight", "DivertFlight"):
                self.assertNotIn(key, flights)
                flights[key] = copy.deepcopy(message)
                for alias in message["legs"][0].get("codeshare", []):
                    alias_key = (alias["carrier"], alias["flightNumber"])
                    self.assertNotIn(alias_key, aliases)
                    aliases.add(alias_key)
                continue
            self.assertIn(key, flights)
            leg, original = message["legs"][0], flights[key]["legs"][0]
            for field in ("departureStation", "arrivalStation"):
                self.assertEqual(leg[field], original[field])
            if action == "CancelFlight":
                self.assertNotIn(key, cancelled)
                cancelled.add(key)
            elif action == "ReinstateFlight":
                self.assertIn(key, cancelled)
                cancelled.remove(key)
            elif action in ("UpdateTiming", "ReplaceFlight"):
                if leg["arrivalStation"] == "YYZ":
                    self.assertEqual(leg["scheduledDepartureTime"], original["scheduledDepartureTime"])
                    self.assertLess(leg["scheduledArrivalTime"], original["scheduledArrivalTime"])
                else:
                    self.assertGreater(leg["scheduledDepartureTime"], original["scheduledDepartureTime"])

    def test_dst_and_overnight_service_keep_utc_announcements_on_toronto_date(self):
        for day in ("2026-03-08", "2026-11-01", "2026-12-31"):
            with self.subTest(day=day):
                scenario = generate(7, day, 120)
                for event in scenario["events"]:
                    self.assertEqual(parse_utc(event["timestamp"]).astimezone(TORONTO).date().isoformat(), day)
                    if "flight" in event["message"]:
                        leg = event["message"]["legs"][0]
                        if leg["departureStation"] == "YYZ":
                            self.assertEqual(leg["originatingDate"], day)

    def test_cli_writes_loadable_fixture_and_protects_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenario.json"
            command = [sys.executable, "-m", "smartgate_stream", "generate", "--seed", "42",
                       "--date", "2026-11-07", "--flights", "25", "--output", str(path)]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            scenario = load_scenario(path)
            self.assertEqual(scenario, generate(42, "2026-11-07", 25))
            before = path.read_bytes()
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_bytes(), before)
            path.unlink()
            command[command.index("--date") + 1] = "2026-02-30"
            self.assertEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 2)
            self.assertFalse(path.exists())

    def test_cli_defaults_to_toronto_date_at_utc_midnight_and_dst_boundaries(self):
        cases = (("2026-11-08T03:30:00+00:00", "2026-11-07"),
                 ("2026-03-08T04:30:00+00:00", "2026-03-07"),
                 ("2026-11-01T03:30:00+00:00", "2026-10-31"))
        with tempfile.TemporaryDirectory() as directory:
            for index, (instant, expected_date) in enumerate(cases):
                with self.subTest(instant=instant):
                    path = Path(directory) / f"scenario-{index}.json"
                    argv = ["smartgate_stream", "generate", "--seed", "42", "--flights", "2", "--output", str(path)]
                    with patch("sys.argv", argv), patch("smartgate_stream.__main__.datetime") as clock, patch("builtins.print"):
                        clock.now.side_effect = lambda tz: datetime.fromisoformat(instant).astimezone(tz)
                        main()
                        clock.now.assert_called_once_with(TORONTO)
                    scenario = load_scenario(path)
                    self.assertEqual(scenario["metadata"]["date"], expected_date)
                    self.assertEqual(scenario, generate(42, expected_date, 2))

    def test_cli_rate_option_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenario.json"
            command = [sys.executable, "-m", "smartgate_stream", "generate", "--date", "2026-11-07",
                       "--flights", "2", "--output", str(path), "--rate", "2.5"]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(load_scenario(path)["metadata"]["rate"], 2.5)
            path.unlink()
            for rate in ("0", "-1", "nan", "inf", "not-a-number"):
                with self.subTest(rate=rate):
                    command[-1] = rate
                    self.assertEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 2)
                    self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
