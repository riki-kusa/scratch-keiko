#!/usr/bin/env python3
"""Bounded, read-only Scratch API measurement. Python 3.11+, standard library."""
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import email.utils
import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser

BASE = 'https://api.scratch.mit.edu'
SITE = 'https://scratch.mit.edu'
USER_AGENT = 'Scratch-KEIKO-Research-Probe/0.2 (limited read-only experiment)'
METRICS = ('views', 'loves', 'favorites')
HEADERS = ('Cache-Control', 'Age', 'ETag', 'Date', 'Last-Modified', 'Vary',
           'Expires', 'X-Cache', 'CF-Cache-Status', 'Retry-After', 'Content-Type',
           'Content-Encoding', 'Via')
MAX_BODY = 2_000_000
FEATURED = ('community_newest_projects', 'community_most_remixed_projects',
            'curator_top_projects', 'community_most_loved_projects',
            'community_featured_projects', 'scratch_design_studio')


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso(value=None):
    return (value or utc_now()).isoformat(timespec='milliseconds')


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def append_jsonl(path, value):
    # One normalized HTTP attempt and its rows form one journal entry.
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def read_logs(data):
    for path in sorted(data.glob('runs/*.jsonl')):
        lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
        for index, line in enumerate(lines):
            if not line.endswith('\n') and index == len(lines) - 1:
                # Interrupted write. Earlier complete entries remain replayable.
                continue
            yield json.loads(line)


def parse_stats(project):
    raw = project.get('stats')
    if not isinstance(raw, dict):
        raw = project
    stats = {k: raw.get(k) if type(raw.get(k)) is int and raw[k] >= 0 else None for k in METRICS}
    return stats if any(value is not None for value in stats.values()) else None


def string(value):
    return value if isinstance(value, str) else None


def normalize(project, section=None):
    author = project.get('author')
    if isinstance(author, dict):
        author = author.get('username')
    history = project.get('history')
    if not isinstance(history, dict):
        history = {}
    return {
        'id': project.get('id', project.get('project_id')),
        'title': string(project.get('title', project.get('project_title'))),
        'author': string(author or project.get('creator', project.get('username'))),
        'created': string(history.get('created')),
        'shared': string(history.get('shared')),
        'modified': string(history.get('modified')),
        'stats': parse_stats(project),
        'public': project.get('public') if type(project.get('public')) is bool else None,
        'visibility': string(project.get('visibility')),
        'section': section,
    }


class ProjectLinks(HTMLParser):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.known = False
        self.in_list = False
        self.in_item = False
        self.rows = []
        self.anchor = None
        self.text = []
        self.icons = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get('class') or '').split())
        if tag == 'ul' and 'activity-stream' in classes and self.kind == 'activity':
            self.known = self.in_list = True
        if tag == 'li':
            self.in_item = self.in_list if self.kind == 'activity' else {'project', 'thumb', 'item'} <= classes
            self.known |= self.in_item
            self.icons = []
        if self.in_item and tag == 'span':
            self.icons.extend(sorted(classes & {'love', 'favorite', 'project'}))
        if self.in_item and tag == 'a':
            href = attrs.get('href') or ''
            match = re.fullmatch(r'(?:https://scratch\.mit\.edu)?/projects/(\d+)/?(?:[?#].*)?', href)
            if match:
                self.anchor = int(match[1])
                self.text = []

    def handle_data(self, value):
        if self.anchor is not None:
            self.text.append(value)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            self.rows.append(normalize({'id': self.anchor, 'title': ''.join(self.text).strip() or None},
                                       self.icons[0] if self.icons else None))
            self.anchor = None
        if tag == 'li':
            self.in_item = False
        if tag == 'ul' and self.in_list:
            self.in_list = False


@dataclass(frozen=True)
class Route:
    name: str
    path: str
    kind: str
    purpose: str = 'discovery'

    @property
    def url(self):
        return (SITE if self.kind in ('activity', 'loves') else BASE) + self.path


def parse_body(route, body):
    if route.kind in ('activity', 'loves'):
        parser = ProjectLinks(route.kind)
        parser.feed(body.decode('utf-8'))
        if not parser.known:
            raise ValueError('expected Scratch HTML project container not found')
        return parser.rows
    content = json.loads(body)
    if route.kind == 'detail':
        if not isinstance(content, dict) or content.get('id') != int(route.name.split(':')[1]):
            raise ValueError('detail ID missing or mismatched')
        row = normalize(content)
        if not row['stats'] or any(row['stats'][metric] is None for metric in METRICS):
            raise ValueError('detail statistics missing or incomplete')
        return [row]
    if route.kind == 'featured':
        if not isinstance(content, dict) or not any(isinstance(content.get(key), list) for key in FEATURED):
            raise ValueError('expected featured project sections')
        return [normalize(row, key) for key in FEATURED if isinstance(content.get(key), list) for row in content[key]
                if isinstance(row, dict) and type(row.get('id', row.get('project_id'))) is int]
    if not isinstance(content, list) or any(not isinstance(row, dict) or type(row.get('id')) is not int for row in content):
        raise ValueError('expected project list with numeric IDs')
    return [normalize(row) for row in content]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Fetcher:
    def __init__(self, cfg, journal, run, deadline=None):
        self.cfg = cfg
        self.journal = journal
        self.run = run
        self.deadline = deadline
        self.lock = threading.Lock()
        self.count = 0
        self.next_start = 0
        self.stopped = None
        self.opener = urllib.request.build_opener(NoRedirect)

    def reserve(self):
        while True:
            with self.lock:
                if self.stopped:
                    return None
                if self.deadline is not None and utc_now() >= self.deadline:
                    self.stopped = 'campaign-end'
                    return None
                if self.count >= self.cfg['max_requests']:
                    self.stopped = 'request-cap'
                    return None
                remaining = self.next_start - time.monotonic()
                if remaining <= 0:
                    self.next_start = time.monotonic() + self.cfg['interval_seconds']
                    self.count += 1
                    return self.count
            time.sleep(min(remaining, 0.5))

    def wait_after(self, headers, attempt, final_rate_limit):
        value = headers.get('Retry-After')
        wait = float(3 * 2 ** (attempt - 1))
        if value:
            try:
                retry_after = float(value)
            except ValueError:
                try:
                    retry_after = (email.utils.parsedate_to_datetime(value) - utc_now()).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    retry_after = 0
            wait = max(wait, retry_after)
        with self.lock:
            if final_rate_limit or wait > self.cfg['max_backoff_seconds']:
                self.stopped = 'repeated-rate-limit' if final_rate_limit else 'retry-after-exceeds-wait-budget'
                wait = max(wait, 900) if final_rate_limit else wait
                append_jsonl(self.journal, {
                    'event': 'cooldown', 'run': self.run, 'utc': iso(),
                    'resume_not_before': iso(utc_now() + dt.timedelta(seconds=min(wait, 31_536_000))),
                })
            else:
                self.next_start = max(self.next_start, time.monotonic() + wait)

    def fetch(self, route):
        for attempt in range(1, self.cfg['retries'] + 2):
            serial = self.reserve()
            if serial is None:
                return None
            started = time.monotonic()
            began = iso()
            headers, status, raw, rows, error, retry = {}, None, b'', [], None, False
            request = urllib.request.Request(route.url, headers={
                'User-Agent': USER_AGENT, 'Accept': 'application/json, text/html;q=0.8',
                'Accept-Encoding': 'gzip',
            })
            try:
                with self.opener.open(request, timeout=self.cfg['timeout_seconds']) as response:
                    status = response.status
                    headers = {key: response.headers[key] for key in HEADERS if key in response.headers}
                    raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise ValueError('response exceeds body limit')
                if headers.get('Content-Encoding', '').lower() == 'gzip':
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                        body = compressed.read(MAX_BODY + 1)
                    if len(body) > MAX_BODY:
                        raise ValueError('decoded response exceeds body limit')
                else:
                    body = raw
                rows = parse_body(route, body)
            except urllib.error.HTTPError as exc:
                with contextlib.closing(exc):
                    status = exc.code
                    headers = {key: exc.headers[key] for key in HEADERS if key in exc.headers}
                    # Error bodies are unnecessary; their transfer size is unmeasured.
                error = f'HTTP {status}'
                retry = status == 429 or 500 <= status <= 599
            except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as exc:
                error = type(exc).__name__ + ': ' + str(exc)[:170]
                retry = True
            except (ValueError, EOFError, OSError) as exc:
                error = type(exc).__name__ + ': ' + str(exc)[:170]
            finished = iso()
            record = {
                'event': 'attempt', 'run': self.run, 'serial': serial, 'attempt': attempt,
                'route': route.name, 'kind': route.kind, 'purpose': route.purpose,
                'url': route.url, 'started_utc': began, 'utc': finished, 'status': status,
                'ok': status == 200 and error is None, 'error': error,
                'elapsed_seconds': round(time.monotonic() - started, 4),
                'body_bytes_read': len(raw), 'headers': headers,
                'body_sha256': hashlib.sha256(raw).hexdigest() if raw else None,
                'projects': rows,
            }
            if retry:
                self.wait_after(headers, attempt, status == 429 and attempt > self.cfg['retries'])
            with self.lock:
                append_jsonl(self.journal, record)
                print(f'{serial:02} {route.name}: {status} {len(rows)} rows {record["elapsed_seconds"]:.2f}s'
                      + (f' [{error}]' if error else ''), flush=True)
            if not retry or attempt > self.cfg['retries']:
                return record
        return None

    def batch(self, routes):
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.cfg['parallelism']) as pool:
            return list(filter(None, pool.map(self.fetch, routes)))


def query(path, params):
    return path + '?' + urllib.parse.urlencode(params)


def routes_for(cfg, daily, day):
    limit = cfg['list_limit']
    routes = [Route('detail:' + str(cfg['anchor_id']), '/projects/' + str(cfg['anchor_id']), 'detail', 'reference')]
    if not daily:
        return routes
    for index, title in enumerate(cfg['anchor_queries']):
        routes.append(Route(f'anchor-title:{index}', query('/search/projects',
                            {'q': title, 'mode': 'popular', 'language': 'en', 'limit': limit}),
                            'search', 'anchor'))
    for index, params in enumerate(cfg['searches']):
        routes.append(Route(f'search:{index}', query('/search/projects', {'limit': limit, **params}), 'search'))
    for lang in ('ja', 'en', 'es'):
        routes.append(Route(f'explore:{lang}', query('/explore/projects',
                            {'q': '*', 'mode': 'trending', 'language': lang, 'limit': limit}), 'explore'))
    for index, user in enumerate(cfg['users']):
        name = user['name']
        slug = urllib.parse.quote(name, safe='')
        routes.extend([
            Route(f'author:{name}', query(f'/users/{slug}/projects', {'limit': limit}), 'author'),
            Route(f'favorites:{name}', query(f'/users/{slug}/favorites', {'limit': limit}), 'favorites'),
        ])
        # Include all discovery routes in the pilot, then rotate the HTML sources.
        if day == 0 or (day + index) % 2 == 0:
            routes.extend([
                Route(f'activity:{name}', query('/messages/ajax/user-activity/', {'user': name, 'max': 40}), 'activity'),
                Route(f'loves:{name}', f'/projects/all/{slug}/loves/?page=1', 'loves'),
            ])
    routes.extend(Route(f'studio:{sid}', query(f'/studios/{sid}/projects', {'limit': limit}), 'studio')
                  for sid in cfg['studios'])
    routes.append(Route('featured', '/proxy/featured', 'featured'))
    return routes


def make_panel(cfg, records):
    by_group = {}
    used = {cfg['anchor_id']}
    # Fixed, deterministic sample. Select from independent sources before looking at search.
    for group, kinds in (('author', {'author'}), ('community', {'favorites', 'studio', 'activity', 'loves'}),
                         ('search', {'search', 'explore'})):
        candidates = {}
        for record in records:
            if record['kind'] not in kinds or record['purpose'] != 'discovery':
                continue
            for row in record['projects']:
                if row['id'] not in used:
                    existing = candidates.get(row['id'])
                    if existing is None or (not existing[1]['title'] and row['title']):
                        candidates[row['id']] = (record['route'], row)
        selected, route_counts = [], {}
        for pid in sorted(candidates, key=lambda pid: hashlib.sha256(
                f'{cfg["panel_seed"]}:{group}:{pid}'.encode()).hexdigest()):
            route, row = candidates[pid]
            if route_counts.get(route, 0) >= 1:
                continue
            selected.append({'id': pid, 'title': row['title'], 'seed_route': route, 'group': group})
            route_counts[route] = 1
            used.add(pid)
            if len(selected) >= cfg['panel_per_group']:
                break
        by_group[group] = selected
    return [item for group in by_group.values() for item in group]


def check_config(cfg):
    bounds = {
        'interval_seconds': (1, 60), 'parallelism': (1, 4), 'max_requests': (1, 160),
        'timeout_seconds': (3, 40), 'retries': (0, 2), 'max_backoff_seconds': (3, 120),
        'list_limit': (1, 40), 'panel_per_group': (1, 6), 'exact_title_checks': (0, 12),
        'days': (28, 28), 'anchor_id': (1, 10**12), 'panel_seed': (0, 10**12),
        **{f'burst_{k}_per_6h': (1, 10**9) for k in METRICS},
    }
    for key, (lower, upper) in bounds.items():
        if type(cfg.get(key)) is not int or not lower <= cfg[key] <= upper:
            raise ValueError(f'{key}: expected integer in [{lower}, {upper}]')
    for item in cfg['users']:
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', item['name']):
            raise ValueError('invalid user name')


def measure(args):
    cfg = load_json(args.config)
    check_config(cfg)
    data = args.data
    state_file = data / 'campaign.json'
    if args.mode == 'campaign' and not args.start and not state_file.exists():
        print('Campaign inactive. Start it with workflow_dispatch and start_campaign=true.')
        return
    (data / 'runs').mkdir(parents=True, exist_ok=True)
    logs = list(read_logs(data))
    if any(x['event'] == 'cooldown' and dt.datetime.fromisoformat(x['resume_not_before']) > utc_now() for x in logs):
        print('Paused until the recorded Retry-After has elapsed.')
        return
    slot, day, daily, deadline = 0, 0, True, None
    if args.mode == 'campaign':
        if not state_file.exists():
            if logs:
                raise ValueError('campaign metadata missing; restore campaign.json before continuing')
            write_json(state_file, {'started_utc': iso(), 'days': cfg['days'], 'config': cfg})
        state = load_json(state_file)
        if state['config'] != cfg:
            raise ValueError('campaign configuration changed; restore the original config or use a new data directory')
        start = dt.datetime.fromisoformat(state['started_utc'])
        deadline = start + dt.timedelta(days=state['days'])
        slot = int((utc_now() - start).total_seconds() // 21600)
        if slot < 0 or utc_now() >= deadline:
            print('Outside the 28-day observation window.')
            return
        if any(x['event'] == 'end' and x['slot'] == slot for x in logs):
            print(f'Slot {slot} already recorded.')
            return
        day = slot // 4
        daily = not any(x['event'] == 'end' and x['day'] == day and x['daily'] for x in logs)
    elif any(x.get('mode') == 'campaign' for x in logs if x['event'] == 'start') or state_file.exists():
        raise ValueError('pilot and campaign must use separate data directories')

    run = utc_now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    journal = data / 'runs' / f'{run}.jsonl'
    started = time.monotonic()
    append_jsonl(journal, {'event': 'start', 'run': run, 'utc': iso(), 'slot': slot, 'day': day,
                           'daily': daily, 'mode': args.mode, 'config': cfg})
    fetcher = Fetcher(cfg, journal, run, deadline)
    panel_file = data / 'panel.json'
    panel = load_json(panel_file) if panel_file.exists() else []
    routes = routes_for(cfg, daily, day)
    if panel:
        routes[1:1] = detail_routes(panel)
    def plan(items):
        append_jsonl(journal, {'event': 'plan', 'run': run, 'routes': [vars(route) for route in items]})

    plan(routes)
    # Probe the comparison target first, then run the bounded discovery batch.
    fetcher.fetch(routes[0])
    records = fetcher.batch(routes[1:])
    if not panel and daily:
        proposed = make_panel(cfg, [r for r in records if r['ok']])
        if proposed:
            panel = proposed
            write_json(panel_file, panel)
            reference_routes = detail_routes(panel)
            plan(reference_routes)
            records += fetcher.batch(reference_routes)
    if daily and panel:
        detail_titles = {r['projects'][0]['id']: r['projects'][0]['title'] for r in records
                         if r['ok'] and r['kind'] == 'detail'}
        independent = [{**p, 'title': p['title'] or detail_titles.get(p['id'])}
                       for p in panel if p['group'] != 'search']
        independent = [p for p in independent if p['title']]
        checks = [Route(f'check-title:{p["id"]}', query('/search/projects',
                           {'q': p['title'], 'mode': 'popular', 'language': 'en', 'limit': cfg['list_limit']}),
                           'search', 'independent-check') for p in independent[:cfg['exact_title_checks']]]
        plan(checks)
        fetcher.batch(checks)
    append_jsonl(journal, {'event': 'end', 'run': run, 'utc': iso(), 'slot': slot, 'day': day,
                           'daily': daily, 'attempts': fetcher.count, 'stop_reason': fetcher.stopped,
                           'elapsed_seconds': round(time.monotonic() - started, 4)})


def detail_routes(panel):
    return [Route(f'detail:{p["id"]}', f'/projects/{p["id"]}', 'detail', 'reference') for p in panel]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=pathlib.Path, default=pathlib.Path('config.json'))
    parser.add_argument('--data', type=pathlib.Path, required=True)
    parser.add_argument('--mode', choices=['pilot', 'campaign'], default='pilot')
    parser.add_argument('--start', action='store_true', help='Arm a new campaign if it does not exist')
    args = parser.parse_args()
    measure(args)


if __name__ == '__main__':
    main()
