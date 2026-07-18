"""AI-QI ingest scheduler (Part 1).

A long-running worker that triggers the CPCB ingest once an hour. Used for
local dev and can run as a Render "Background Worker". In production you can
instead point a Render Cron Job at `python -m services.ingest.ingest` and skip
this file entirely — the job logic is identical either way.

Run:  python -m services.ingest.scheduler   (Ctrl+C to stop)
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from services.ingest.ingest import run_ingest
from services.ingest.weather import run_weather_ingest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
# httpx logs the full request URL at INFO — which includes ?api-key=... .
# Silence it so the secret never lands in logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("aiqi.scheduler")

# CPCB refreshes roughly hourly; run a few minutes past the hour so the
# upstream has published the new values before we pull.
INGEST_MINUTE = 5


def ingest_job() -> None:
    """Run CPCB + weather ingest. Never lets one failure kill the scheduler.

    The two sources are independent: a weather failure must not lose CPCB data
    and vice versa, so each is guarded separately.
    """
    try:
        stations, inserted = run_ingest()
        log.info("cpcb ok: %d stations, %d new readings", stations, inserted)
    except Exception:  # noqa: BLE001 — a scheduled job must survive any error
        log.exception("cpcb ingest failed; will retry next hour")

    try:
        polled, w_inserted = run_weather_ingest()
        if polled:
            log.info("weather ok: %d polled, %d new rows", polled, w_inserted)
        else:
            log.info("weather skipped (no OPENWEATHER_API_KEY set)")
    except Exception:  # noqa: BLE001
        log.exception("weather ingest failed; will retry next hour")


def main() -> None:
    scheduler = BlockingScheduler(timezone="Asia/Kolkata")
    # Hourly at HH:05.
    scheduler.add_job(ingest_job, "cron", minute=INGEST_MINUTE, id="cpcb_ingest")

    log.info("scheduler starting - running one ingest now, then hourly at :%02d", INGEST_MINUTE)
    ingest_job()  # prime immediately so we don't wait up to an hour for first data

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")


if __name__ == "__main__":
    main()
