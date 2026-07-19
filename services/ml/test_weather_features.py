"""Tests for the weather-regressor helpers (Part 5+).
Run: python -m pytest services/ml/test_weather_features.py -q

Pure dataframe logic — no DB. The weather_history() DB reader isn't tested here.
"""

import pandas as pd

from services.ml.weather_features import (
    WEATHER_REGRESSORS,
    attach_weather,
    has_weather_signal,
    hourly_climatology,
)


def _wdf(rows):
    """rows = list of (hour, wind, temp, humidity) on an arbitrary fixed day."""
    return pd.DataFrame(
        [(pd.Timestamp("2026-01-01") + pd.Timedelta(hours=h), w, t, hu) for h, w, t, hu in rows],
        columns=["ds", "wind_speed", "temp_c", "humidity"],
    )


def test_climatology_averages_by_hour():
    wdf = _wdf([(8, 2.0, 20.0, 50), (8, 4.0, 30.0, 60), (9, 1.0, 25.0, 55)])
    clim = hourly_climatology(wdf)
    assert clim[8]["wind_speed"] == 3.0        # mean(2, 4)
    assert clim[8]["temp_c"] == 25.0
    assert clim[9]["wind_speed"] == 1.0


def test_climatology_empty():
    assert hourly_climatology(pd.DataFrame(columns=["ds", *WEATHER_REGRESSORS])) == {}


def test_attach_uses_actual_when_present():
    hist = _wdf([(8, 2.5, 22.0, 48)])
    df = pd.DataFrame({"ds": [pd.Timestamp("2026-01-01 08:00")], "y": [100.0]})
    out = attach_weather(df, hist, hourly_climatology(hist))
    assert out.loc[0, "wind_speed"] == 2.5     # actual weather joined in


def test_attach_fills_future_from_climatology():
    hist = _wdf([(8, 2.0, 20.0, 50), (8, 4.0, 22.0, 52)])   # climatology for hour 8
    clim = hourly_climatology(hist)
    # a future row at 08:00 with no actual weather
    future = pd.DataFrame({"ds": [pd.Timestamp("2026-06-01 08:00")]})
    out = attach_weather(future, hist, clim)
    assert out.loc[0, "wind_speed"] == 3.0     # filled from hour-8 climatology
    assert out["wind_speed"].notna().all()


def test_attach_always_fully_populated():
    # hour with no climatology and no actual -> falls back to overall mean, never NaN
    hist = _wdf([(8, 2.0, 20.0, 50)])
    future = pd.DataFrame({"ds": [pd.Timestamp("2026-06-01 03:00")]})  # hour 3, unseen
    out = attach_weather(future, hist, hourly_climatology(hist))
    for col in WEATHER_REGRESSORS:
        assert out[col].notna().all()


def test_has_signal_requires_variance_and_volume():
    flat = _wdf([(h % 24, 2.0, 20.0, 50) for h in range(150)])   # constant wind
    assert not has_weather_signal(flat)                          # zero variance
    varied = _wdf([(h % 24, 2.0 + (h % 5), 20.0 + h, 40 + (h % 7)) for h in range(150)])
    assert has_weather_signal(varied)


def test_has_signal_requires_enough_rows():
    tiny = _wdf([(h, 2.0 + h, 20.0 + h, 40 + h) for h in range(10)])
    assert not has_weather_signal(tiny)                          # below MIN_WEATHER_ROWS
