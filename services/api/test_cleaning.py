"""Tests for the clean-on-read pipeline. Run: python -m pytest services/api/test_cleaning.py -q"""

from services.api.cleaning import (
    clean_series,
    flag_outliers,
    impute_hour_of_day,
    is_plausible,
    rolling_average,
    validate,
)


def test_is_plausible():
    assert is_plausible("PM2.5", 150)
    assert not is_plausible("PM2.5", -5)        # negative impossible
    assert not is_plausible("PM2.5", 5000)      # above physical max
    assert not is_plausible("PM2.5", None)      # missing
    assert is_plausible("CO", 3)                # mg/m3 scale


def test_validate_replaces_impossible_with_none():
    vals = [50.0, -1.0, 9999.0, 80.0]
    assert validate("PM2.5", vals) == [50.0, None, None, 80.0]


def test_flag_outliers_removes_spike():
    # tight cluster around 50 with one absurd 3-sigma+ spike
    vals = [48.0, 50.0, 52.0, 49.0, 51.0, 500.0]
    out = flag_outliers(vals)
    assert out[-1] is None                      # the 500 spike is dropped
    assert out[:5] == vals[:5]                   # the rest is untouched


def test_flag_outliers_needs_enough_points():
    vals = [10.0, 999.0]                          # too few to judge
    assert flag_outliers(vals) == vals            # left as-is


def test_flag_outliers_flat_series():
    vals = [5.0, 5.0, 5.0, 5.0]                   # sigma == 0
    assert flag_outliers(vals) == vals            # nothing flagged


def test_rolling_average_smooths_and_keeps_length():
    vals = [10.0, 20.0, 30.0]
    out = rolling_average(vals, window=2)
    assert len(out) == 3
    assert out[0] == 10.0                          # first point: itself
    assert out[1] == 15.0                          # mean(10,20)
    assert out[2] == 25.0                          # mean(20,30)


def test_rolling_average_preserves_gaps():
    vals = [None, 20.0, None]
    out = rolling_average(vals, window=3)
    assert out[0] is None                          # gap stays a gap
    assert out[1] == 20.0                          # present point smoothed
    assert out[2] is None                          # gap NOT filled by smoothing


def test_impute_fills_gaps_from_hour_average():
    hours = [7, 8, 9]
    values = [70.0, None, 90.0]          # 08:00 is a gap
    averages = {7: 65.0, 8: 80.0, 9: 88.0}
    filled, flags = impute_hour_of_day(hours, values, averages)
    assert filled == [70.0, 80.0, 90.0]  # gap filled with the 08:00 average
    assert flags == [False, True, False]  # only the gap is flagged imputed


def test_impute_leaves_gap_when_no_average():
    hours = [3]
    values = [None]
    averages = {}                         # no history for hour 3
    filled, flags = impute_hour_of_day(hours, values, averages)
    assert filled == [None]               # nothing to fill it with
    assert flags == [False]


def test_impute_does_not_touch_present_values():
    hours = [8]
    values = [50.0]
    averages = {8: 999.0}
    filled, flags = impute_hour_of_day(hours, values, averages)
    assert filled == [50.0]               # real reading kept, not overwritten
    assert flags == [False]


def test_impute_falls_back_to_neighbour():
    hours = [3, 4]
    values = [None, None]
    own = {4: 40.0}                       # own history only for hour 4
    neighbour = {3: 33.0, 4: 999.0}       # neighbour has hour 3
    filled, flags = impute_hour_of_day(hours, values, own, neighbour)
    assert filled == [33.0, 40.0]         # hour 3 from neighbour, hour 4 from OWN (preferred)
    assert flags == [True, True]


def test_impute_prefers_own_over_neighbour():
    hours = [8]
    values = [None]
    own = {8: 80.0}
    neighbour = {8: 10.0}
    filled, _ = impute_hour_of_day(hours, values, own, neighbour)
    assert filled == [80.0]               # own average wins when both exist


def test_impute_gap_stays_when_neither_source_has_hour():
    hours = [2]
    values = [None]
    filled, flags = impute_hour_of_day(hours, values, {}, {5: 1.0})
    assert filled == [None]               # neighbour has hour 5, not hour 2
    assert flags == [False]


def test_clean_series_end_to_end():
    # impossible value, a spike, and noise together
    vals = [50.0, -3.0, 55.0, 60.0, 9999.0, 58.0]
    out = clean_series("PM2.5", vals)
    assert len(out) == len(vals)                   # length preserved
    assert out[1] is None                          # -3 rejected by validate
    # 9999 rejected by validate; remaining series has no extreme -> smoothed values finite
    assert all(v is None or v >= 0 for v in out)
