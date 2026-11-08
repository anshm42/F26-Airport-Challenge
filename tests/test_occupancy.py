from gms.occupancy import build_presences, intervals_overlap, sets_conflict
from gms.config import GROUND_MINUTES, PREP_MINUTES


def leg(dep_station, arr_station, dep_abs, arr_abs):
    return {"departureStation": dep_station, "arrivalStation": arr_station,
            "dep_abs": dep_abs, "arr_abs": arr_abs}


def test_inbound_only_uses_ground_buffer():
    legs = [leg("YVR", "YYZ", 600, 750)]
    assert build_presences(legs) == [(750, 750 + GROUND_MINUTES)]


def test_turnaround_uses_real_window():
    legs = [leg("YUL", "YYZ", 540, 660), leg("YYZ", "YOW", 780, 840)]
    assert build_presences(legs) == [(660, 780)]


def test_originate_then_return_yields_two_intervals():
    # YYZ->YUL dep 08:00 (480), YUL->YYZ arr 13:00 (780): gate free 08:00-13:00
    legs = [leg("YYZ", "YUL", 480, 600), leg("YUL", "YYZ", 660, 780)]
    assert build_presences(legs) == [(480 - PREP_MINUTES, 480), (780, 780 + GROUND_MINUTES)]


def test_multi_leg_non_yyz_legs_ignored():
    # A leg that never touches YYZ contributes no presence.
    legs = [leg("LAX", "SFO", 100, 200), leg("SFO", "YYZ", 300, 500)]
    assert build_presences(legs) == [(500, 500 + GROUND_MINUTES)]


def test_half_open_touching_is_not_a_conflict():
    assert intervals_overlap((600, 720), (700, 800)) is True
    assert intervals_overlap((600, 720), (720, 800)) is False  # back-to-back allowed


def test_sets_conflict_any_pair():
    a = [(420, 480), (780, 840)]
    b = [(750, 810)]   # overlaps the second interval of a
    assert sets_conflict(a, b) is True
    c = [(480, 700)]   # touches/after first, before second
    assert sets_conflict(a, c) is False
