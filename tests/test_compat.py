from gms.compat import classify, type_allowed, hard_compatible, is_premium_misuse
from gms.config import CARGO, DOMESTIC, INTERNATIONAL


def yyz_leg(dep, arr, ac):
    return {"departureStation": dep, "arrivalStation": arr, "aircraftType": ac}


def test_classify(static_data):
    _, ac_info, stations = static_data
    assert classify([yyz_leg("YHZ", "YYZ", "76F")], ac_info, stations) == CARGO
    assert classify([yyz_leg("YVR", "YYZ", "73G")], ac_info, stations) == DOMESTIC
    assert classify([yyz_leg("LHR", "YYZ", "73G")], ac_info, stations) == INTERNATIONAL
    # mixed: any international YYZ leg makes the whole flight international
    assert classify([yyz_leg("YVR", "YYZ", "73G"), yyz_leg("YYZ", "LHR", "73G")],
                    ac_info, stations) == INTERNATIONAL
    # unknown station defaults to international (stricter, more realistic)
    assert classify([yyz_leg("ZZZ", "YYZ", "73G")], ac_info, stations) == INTERNATIONAL


def test_type_allowed_is_asymmetric():
    assert type_allowed(CARGO, 0) and not type_allowed(CARGO, 1) and not type_allowed(CARGO, 2)
    assert type_allowed(DOMESTIC, 1) and type_allowed(DOMESTIC, 2) and not type_allowed(DOMESTIC, 0)
    assert type_allowed(INTERNATIONAL, 2) and not type_allowed(INTERNATIONAL, 1)


def test_hard_compatible_reasons(static_data):
    gates, _, _ = static_data
    d3, i3, i1 = gates["D3"], gates["I3"], gates["I1"]
    # 787: 60m + jetbridge required
    assert hard_compatible(DOMESTIC, 60, True, d3) == (False, "wingspan")   # D3 max 45
    assert hard_compatible(DOMESTIC, 60, True, i3) == (False, "jetbridge")  # I3 fits 60 but no jetbridge
    assert hard_compatible(DOMESTIC, 60, True, i1) == (True, None)          # I1 fits, has jetbridge
    # international flight cannot use a domestic gate (no customs)
    assert hard_compatible(INTERNATIONAL, 36, False, gates["D1"]) == (False, "type")


def test_premium_misuse():
    assert is_premium_misuse(DOMESTIC, INTERNATIONAL) is True
    assert is_premium_misuse(DOMESTIC, DOMESTIC) is False
    assert is_premium_misuse(INTERNATIONAL, INTERNATIONAL) is False
