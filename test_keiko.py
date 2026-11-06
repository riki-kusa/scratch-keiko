"""Regression checks with synthetic HTTP responses. No network calls."""
import datetime as dt
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch

from keiko_probe import Fetcher, Route, append_jsonl, iso, measure, read_logs, utc_now, write_json
from report import build_report, replay

CONFIG_PATH = pathlib.Path(__file__).with_name('config.json')
CONFIG = json.loads(CONFIG_PATH.read_text())
SEARCH = Route('search:test', '/search/projects', 'search')


def response(body):
    reply = io.BytesIO(body)
    reply.status = 200
    reply.headers = Message()
    return reply


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers['Retry-After'] = str(retry_after)
    return urllib.error.HTTPError(SEARCH.url, code, 'synthetic', headers, io.BytesIO())


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = pathlib.Path(temp.name)
        (self.data / 'runs').mkdir()
        self.journal = self.data / 'runs' / 'test.jsonl'
        self.cfg = {**CONFIG, 'max_requests': 2, 'retries': 1}
        self.fetcher = Fetcher(self.cfg, self.journal, 'test')
        self.fetcher.opener = Mock()

    def test_retry_attempts_logged_and_capped(self):
        self.fetcher.opener.open.side_effect = [http_error(503), response(b'[]')]
        with patch.object(self.fetcher, 'wait_after', wraps=self.fetcher.wait_after) as wait:
            # Inject the wait clock so the test does not sleep.
            with patch('keiko_probe.time.sleep', side_effect=lambda _: setattr(self.fetcher, 'next_start', 0)):
                result = self.fetcher.fetch(SEARCH)
        self.assertTrue(result['ok'])
        self.assertEqual(wait.call_count, 1)
        self.assertIsNone(self.fetcher.fetch(SEARCH))
        self.assertEqual([e['status'] for e in read_logs(self.data)], [503, 200])

    def test_long_retry_after_records_persistent_cooldown(self):
        self.fetcher.opener.open.side_effect = http_error(429, 120)
        self.assertIsNone(self.fetcher.fetch(SEARCH))
        entries = list(read_logs(self.data))
        self.assertEqual([e['event'] for e in entries], ['cooldown', 'attempt'])
        self.assertGreater(dt.datetime.fromisoformat(entries[0]['resume_not_before']), utc_now())
        self.assertEqual(entries[-1]['status'], 429)
        self.assertEqual(self.fetcher.opener.open.call_count, 1)

    def test_repeat_429_stops_other_routes(self):
        self.fetcher.opener.open.side_effect = [http_error(429, 0), http_error(429, 0)]
        with patch('keiko_probe.time.sleep', side_effect=lambda _: setattr(self.fetcher, 'next_start', 0)):
            result = self.fetcher.fetch(SEARCH)
        self.assertFalse(result['ok'])
        self.assertIsNone(self.fetcher.fetch(SEARCH))
        self.assertEqual(self.fetcher.stopped, 'repeated-rate-limit')

    def test_html_200_from_json_endpoint_is_failure(self):
        self.fetcher.opener.open.return_value = response(b'<html>unavailable</html>')
        self.assertFalse(self.fetcher.fetch(SEARCH)['ok'])
        self.assertEqual(list(read_logs(self.data))[0]['projects'], [])

    def test_partial_journal_is_recoverable(self):
        append_jsonl(self.journal, {'event': 'plan', 'run': 'test', 'routes': []})
        with self.journal.open('a') as stream:
            stream.write('{"event":')
        self.assertEqual(len(list(read_logs(self.data))), 1)
        build_report(self.data, self.data / 'out')

    def test_inactive_expired_and_complete_slot_skip_http(self):
        args = SimpleNamespace(config=CONFIG_PATH, data=self.data, mode='campaign', start=False)
        with patch('keiko_probe.Fetcher') as fetcher:
            measure(args)
            write_json(self.data / 'campaign.json', {'started_utc': iso(utc_now() - dt.timedelta(days=29)),
                       'days': 28, 'config': CONFIG})
            measure(args)
            write_json(self.data / 'campaign.json', {'started_utc': iso(), 'days': 28, 'config': CONFIG})
            append_jsonl(self.journal, {'event': 'end', 'slot': 0})
            measure(args)
        fetcher.assert_not_called()

    def test_short_window_missing_differs_from_no_burst(self):
        pid = CONFIG['anchor_id']
        starts, requests = [], []
        first = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        for slot, views in enumerate((100, 300)):
            run = str(slot)
            stamp = iso(first + dt.timedelta(hours=6 * slot))
            starts.append({'run': run, 'utc': stamp, 'daily': slot == 0})
            requests.append({'run': run, 'kind': 'detail', 'route': f'detail:{pid}', 'utc': stamp,
                             'ok': True, 'body_bytes_read': 100, 'elapsed_seconds': 1,
                             'projects': [{'id': pid, 'stats': {'views': views, 'loves': 0, 'favorites': 0}}]})
        policies = {p['policy']: p for p in replay(CONFIG, starts, requests, [])}
        self.assertEqual(policies['individual']['detected_spikes'], 1)
        self.assertEqual(policies['bulk']['missed_no_comparable_window'], 1)


if __name__ == '__main__':
    unittest.main()
