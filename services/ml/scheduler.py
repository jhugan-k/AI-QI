"""AI-QI ML scheduler (Part 5+).

Long-running worker that retrains models and refreshes forecasts once a day,
the ML counterpart to the hourly ingest scheduler. Runs two jobs in order:

    batch     — retrain every trainable series and store its fresh 24h forecast
    backtest  — re-score the models on a holdout so /accuracy stays current

Like the ingest scheduler, this can run as a background worker, or you can point
a daily cron job at `python -m services.ml.batch` (then `...backtest`) instead —
the job logic is identical either way.

Runs daily at BATCH_HOUR:BATCH_MINUTE IST — after the overnight readings are in,
and offset from the hourly ingest so the two don't contend.

Run:  python -m services.ml.scheduler   (Ctrl+C to stop)
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from services.ml.backtest import run as run_backtest
from services.ml.batch import run_batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aiqi.ml.scheduler")

# Early morning IST: readings for the day-so-far are in, and it's away from the
# hourly ingest at :05. Training is minutes of CPU, so off-peak is kind.
BATCH_HOUR = 2
BATCH_MINUTE = 30


def ml_job() -> None:
    """Retrain + re-forecast, then re-score. Each stage guarded independently so
    a training failure doesn't block scoring the models that did train."""
    try:
        run_batch()
        log.info("batch ok: models retrained and forecasts refreshed")
    except Exception:  # noqa: BLE001 — a scheduled job must survive any error
        log.exception("batch failed; will retry tomorrow")

    try:
        run_backtest()
        log.info("backtest ok: accuracy scores refreshed")
    except Exception:  # noqa: BLE001
        log.exception("backtest failed; will retry tomorrow")


def main() -> None:
    scheduler = BlockingScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(ml_job, "cron", hour=BATCH_HOUR, minute=BATCH_MINUTE, id="ml_batch")

    log.info("ml scheduler starting — priming once now, then daily at %02d:%02d IST",
             BATCH_HOUR, BATCH_MINUTE)
    ml_job()  # prime so a fresh deploy has models/forecasts without waiting a day

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("ml scheduler stopped")


if __name__ == "__main__":
    main()
