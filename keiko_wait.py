#!/usr/bin/env python3
"""Keep a dispatched runner available for the next observation opportunity."""
import datetime as dt
import pathlib
import time

from keiko_probe import iso, load_json, read_logs, utc_now

MAX_WAIT_SECONDS = 4 * 3600
DATA = pathlib.Path('data/campaign')


def wait_until(data, now):
    path = data / 'campaign.json'
    if not path.exists():
        return now
    campaign = load_json(path)
    start = dt.datetime.fromisoformat(campaign['started_utc'])
    deadline = start + dt.timedelta(days=campaign['days'])
    if now >= deadline:
        return now
    slot = int((now - start).total_seconds() // 21600)
    if slot < 0:
        return min(start, now + dt.timedelta(seconds=MAX_WAIT_SECONDS))
    logs = list(read_logs(data))
    completed = any(row['event'] == 'end' and row['slot'] == slot for row in logs)
    # Incomplete slots get a bounded retry; never create an immediate dispatch loop.
    target = (start + dt.timedelta(hours=6 * (slot + 1), seconds=60)
              if completed else now + dt.timedelta(seconds=60))
    cooldowns = [dt.datetime.fromisoformat(row['resume_not_before'])
                 for row in logs if row['event'] == 'cooldown']
    if cooldowns:
        target = max(target, *cooldowns)
    return min(target, deadline, now + dt.timedelta(seconds=MAX_WAIT_SECONDS))


def main():
    target = wait_until(DATA, utc_now())
    print(f'Continuation waiting until {iso(target)} (UTC); no Scratch API requests during this wait.',
          flush=True)
    monotonic_deadline = time.monotonic() + MAX_WAIT_SECONDS
    while utc_now() < target and time.monotonic() < monotonic_deadline:
        time.sleep(min(60, max(0, (target - utc_now()).total_seconds()),
                       max(0, monotonic_deadline - time.monotonic())))
    print('Continuation wait complete.', flush=True)


if __name__ == '__main__':
    main()
