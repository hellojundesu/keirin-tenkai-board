"""Resolve one immutable race-card target date for a workflow run.

Scheduled runs use the most recent occurrence of their exact UTC cron time.
This preserves the intended JST race date even when GitHub starts the job after
midnight.  Manual runs may explicitly provide a date.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCHEDULE_RE = re.compile(r"^(\d{1,2}) (\d{1,2}) \* \* \*$")


def valid_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ValueError("target date must be YYYY-MM-DD")
    if dt.date.fromisoformat(value).isoformat() != value:
        raise ValueError("target date is invalid")
    return value


def resolve_target_date(
    *,
    event_name: str,
    schedule: str = "",
    manual_date: str = "",
    now_utc: dt.datetime | None = None,
) -> str:
    if manual_date:
        return valid_date(manual_date)
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    if event_name == "schedule":
        match = SCHEDULE_RE.fullmatch(schedule.strip())
        if not match:
            raise ValueError("unrecognized schedule")
        minute, hour = (int(match.group(1)), int(match.group(2)))
        if minute > 59 or hour > 23:
            raise ValueError("invalid schedule")
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < scheduled:
            scheduled -= dt.timedelta(days=1)
        return scheduled.date().isoformat()
    return (now + dt.timedelta(hours=9)).date().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--schedule", default="")
    parser.add_argument("--manual-date", default="")
    args = parser.parse_args()
    print(
        resolve_target_date(
            event_name=args.event_name,
            schedule=args.schedule,
            manual_date=args.manual_date,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

