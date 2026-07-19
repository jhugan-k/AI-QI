"""Tests for forecast accuracy scoring (Part 5).
Run: python -m pytest services/api/test_scoring.py -q

Imports services.api.main, which is safe offline: the DB pool is created with
open=False and never connected at import time.
"""

import math

from services.api.main import _score


def _rows(pairs):
    """pairs = list of (yhat, actual, lower, upper) -> dict rows like the SQL join."""
    return [
        {"yhat": y, "actual": a, "yhat_lower": lo, "yhat_upper": hi}
        for y, a, lo, hi in pairs
    ]


def test_score_empty_is_all_none():
    m = _score([])
    assert m.n == 0
    assert m.mae is None and m.rmse is None and m.mape is None and m.coverage is None


def test_score_perfect_prediction():
    m = _score(_rows([(50.0, 50.0, 45.0, 55.0), (60.0, 60.0, 55.0, 65.0)]))
    assert m.n == 2
    assert m.mae == 0.0
    assert m.rmse == 0.0
    assert m.mape == 0.0
    assert m.coverage == 100.0


def test_score_known_errors():
    # errors of +10 and -20 against actuals 100 and 100
    m = _score(_rows([(110.0, 100.0, 90.0, 120.0), (80.0, 100.0, 70.0, 130.0)]))
    assert m.mae == 15.0                                   # (10 + 20) / 2
    assert m.rmse == round(math.sqrt((100 + 400) / 2), 2)  # ~15.81
    assert m.mape == 15.0                                  # (10% + 20%) / 2
    assert m.coverage == 100.0                             # both actuals inside band


def test_score_coverage_counts_band_misses():
    # first actual outside its band, second inside -> 50% coverage
    m = _score(_rows([(50.0, 90.0, 45.0, 55.0), (50.0, 52.0, 45.0, 55.0)]))
    assert m.coverage == 50.0


def test_score_skips_rows_missing_a_value():
    rows = _rows([(50.0, 50.0, 45.0, 55.0)]) + [
        {"yhat": None, "actual": 10.0, "yhat_lower": 1.0, "yhat_upper": 20.0},
        {"yhat": 10.0, "actual": None, "yhat_lower": 1.0, "yhat_upper": 20.0},
    ]
    m = _score(rows)
    assert m.n == 1                                        # only the complete pair scored


def test_score_mape_ignores_zero_actual():
    # a zero actual would divide by zero; it must be excluded from MAPE only
    m = _score(_rows([(5.0, 0.0, 0.0, 10.0), (110.0, 100.0, 90.0, 120.0)]))
    assert m.n == 2                                        # both count for MAE
    assert m.mape == 10.0                                  # only the non-zero actual
