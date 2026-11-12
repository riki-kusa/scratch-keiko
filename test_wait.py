import datetime as dt
import pathlib
import tempfile
import unittest

from keiko_probe import append_jsonl, iso, write_json
from keiko_wait import MAX_WAIT_SECONDS, wait_until


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = pathlib.Path(self.temp.name)
        (self.data / 'runs').mkdir()
        self.start = dt.datetime(2026, 11, 6, 1, 49, 29, tzinfo=dt.timezone.utc)
        write_json(self.data / 'campaign.json', {'started_utc': iso(self.start), 'days': 28})

    def test_completed_slot_waits_for_next_slot(self):
        append_jsonl(self.data / 'runs' / 'first.jsonl', {'event': 'end', 'slot': 0})
        now = self.start + dt.timedelta(hours=4)
        self.assertEqual(wait_until(self.data, now), self.start + dt.timedelta(hours=6, seconds=60))

    def test_wait_is_bounded_before_runner_limit(self):
        append_jsonl(self.data / 'runs' / 'first.jsonl', {'event': 'end', 'slot': 0})
        now = self.start + dt.timedelta(minutes=1)
        self.assertEqual(wait_until(self.data, now), now + dt.timedelta(seconds=MAX_WAIT_SECONDS))

    def test_incomplete_slot_retries_without_busy_loop(self):
        now = self.start + dt.timedelta(minutes=1)
        self.assertEqual(wait_until(self.data, now), now + dt.timedelta(seconds=60))

    def test_retry_after_is_respected(self):
        now = self.start + dt.timedelta(hours=4)
        until = now + dt.timedelta(hours=1)
        append_jsonl(self.data / 'runs' / 'first.jsonl',
                     {'event': 'cooldown', 'resume_not_before': iso(until)})
        self.assertEqual(wait_until(self.data, now), until)

    def test_wait_ends_at_campaign_deadline(self):
        now = self.start + dt.timedelta(days=28, seconds=-20)
        self.assertEqual(wait_until(self.data, now), self.start + dt.timedelta(days=28))
        self.assertEqual(wait_until(self.data, now + dt.timedelta(seconds=20)),
                         now + dt.timedelta(seconds=20))


if __name__ == '__main__':
    unittest.main()
