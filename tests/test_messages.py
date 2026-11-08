from gms.messages import FlightStore, make_flight_id, parse_gate_outage, OUTAGE_OPEN_END


def create(carrier="AC", number="100", date="2025-06-15", legs=None):
    return {"action": "CreateFlight",
            "flight": {"carrier": carrier, "flightNumber": number, "flightSuffix": None, "originatingDate": date},
            "legs": legs or [
                {"legType": "Originating", "aircraftType": "73G",
                 "departureStation": "YVR", "arrivalStation": "YYZ",
                 "scheduledDepartureTime": "09:00", "scheduledArrivalTime": "11:00"},
                {"legType": "Through", "aircraftType": "73G",
                 "departureStation": "YYZ", "arrivalStation": "YVR",
                 "scheduledDepartureTime": "13:00", "scheduledArrivalTime": "15:00"},
            ]}


def test_make_flight_id():
    assert make_flight_id("AC", "123", "2025-06-15") == "AC-123-2025-06-15"
    assert make_flight_id("AC", "123", "2025-06-15", "A") == "AC-123A-2025-06-15"


def test_create_and_replace():
    store = FlightStore()
    fid, _ = store.apply(create(), 540)
    assert fid == "AC-100-2025-06-15"
    assert len(store.flights[fid]["legs"]) == 2


def test_update_timing_merges_times_keeps_equipment():
    store = FlightStore()
    fid, _ = store.apply(create(), 540)
    store.apply({"action": "UpdateTiming",
                 "flight": {"carrier": "AC", "flightNumber": "100", "flightSuffix": None, "originatingDate": "2025-06-15"},
                 "legs": [{"legType": "Originating", "aircraftType": "73G",
                           "departureStation": "YVR", "arrivalStation": "YYZ",
                           "scheduledDepartureTime": "10:00", "scheduledArrivalTime": "12:00"}]}, 600)
    inbound = store.flights[fid]["legs"][0]
    assert inbound["scheduledArrivalTime"] == "12:00"     # updated
    assert inbound["aircraftType"] == "73G"               # untouched


def test_update_equipment_all_legs():
    store = FlightStore()
    fid, _ = store.apply(create(), 540)
    store.apply({"action": "UpdateEquipment",
                 "flight": {"carrier": "AC", "flightNumber": "100", "flightSuffix": None, "originatingDate": "2025-06-15"},
                 "equipmentUpdate": {"scope": "AllLegs", "aircraftType": "789"}}, 600)
    assert all(l["aircraftType"] == "789" for l in store.flights[fid]["legs"])


def test_update_equipment_specific_leg():
    store = FlightStore()
    fid, _ = store.apply(create(), 540)
    store.apply({"action": "UpdateEquipment",
                 "flight": {"carrier": "AC", "flightNumber": "100", "flightSuffix": None, "originatingDate": "2025-06-15"},
                 "equipmentUpdate": {"scope": "SpecificLeg", "departureStation": "YYZ",
                                     "arrivalStation": "YVR", "aircraftType": "789"}}, 600)
    legs = store.flights[fid]["legs"]
    assert legs[0]["aircraftType"] == "73G"   # YVR->YYZ unchanged
    assert legs[1]["aircraftType"] == "789"   # YYZ->YVR changed


def test_cancel_all_then_reinstate():
    store = FlightStore()
    fid, _ = store.apply(create(), 540)
    store.apply({"action": "CancelFlight",
                 "flight": {"carrier": "AC", "flightNumber": "100", "flightSuffix": None, "originatingDate": "2025-06-15"},
                 "cancelScope": {"type": "AllLegs"}}, 600)
    assert store.flights[fid]["cancelled"] is True
    store.apply({"action": "ReinstateFlight",
                 "flight": {"carrier": "AC", "flightNumber": "100", "flightSuffix": None, "originatingDate": "2025-06-15"}}, 660)
    assert store.flights[fid]["cancelled"] is False


def test_cancel_specific_leg():
    store = FlightStore()
    fid, _ = store.apply(create(), 540)
    store.apply({"action": "CancelFlight",
                 "flight": {"carrier": "AC", "flightNumber": "100", "flightSuffix": None, "originatingDate": "2025-06-15"},
                 "cancelScope": {"type": "SpecificLeg", "departureStation": "YYZ", "arrivalStation": "YVR"}}, 600)
    state = store.flights[fid]
    assert ("YYZ", "YVR") in state["cancelled_legs"]
    assert state["cancelled"] is False   # one leg remains


def test_parse_gate_outage():
    gid, window = parse_gate_outage({"action": "GateOutage", "gate": "D1",
                                     "scheduledStartTime": "10:00", "scheduledEndTime": "15:00"})
    assert gid == "D1"
    assert window == (600, 900)


def test_parse_gate_outage_overnight_and_open_ended():
    _, overnight = parse_gate_outage({"gate": "C1", "scheduledStartTime": "23:00", "scheduledEndTime": "02:00"})
    assert overnight == (23 * 60, 2 * 60 + 24 * 60)   # end rolls to next day
    _, open_ended = parse_gate_outage({"gate": "C1", "scheduledStartTime": "08:00"})
    assert open_ended == (8 * 60, OUTAGE_OPEN_END)


def test_divert_flight_is_flagged():
    store = FlightStore()
    fid, _ = store.apply({
        "action": "DivertFlight", "reason": "medical",
        "flight": {"carrier": "QK", "flightNumber": "501", "flightSuffix": None, "originatingDate": "2025-06-15"},
        "legs": [{"legType": "Originating", "aircraftType": "321",
                  "departureStation": "YQB", "arrivalStation": "YYZ",
                  "scheduledDepartureTime": "10:00", "scheduledArrivalTime": "11:15"}],
    }, 660)
    assert fid == "QK-501-2025-06-15"
    assert store.flights[fid]["diversion"] is True
    assert store.flights[fid]["reason"] == "medical"


def test_unknown_flight_update_is_ignored():
    store = FlightStore()
    fid, warn = store.apply({"action": "UpdateTiming",
                             "flight": {"carrier": "AC", "flightNumber": "999", "flightSuffix": None, "originatingDate": "2025-06-15"},
                             "legs": []}, 540)
    assert fid is None and "unknown" in warn.lower()
