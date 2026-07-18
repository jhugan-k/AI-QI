"""Delhi-specific seasonal drivers for the forecast models (Part 3).

Single source of truth for the two big episodic pollution drivers, used by BOTH
the synthetic seed (to inject the effect) and the model (to learn it):

  - Diwali: firecracker spike. Modeled as a Prophet *holiday* with an after-
    window because the smoke lingers for days.
  - Stubble burning: a multi-week SEASON (mid-Oct to end-Nov) when farm fires
    upwind blanket Delhi. Modeled as an additional binary regressor.

Diwali dates are lunar (shift yearly), so they're listed explicitly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

# Diwali dates (approximate; the festival is set by the lunar calendar).
DIWALI_DATES: list[date] = [
    date(2024, 11, 1),
    date(2025, 10, 21),
    date(2026, 11, 8),
]
DIWALI_LOWER_WINDOW = -1   # day before (pre-festival bursting)
DIWALI_UPPER_WINDOW = 5    # smoke lingers ~5 days after

# Stubble-burning season, as (month, day) bounds. Same window each year.
STUBBLE_START = (10, 15)
STUBBLE_END = (11, 30)


def is_stubble_season(ts: datetime) -> int:
    """1 if the timestamp falls in the stubble-burning season, else 0."""
    md = (ts.month, ts.day)
    return int(STUBBLE_START <= md <= STUBBLE_END)


def in_diwali_window(ts: datetime) -> bool:
    """True if ts is within a Diwali effect window (day before to 5 days after)."""
    for d in DIWALI_DATES:
        start = datetime(d.year, d.month, d.day) + timedelta(days=DIWALI_LOWER_WINDOW)
        end = datetime(d.year, d.month, d.day) + timedelta(days=DIWALI_UPPER_WINDOW)
        if start.date() <= ts.date() <= end.date():
            return True
    return False


def add_event_regressors(df: pd.DataFrame) -> pd.DataFrame:
    """Add the `stubble` and `diwali` regressor columns (train + future frames).

    Diwali is an ADDITIONAL REGRESSOR here, not a Prophet holiday. Prophet's
    windowed holidays expand into a separate feature per day-offset, which
    fragments a sparse (few-occurrence) event into many weakly-supported
    features that the prior shrinks to ~0 — empirically the Diwali holiday
    learned no effect. A single binary regressor over the effect window gets
    one well-supported coefficient, exactly like `stubble` (which worked).
    """
    df = df.copy()
    df["stubble"] = df["ds"].apply(is_stubble_season)
    df["diwali"] = df["ds"].apply(lambda ts: int(in_diwali_window(ts)))
    return df
