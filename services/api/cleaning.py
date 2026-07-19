"""Clean-on-read preprocessing for pollutant time series (Part 2).

Pure functions, no pandas — the series are small (24-720 points) and explicit
logic is easier to reason about and test. Pipeline order matters:

    validate (drop impossible) -> flag outliers (3-sigma) -> smooth (rolling avg)

None means "no value" throughout (offline sensor or a value we rejected); every
function preserves list length and position so timestamps stay aligned.
"""

from __future__ import annotations

from statistics import median

# Physically plausible concentration ranges. Values outside these are sensor
# errors, not real air quality, so we reject them outright. Units: ug/m3,
# except CO in mg/m3. Upper bounds are generous (Delhi hits extreme values).
PLAUSIBLE_RANGES: dict[str, tuple[float, float]] = {
    "PM2.5": (0, 1000),
    "PM10": (0, 2000),
    "NO2": (0, 500),
    "SO2": (0, 500),
    "CO": (0, 50),        # mg/m3
    "OZONE": (0, 500),
    "NH3": (0, 1000),
}

# Robust outlier detection uses a MODIFIED z-score (median + MAD), not mean/std.
# Naive 3-sigma suffers "masking": one big spike inflates the mean and std so
# much its own z-score falls under 3 and it escapes. Median/MAD can't be
# distorted that way. 3.5 is the standard Iglewicz-Hoaglin threshold.
MODIFIED_Z_THRESHOLD = 3.5
MAD_SCALE = 0.6745        # makes MAD comparable to a standard deviation
SMOOTH_WINDOW = 3         # trailing points in the rolling average
MIN_POINTS_FOR_OUTLIERS = 4   # need a few points before stats are meaningful


def is_plausible(pollutant: str, value: float | None) -> bool:
    """True if value is within the pollutant's physical range."""
    if value is None:
        return False
    lo, hi = PLAUSIBLE_RANGES.get(pollutant, (0.0, float("inf")))
    return lo <= value <= hi


def validate(pollutant: str, values: list[float | None]) -> list[float | None]:
    """Replace physically-impossible values with None."""
    return [v if is_plausible(pollutant, v) else None for v in values]


def flag_outliers(
    values: list[float | None], threshold: float = MODIFIED_Z_THRESHOLD
) -> list[float | None]:
    """Replace robust statistical outliers with None (modified z-score).

    Uses median + MAD instead of mean + std so a single big spike can't mask
    itself (see MODIFIED_Z_THRESHOLD note above).

    Caveat worth stating: even robust removal can drop *legitimate* spikes
    (Diwali, stubble-burning). Physical-range validation handles the truly
    impossible; context-aware anomaly handling belongs in the ML layer (Part 3).
    """
    present = [v for v in values if v is not None]
    if len(present) < MIN_POINTS_FOR_OUTLIERS:
        return list(values)
    med = median(present)
    mad = median([abs(v - med) for v in present])
    if mad == 0:                       # >half the points identical: nothing to flag
        return list(values)
    return [
        v if (v is None or abs(MAD_SCALE * (v - med) / mad) <= threshold) else None
        for v in values
    ]


def rolling_average(
    values: list[float | None], window: int = SMOOTH_WINDOW
) -> list[float | None]:
    """Trailing rolling mean over present values. Output stays length-aligned.

    Only smooths positions that HAD a value. A position that is None stays None
    — that's a real gap for the imputation step to fill, and smoothing must not
    silently paper over it with neighbouring values.
    """
    out: list[float | None] = []
    for i in range(len(values)):
        if values[i] is None:
            out.append(None)           # preserve gaps; don't impute here
            continue
        chunk = [v for v in values[max(0, i - window + 1): i + 1] if v is not None]
        out.append(sum(chunk) / len(chunk))
    return out


def clean_series(pollutant: str, values: list[float | None]) -> list[float | None]:
    """Full clean-on-read pipeline for one pollutant series."""
    values = validate(pollutant, values)
    values = flag_outliers(values)
    values = rolling_average(values)
    return values


def impute_hour_of_day(
    hours: list[int],
    values: list[float | None],
    hour_averages: dict[int, float],
    neighbour_averages: dict[int, float] | None = None,
) -> tuple[list[float | None], list[bool]]:
    """Fill None gaps with the typical value for that hour-of-day.

    Pollution has a strong daily cycle, so a gap at 08:00 is best guessed from
    what this station usually reads at 08:00. `hour_averages` maps IST hour
    (0-23) -> mean value, supplied by the caller from the DB.

    Two-stage fallback:
      1. the station's OWN hour-of-day average (best — same location)
      2. a NEIGHBOUR station's hour-of-day average (`neighbour_averages`), used
         only for hours the station itself has never reported. A newly-installed
         station, or one offline for a whole hour-of-day, has no own-history for
         that hour; the nearest station is the next-best spatial proxy.

    Returns (filled_values, imputed_flags) where imputed_flags[i] is True iff
    position i was a gap that we filled (from either stage). A gap with no
    average from either source stays None (nothing to fill it with).
    """
    filled: list[float | None] = []
    flags: list[bool] = []
    for h, v in zip(hours, values):
        if v is not None:
            filled.append(v)
            flags.append(False)
        elif h in hour_averages:
            filled.append(hour_averages[h])
            flags.append(True)
        elif neighbour_averages and h in neighbour_averages:
            filled.append(neighbour_averages[h])
            flags.append(True)
        else:
            filled.append(None)
            flags.append(False)
    return filled, flags
