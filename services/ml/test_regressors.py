"""Tests for the seasonal event regressors (Part 3).
Run: python -m pytest services/ml/test_regressors.py -q
"""

from datetime import datetime

import pandas as pd

from services.ml.regressors import add_event_regressors, is_stubble_season


def test_stubble_season_window():
    assert is_stubble_season(datetime(2026, 11, 3, 10)) == 1     # inside Oct15-Nov30
    assert is_stubble_season(datetime(2026, 10, 20, 0)) == 1
    assert is_stubble_season(datetime(2026, 8, 15, 10)) == 0     # summer, outside
    assert is_stubble_season(datetime(2026, 12, 5, 10)) == 0     # after the window


def test_add_event_regressors_adds_columns():
    df = pd.DataFrame({"ds": pd.to_datetime(["2026-08-15 10:00", "2026-11-03 10:00"])})
    out = add_event_regressors(df)
    assert {"stubble", "diwali"} <= set(out.columns)
    assert out.loc[0, "stubble"] == 0                            # summer row
    assert out.loc[1, "stubble"] == 1                            # stubble-season row
    assert set(out["stubble"].unique()) <= {0, 1}               # binary regressor
