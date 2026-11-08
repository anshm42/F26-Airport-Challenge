from gms.timeutil import hhmm_str_to_min, hhmm_key_to_min, absolutize, fmt


def test_parse_times():
    assert hhmm_str_to_min("14:30") == 870
    assert hhmm_str_to_min("00:00") == 0
    assert hhmm_key_to_min("0900") == 540
    assert hhmm_key_to_min("0700") == 420


def test_absolutize_monotonic_same_day():
    assert absolutize([480, 600, 660, 780]) == [480, 600, 660, 780]


def test_absolutize_rolls_over_midnight():
    # depart 22:00, arrive 02:00 -> arrival must roll into the next day
    out = absolutize([22 * 60, 2 * 60])
    assert out == [1320, 1560]          # 22:00, 02:00+1d
    assert out[1] == 2 * 60 + 24 * 60   # 02:00 next day


def test_absolutize_multi_leg_overnight():
    # 23:30 dep, 00:30 arr, 01:30 dep, 03:00 arr  -> strictly increasing
    out = absolutize([23 * 60 + 30, 30, 90, 180])
    assert out == sorted(out)
    assert all(out[i] < out[i + 1] for i in range(len(out) - 1))


def test_fmt_day_offset():
    assert fmt(750) == "12:30"
    assert fmt(24 * 60 + 30) == "00:30+1d"
    assert fmt(None) == "N/A"
