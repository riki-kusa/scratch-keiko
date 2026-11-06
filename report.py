#!/usr/bin/env python3
"""Rebuild every report offline from normalized observation journals."""
import argparse
import csv
import datetime as dt
import json
import pathlib
import statistics
from collections import Counter, defaultdict
from itertools import combinations

from keiko_probe import METRICS, iso, load_json, read_logs, write_json

JST = dt.timezone(dt.timedelta(hours=9))
KINDS = ('search', 'explore', 'author', 'favorites', 'studio', 'activity', 'loves', 'featured', 'detail')
BULK = {'author', 'favorites'}


def elapsed_hours(before, after):
    return (dt.datetime.fromisoformat(after) - dt.datetime.fromisoformat(before)).total_seconds() / 3600


def as_jst(stamp):
    return dt.datetime.fromisoformat(stamp).astimezone(JST).strftime('%Y-%m-%d %H:%M:%S JST')


def ratio(a, b):
    return round(a / b, 4) if b else None


def average(values):
    return round(statistics.mean(values), 3) if values else None


def numbers(row):
    stats = row.get('stats')
    return stats if stats and all(stats[k] is not None for k in METRICS) else None


def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            # Public titles may start with formula characters. Keep spreadsheets from executing them.
            writer.writerow({key: "'" + value if isinstance(value, str) and value.lstrip().startswith(
                ('=', '+', '-', '@')) else value for key, value in row.items()})


def markdown_table(headers, rows):
    def cell(value):
        if value is None:
            return '—'
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('<', '&lt;').replace('>', '&gt;')
    return '\n'.join(['| ' + ' | '.join(headers) + ' |',
                     '| ' + ' | '.join('---' for _ in headers) + ' |'] +
                     ['| ' + ' | '.join(map(cell, row)) + ' |' for row in rows])


def discovery_metrics(requests):
    discovery = [r for r in requests if r['purpose'] == 'discovery' and r['kind'] != 'detail']
    found_by_kind = {kind: set() for kind in KINDS if kind != 'detail'}
    route_ids = defaultdict(set)
    for req in discovery:
        if req['ok']:
            ids = {p['id'] for p in req['projects']}
            found_by_kind[req['kind']].update(ids)
            route_ids[req['route']].update(ids)
    union = set().union(*found_by_kind.values())
    rows = []
    for kind in KINDS:
        group = [r for r in requests if r['kind'] == kind]
        operations = defaultdict(list)
        for req in group:
            operations[(req['run'], req['route'])].append(req)
        # Detailed target checks and anchor searches are counted as requests,
        # but their deliberately targeted IDs do not inflate discovery counts.
        selected = [r for r in group if r['ok'] and (r['purpose'] == 'discovery' or kind == 'detail')]
        projects = [p for r in selected for p in r['projects']]
        ids = {p['id'] for p in projects}
        exclusive = ids - set().union(*(s for k, s in found_by_kind.items() if k != kind)) if kind != 'detail' else set()
        discover_count = sum(r['purpose'] == 'discovery' for r in group)
        rows.append({
            'kind': kind, 'attempts': len(group), 'http_200': sum(r['status'] == 200 for r in group),
            'valid_attempts': sum(r['ok'] for r in group),
            'operations_attempted': len(operations),
            'operations_ok': sum(any(r['ok'] for r in op) for op in operations.values()),
            'attempt_success_rate': ratio(sum(r['ok'] for r in group), len(group)),
            'operation_success_rate': ratio(sum(any(r['ok'] for r in op) for op in operations.values()), len(operations)),
            'statuses': dict(Counter(str(r['status']) for r in group)),
            'bytes_read': sum(r['body_bytes_read'] for r in group),
            'latency_mean_s': average([r['elapsed_seconds'] for r in group]),
            'latency_median_s': round(statistics.median([r['elapsed_seconds'] for r in group]), 3) if group else None,
            'request_seconds_sum': round(sum(r['elapsed_seconds'] for r in group), 3),
            'rows': len(projects), 'distinct_ids': len(ids),
            'repeat_row_rate': ratio(len(projects) - len(ids), len(projects)),
            'cross_source_overlap_rate': ratio(len(ids - exclusive), len(ids)) if kind != 'detail' else None,
            'exclusive_ids': len(exclusive) if kind != 'detail' else None,
            'discovery_attempts_per_exclusive_id': ratio(discover_count, len(exclusive)),
            'discovery_attempts_per_distinct_id': ratio(discover_count, len(ids)) if kind != 'detail' else None,
            'complete_stats_rows': sum(numbers(p) is not None for p in projects),
            'created_rows': sum(bool(p['created']) for p in projects),
            'shared_rows': sum(bool(p['shared']) for p in projects),
            'modified_rows': sum(bool(p['modified']) for p in projects),
            'share_years_distinct_ids': {year: len({p['id'] for p in projects if (p['shared'] or '')[:4] == year})
                                        for year in sorted({p['shared'][:4] for p in projects if p['shared']})},
        })
    matrix = [{'a': a, 'b': b, 'intersection': len(found_by_kind[a] & found_by_kind[b]),
               'jaccard': ratio(len(found_by_kind[a] & found_by_kind[b]), len(found_by_kind[a] | found_by_kind[b]))}
              for a, b in combinations(found_by_kind, 2)]
    matrix.extend({'a': a, 'b': b, 'intersection': len(route_ids[a] & route_ids[b]),
                   'jaccard': ratio(len(route_ids[a] & route_ids[b]), len(route_ids[a] | route_ids[b]))}
                  for a, b in combinations(sorted(route_ids), 2))
    known = set()
    new_by_day = defaultdict(Counter)
    for req in sorted(discovery, key=lambda r: (r['utc'], r['serial'])):
        ids = {p['id'] for p in req['projects']} if req['ok'] else set()
        day = as_jst(req['utc'])[:10]
        new_by_day[day][req['kind']] += len(ids - known)
        known |= ids
    return rows, matrix, union, new_by_day


def observation_fields(req, row):
    return {
        'run': req['run'], 'route': req['route'], 'kind': req['kind'], 'purpose': req['purpose'],
        'observed_utc': req['utc'], 'observed_jst': as_jst(req['utc']), 'http_status': req['status'],
        'age': req['headers'].get('Age'), 'cache_control': req['headers'].get('Cache-Control'),
        'etag': req['headers'].get('ETag'), 'date': req['headers'].get('Date'),
        'last_modified': req['headers'].get('Last-Modified'),
        'body_sha256': req['body_sha256'], 'project_id': row['id'],
        **{k: row[k] for k in ('title', 'author', 'created', 'shared', 'modified', 'public', 'visibility', 'section')},
        **{k: (row['stats'] or {}).get(k) for k in METRICS},
    }


def comparisons_for(requests, anchor):
    reference = {}
    for req in requests:
        if req['kind'] == 'detail' and req['ok']:
            reference[(req['run'], req['projects'][0]['id'])] = req
    paired, titles = [], []
    for req in requests:
        if req['purpose'] in ('anchor', 'independent-check'):
            ref = anchor if req['purpose'] == 'anchor' else int(req['route'].split(':')[1])
            titles.append({
                'run': req['run'], 'observed_utc': req['utc'], 'route': req['route'],
                'url': req['url'], 'http_status': req['status'], 'ok': req['ok'],
                'target_id': ref, 'found': any(p['id'] == ref for p in req['projects']) if req['ok'] else None,
                'returned_rows': len(req['projects']) if req['ok'] else None,
            })
        if not req['ok'] or req['kind'] == 'detail':
            continue
        for row in req['projects']:
            detail = reference.get((req['run'], row['id']))
            if detail is None:
                continue
            target = detail['projects'][0]
            fields = observation_fields(req, row)
            fields['detail_utc'] = detail['utc']
            fields['detail_http_status'] = detail['status']
            fields['detail_age'] = detail['headers'].get('Age')
            fields['detail_cache_control'] = detail['headers'].get('Cache-Control')
            fields['detail_etag'] = detail['headers'].get('ETag')
            fields['detail_minus_list_seconds'] = round(3600 * elapsed_hours(req['utc'], detail['utc']), 3)
            for key in ('title', 'modified', 'public', 'visibility'):
                fields['detail_' + key] = target[key]
                fields[key + '_matches'] = fields[key] == target[key] if fields[key] is not None and target[key] is not None else None
            for metric in METRICS:
                value = target['stats'][metric]
                fields['detail_' + metric] = value
                fields['detail_minus_list_' + metric] = value - fields[metric] if fields[metric] is not None else None
            paired.append(fields)
    return paired, titles


def change_cadence(pairs):
    groups = defaultdict(list)
    for row in pairs:
        if row['kind'] in {'search', 'explore', 'author', 'favorites'} and abs(row['detail_minus_list_seconds']) <= 1800:
            groups[(row['route'], row['project_id'])].append(row)
    results = []
    for (route, pid), rows in sorted(groups.items()):
        ordered = sorted(rows, key=lambda r: r['observed_utc'])
        for metric in METRICS:
            valid = [r for r in ordered if r[metric] is not None]
            intervals = list(zip(valid, valid[1:]))
            gaps = [elapsed_hours(a['observed_utc'], b['observed_utc']) for a, b in intervals]
            results.append({
                'route': route, 'project_id': pid, 'metric': metric,
                'pairs': len(valid), 'median_pair_interval_hours': round(statistics.median(gaps), 3) if gaps else None,
                'list_change_intervals': sum(a[metric] != b[metric] for a, b in intervals),
                'detail_change_intervals': sum(a['detail_' + metric] != b['detail_' + metric] for a, b in intervals),
                'detail_changed_list_unchanged': sum(a[metric] == b[metric] and
                    a['detail_' + metric] != b['detail_' + metric] for a, b in intervals),
            })
    return results


def discrepancies(pairs):
    groups = defaultdict(list)
    for row in pairs:
        if row['kind'] in {'search', 'explore', 'author', 'favorites'} and abs(row['detail_minus_list_seconds']) <= 1800:
            groups[(row['route'], row['project_id'])].append(row)
    rows = []
    for (route, pid), values in sorted(groups.items()):
        sequences, current = [], []
        for item in sorted(values, key=lambda r: r['observed_utc']):
            difference = [key for key in ('title', 'modified') if item[key + '_matches'] is False]
            difference += [k for k in METRICS if item['detail_minus_list_' + k] not in (None, 0)]
            if difference:
                current.append(item)
            elif current:
                complete = all(item['detail_minus_list_' + k] == 0 for k in METRICS)
                comparable = all(item[k + '_matches'] is True for k in ('title', 'modified'))
                sequences.append((current, 'observed-match' if complete and comparable else 'indeterminate'))
                current = []
        if current:
            sequences.append((current, 'unresolved-at-last-pair'))
        for items, state in sequences:
            first, last = items[0], items[-1]
            distinct_http = len({r['date'] for r in items if r['date']})
            changed_http = len({r['body_sha256'] for r in items if r['body_sha256']})
            stable_fields = len({tuple(r[k] for k in ('title', 'modified', *METRICS)) for r in items}) == 1
            low_age = sum(str(r['age']).isdigit() and int(r['age']) <= 120 for r in items)
            gaps = [elapsed_hours(a['observed_utc'], b['observed_utc']) for a, b in zip(items, items[1:])]
            rows.append({'route': route, 'project_id': pid, 'first_utc': first['observed_utc'],
                         'last_utc': last['observed_utc'], 'observed_span_hours': round(elapsed_hours(first['observed_utc'], last['observed_utc']), 3),
                         'max_gap_hours': round(max(gaps), 3) if gaps else None,
                         'paired_mismatches': len(items), 'distinct_http_dates': distinct_http,
                         'distinct_body_hashes': changed_http, 'low_age_pairs': low_age,
                         'same_list_fields': stable_fields,
                         'origin_staleness_candidate': len(items) > 1 and low_age >= 2 and distinct_http >= 2 and stable_fields,
                         'state': state})
    return rows


def spike(cfg, old, new):
    hours = elapsed_hours(old[0], new[0])
    if not 4 <= hours <= 8:
        return None
    return any((new[1][k] - old[1][k]) * 6 / hours >= cfg[f'burst_{k}_per_6h'] for k in METRICS)


def matched_cadence(requests):
    # Compare the same project/time targets under actual bulk-page and detail costs.
    by_run = defaultdict(list)
    for req in requests:
        by_run[req['run']].append(req)
    totals = Counter()
    for run in by_run.values():
        detail = {r['projects'][0]['id']: r for r in run if r['kind'] == 'detail' and r['ok']}
        pages = []
        targets = set()
        for req in run:
            if req['kind'] not in BULK or not req['ok']:
                continue
            age = req['headers'].get('Age', '')
            if str(age).isdigit() and int(age) > 43200:
                continue
            matches = {p['id']: p['stats'] == detail[p['id']]['projects'][0]['stats']
                       for p in req['projects'] if p['id'] in detail and numbers(p)
                       and abs(elapsed_hours(req['utc'], detail[p['id']]['utc'])) <= 0.5}
            if matches:
                pages.append((req, matches))
                targets.update(matches)
        remaining = set(targets)
        while remaining:
            req, matches = max(pages, key=lambda page: (len(page[1].keys() & remaining), -page[0]['serial']))
            covered = remaining & matches.keys()
            attempts = [r for r in run if r['route'] == req['route']]
            totals['bulk_attempts'] += len(attempts)
            totals['bulk_bytes'] += sum(r['body_bytes_read'] for r in attempts)
            totals['bulk_request_ms'] += sum(round(r['elapsed_seconds'] * 1000) for r in attempts)
            totals['exact_matches'] += sum(matches[pid] for pid in covered)
            remaining -= covered
        direct = [r for r in run if r['kind'] == 'detail' and int(r['route'].split(':')[1]) in targets]
        totals['paired_targets'] += len(targets)
        totals['detail_attempts'] += len(direct)
        totals['detail_bytes'] += sum(r['body_bytes_read'] for r in direct)
        totals['detail_request_ms'] += sum(round(r['elapsed_seconds'] * 1000) for r in direct)
    return {key: totals[key] for key in ('paired_targets', 'exact_matches', 'bulk_attempts', 'detail_attempts',
                                        'bulk_bytes', 'detail_bytes', 'bulk_request_ms', 'detail_request_ms')}


def replay(cfg, starts, requests, panel):
    # Use only a fixed panel, including the known case. Replay policies using
    # data they would have received and compare against observed reference.
    ids = {cfg['anchor_id'], *(p['id'] for p in panel)}
    by_run = defaultdict(list)
    for req in requests:
        by_run[req['run']].append(req)
    results = []
    for policy in ('individual', 'bulk', 'hybrid', 'adaptive'):
        total = Counter()
        previous_policy, previous_ref, hot = {}, {}, set()
        baseline_errors = defaultdict(list)
        for start in sorted(starts, key=lambda x: x['utc']):
            run = by_run[start['run']]
            detail = {r['projects'][0]['id']: (r['utc'], r['projects'][0]['stats']) for r in run
                      if r['kind'] == 'detail' and r['ok']}
            chosen, used_bulk = {}, []
            if policy != 'individual' and start['daily']:
                used_bulk = [r for r in run if r['kind'] in BULK]
                for r in sorted(used_bulk, key=lambda item: item['utc']):
                    age = r['headers'].get('Age', '')
                    if not r['ok'] or (str(age).isdigit() and int(age) > 43200):
                        continue
                    for row in r['projects']:
                        if row['id'] in ids and numbers(row):
                            chosen[row['id']] = (r['utc'], row['stats'])
            total['bulk_observations'] += len(chosen)
            if policy == 'individual':
                planned_detail = ids
            elif policy == 'hybrid' and start['daily']:
                planned_detail = ids - chosen.keys()
            elif policy == 'adaptive':
                planned_detail = (ids - chosen.keys()) if start['daily'] else set(hot)
            else:
                planned_detail = set()
            selected = used_bulk + [r for r in run if r['kind'] == 'detail'
                                     and int(r['route'].split(':')[1]) in planned_detail]
            total['bulk_attempts'] += len(used_bulk)
            total['detail_attempts'] += len(selected) - len(used_bulk)
            total['detail_requests_planned'] += len(planned_detail)
            total['bytes_read'] += sum(r['body_bytes_read'] for r in selected)
            total['summed_request_ms'] += sum(round(r['elapsed_seconds'] * 1000) for r in selected)
            for pid in planned_detail & detail.keys():
                chosen[pid] = detail[pid]
            total['observations'] += len(chosen)
            if start['daily']:
                total['daily_panel_slots'] += len(ids)
                total['daily_observed'] += len(chosen)
                total['daily_reference_available'] += len(detail)
                for pid in chosen.keys() & detail.keys():
                    if abs(elapsed_hours(chosen[pid][0], detail[pid][0])) <= 0.5:
                        total['daily_paired'] += 1
                        if chosen[pid][1] == detail[pid][1]:
                            total['daily_exact_matches'] += 1
                        for metric in METRICS:
                            baseline_errors[metric].append(abs(chosen[pid][1][metric] - detail[pid][1][metric]))
            next_hot = set(hot)
            for pid in ids:
                ref_detected = spike(cfg, previous_ref[pid], detail[pid]) if pid in previous_ref and pid in detail else None
                observed = spike(cfg, previous_policy[pid], chosen[pid]) if pid in previous_policy and pid in chosen else None
                if ref_detected is not None:
                    total['reference_windows'] += 1
                    if ref_detected:
                        total['reference_spikes'] += 1
                        total['detected_spikes' if observed is True else
                              'missed_no_comparable_window' if observed is None else 'missed_below_threshold'] += 1
                    elif observed is True:
                        total['unmatched_alerts'] += 1
                if pid in chosen:
                    if pid in previous_policy:
                        hours = elapsed_hours(previous_policy[pid][0], chosen[pid][0])
                        if 0 < hours <= 30:
                            rising = any((chosen[pid][1][k] - previous_policy[pid][1][k]) * 6 / hours
                                         >= cfg[f'burst_{k}_per_6h'] for k in METRICS)
                            if rising:
                                next_hot.add(pid)
                            else:
                                next_hot.discard(pid)
                    previous_policy[pid] = chosen[pid]
            hot = next_hot
            previous_ref.update(detail)
        keys = ('bulk_attempts', 'detail_attempts', 'detail_requests_planned', 'bytes_read', 'summed_request_ms',
                'bulk_observations', 'observations', 'daily_panel_slots', 'daily_observed',
                'daily_reference_available', 'daily_paired', 'daily_exact_matches', 'reference_windows',
                'reference_spikes', 'detected_spikes', 'missed_no_comparable_window', 'missed_below_threshold',
                'unmatched_alerts')
        results.append({'policy': policy, 'panel_size': len(ids), **{key: total[key] for key in keys},
                        **{f'mae_{k}': average(baseline_errors[k]) for k in METRICS}})
    return results


def build_report(data, out):
    events = list(read_logs(data))
    starts = [r for r in events if r['event'] == 'start']
    ends = [r for r in events if r['event'] == 'end']
    requests = sorted((r for r in events if r['event'] == 'attempt'), key=lambda r: (r['utc'], r['serial']))
    state = load_json(data / 'campaign.json') if (data / 'campaign.json').exists() else None
    panel = load_json(data / 'panel.json') if (data / 'panel.json').exists() else []
    cfg = state['config'] if state else (starts[0]['config'] if starts else None)
    anchor = cfg['anchor_id'] if cfg else 1399147684
    source, matrix, union, new_by_day = discovery_metrics(requests)
    paired, titles = comparisons_for(requests, anchor)
    mismatch = discrepancies(paired)
    cadence = change_cadence(paired)
    planned = {(e['run'], r['name'], r['kind']) for e in events if e['event'] == 'plan' for r in e['routes']}
    attempted = {(r['run'], r['route'], r['kind']) for r in requests}
    unattempted = [dict(zip(('run', 'route', 'kind'), row)) for row in sorted(planned - attempted)]
    policies = replay(cfg, starts, requests, panel) if cfg else []
    independent = [p for p in panel if p['group'] != 'search']
    generic_search_ids = {p['id'] for r in requests if r['kind'] == 'search' and r['purpose'] == 'discovery'
                          and r['ok'] for p in r['projects']}
    independent_checks = [{**p, 'in_generic_search': p['id'] in generic_search_ids,
                           'exact_title_attempts': sum(t['target_id'] == p['id'] for t in titles),
                           'exact_title_valid_attempts': sum(t['target_id'] == p['id'] and t['ok'] for t in titles),
                           'exact_title_ever_found': any(t['target_id'] == p['id'] and t['found'] for t in titles)}
                          for p in independent]
    anchor_rows = [observation_fields(r, p) for r in requests for p in r['projects'] if p['id'] == anchor]
    out.mkdir(parents=True, exist_ok=True)
    snapshots = [observation_fields(r, p) for r in requests for p in r['projects']]
    write_csv(out / 'observations.csv', snapshots, list(observation_fields({
        'run': '', 'route': '', 'kind': '', 'purpose': '', 'utc': iso(), 'status': None,
        'headers': {}, 'body_sha256': None,
    }, {k: None for k in ('id', 'title', 'author', 'created', 'shared', 'modified', 'public', 'visibility', 'section', 'stats')})))
    write_csv(out / 'list_detail.csv', paired, list(paired[0]) if paired else ['run', 'route', 'project_id'])
    write_csv(out / 'title_checks.csv', titles, list(titles[0]) if titles else ['run', 'route', 'target_id', 'found'])
    write_csv(out / 'overlap.csv', matrix, ['a', 'b', 'intersection', 'jaccard'])
    write_csv(out / 'discrepancy_spans.csv', mismatch, list(mismatch[0]) if mismatch else ['route', 'project_id', 'observed_span_hours'])
    write_csv(out / 'update_cadence.csv', cadence, list(cadence[0]) if cadence else ['route', 'project_id', 'metric'])
    summary = {
        'mode': 'campaign' if state else 'pilot', 'run_count': len(starts), 'finished_runs': len(ends),
        'completed_slots': sorted({r['slot'] for r in ends}) if state else None,
        'campaign_started': state['started_utc'] if state else None,
        'campaign_ends': iso(dt.datetime.fromisoformat(state['started_utc']) + dt.timedelta(days=state['days'])) if state else None,
        'request_attempts': len(requests), 'http_200': sum(r['status'] == 200 for r in requests),
        'valid_attempts': sum(r['ok'] for r in requests), 'bytes_read': sum(r['body_bytes_read'] for r in requests),
        'measured_run_seconds': round(sum(r['elapsed_seconds'] for r in ends), 3),
        'discovery_ids': len(union), 'discovery_ids_all': sorted(union), 'sources': source,
        'first_observed_jst': as_jst(requests[0]['utc']) if requests else None,
        'last_observed_jst': as_jst(requests[-1]['utc']) if requests else None,
        'journal_bytes': sum(p.stat().st_size for p in data.glob('runs/*.jsonl')),
        'new_by_jst_day_first_seen': new_by_day, 'independent_panel': independent_checks,
        'anchor': anchor_rows, 'anchor_title_checks': [t for t in titles if t['target_id'] == anchor],
        'paired_rows': len(paired), 'mismatch_spans': mismatch, 'unattempted_planned_routes': unattempted,
        'replay': policies, 'matched_cadence': matched_cadence(requests),
        'stops': [{'run': r['run'], 'reason': r['stop_reason']} for r in ends if r['stop_reason']],
        'errors': [{k: r[k] for k in ('route', 'status', 'error', 'utc')} for r in requests if not r['ok']],
    }
    write_json(out / 'summary.json', summary)
    write_markdown(out, summary)
    print(f'{len(requests)} attempts; {summary["valid_attempts"]} valid; {len(union)} discovery IDs; report: {out}')


def write_markdown(out, s):
    m = s['matched_cadence']
    lines = [
        '# Scratch KEIKO API観測レポート',
        f'モード **{s["mode"]}**。実行 {s["run_count"]} 回、終了記録 {s["finished_runs"]} 回。',
        f'観測範囲 {s["first_observed_jst"] or "未測定"} ～ {s["last_observed_jst"] or "未測定"}。',
        f'キャンペーン開始・終了予定（UTC） {s["campaign_started"] or "未開始"} ～ {s["campaign_ends"] or "未開始"}。'
        f' 終了記録のある6時間枠 {len(s["completed_slots"] or [])} / 112。',
        f'HTTP試行 **{s["request_attempts"]}** 回、200応答 **{s["http_200"]}** 回、有効 **{s["valid_attempts"]}** 回。'
        f' 読み取り本文 **{s["bytes_read"]:,} B**、終了済み実行時間合計 **{s["measured_run_seconds"]}秒**。'
        f' 観測ログ **{s["journal_bytes"]:,} B**。',
        f'発見経路の異なる作品ID **{s["discovery_ids"]}件**。作品を指名した照合検索と個別APIは発見件数の対象外。',
        '## API別の実測',
        markdown_table(['API', '有効／試行', '成功操作／操作', '本文 B', '中央値 秒', '行', '異なるID',
                        '他経路と重複', 'その経路のみ', '試行／独自ID', '3統計あり'],
                       [[r['kind'], f'{r["valid_attempts"]}/{r["attempts"]}',
                         f'{r["operations_ok"]}/{r["operations_attempted"]}', r['bytes_read'], r['latency_median_s'],
                         r['rows'], r['distinct_ids'], None if r['cross_source_overlap_rate'] is None else
                         f'{r["cross_source_overlap_rate"]:.1%}', r['exclusive_ids'],
                         r['discovery_attempts_per_exclusive_id'], r['complete_stats_rows']] for r in s['sources']]),
        'APIごとの「その経路のみ」は、観測期間内に他の測定経路では取得できなかったID。'
        '発見の初出数は summary.json、経路間の積集合・Jaccard係数は overlap.csv。'
        '年別共有ID数は summary.json に記録。同じIDが複数年の共有日時で返れば複数年に計上する。'
        'HTMLの行はリンク数で、同じ作品への画像・題名リンクを含むことがある。本文バイト数は圧縮が返れば圧縮時の値。エラー本文・HTTPヘッダー・通信プロトコルの転送量は未計測。',
        '## 固定事例・独立標本',
        markdown_table(['時刻 JST', '取得先', 'タイトル', '閲覧', 'ハート', 'お気に入り', 'Age', '最終更新 UTC'],
                       [[x['observed_jst'], x['route'], x['title'], x['views'], x['loves'], x['favorites'], x['age'], x['modified']]
                        for x in s['anchor'][-24:]]),
        markdown_table(['時刻 UTC', '取得先', '状態', '対象IDあり', '返却件数'],
                       [[x['observed_utc'], x['route'], x['http_status'], x['found'], x['returned_rows']]
                        for x in s['anchor_title_checks'][-14:]]),
        markdown_table(['ID', '標本の取得先', '通常検索で一度でも観測', '選定時の題名で一度でも観測', '題名検索の試行'],
                       [[x['id'], x['seed_route'], x['in_generic_search'], x['exact_title_ever_found'] if x['exact_title_valid_attempts'] else None,
                         x['exact_title_attempts']] for x in s['independent_panel']]),
        '題名検索の不検出は指定条件・取得ページ内の不検出。検索データベースに未登録とは断定できない。'
        '11月5日の測定は historical/ に別保管。観測がなかった時間帯は連続不一致を保証できない。',
        '## 統計の鮮度',
        f'同一実行の一覧対個別 **{s["paired_rows"]}組**。'
        f'30分以内の比較で不一致区間候補 **{len(s["mismatch_spans"])}件**。'
        f'複数の低Age応答で同じ一覧値が残った区間候補 **{sum(x["origin_staleness_candidate"] for x in s["mismatch_spans"])}件**。',
        markdown_table(['取得先', 'ID', '不一致観測幅 時間', '照合回数', '低Age回数', '一覧値固定', '状態'],
                       [[x['route'], x['project_id'], x['observed_span_hours'], x['paired_mismatches'],
                         x['low_age_pairs'], x['same_list_fields'], x['state']]
                        for x in sorted(s['mismatch_spans'], key=lambda r: -r['observed_span_hours'])[:20]]),
        'Ageが大きい応答はHTTPキャッシュの影響候補。低Ageかつ別Dateの応答でも同じ一覧値が残るときは'
        '配信層より手前のデータ更新遅れを疑う材料になる。個別側にもキャッシュがあり、'
        'この情報だけで内部DBやキャッシュ階層の原因を確定できない。'
        '日・週に及ぶ区間は、同一条件で差が見られた最初と最後の間隔。欠測を挟んだ継続性は不明。'
        '更新を観測した区間数と、個別値だけが変わった区間数は update_cadence.csv に記録。',
        '## 同じ観測枠での一括取得と個別取得',
        f'両方式で30分以内に値を取得できた **{m["paired_targets"]}作品・枠**に限定。'
        f' 一覧 **{m["bulk_attempts"]}試行／{m["bulk_bytes"]:,} B／{m["bulk_request_ms"] / 1000:.2f}秒**。'
        f' 個別 **{m["detail_attempts"]}試行／{m["detail_bytes"]:,} B／{m["detail_request_ms"] / 1000:.2f}秒**。'
        f' 3指標一致 **{m["exact_matches"]}/{m["paired_targets"]}**。',
        '取得済みの一覧から対象を覆うページを事後選択した参考値。取得前に同じページを選べるか、'
        '一致しなかった作品に同じ品質を提供できるかは未確認。',
        '## 4方式の履歴再生',
        markdown_table(['方式', '一覧試行', '個別試行', '本文 B', '応答時間合計 秒', '日次の観測枠',
                        '日次3指標一致／比較可能', '急増検知／参照急増', '短窓欠測／閾値未達'],
                       [[x['policy'], x['bulk_attempts'], x['detail_attempts'], x['bytes_read'], x['summed_request_ms'] / 1000,
                         f'{x["daily_observed"]}/{x["daily_panel_slots"]}',
                         f'{x["daily_exact_matches"]}/{x["daily_paired"]}',
                         f'{x["detected_spikes"]}/{x["reference_spikes"]}',
                         f'{x["missed_no_comparable_window"]}/{x["missed_below_threshold"]}'] for x in s['replay']]),
        '個別の固定標本を参照にした履歴再生。時間はリクエスト時間の合計で、別々に稼働したジョブの実行時間ではない。'
        '日次観測率は同じ標本・日次枠で比較。短窓の急増は4～8時間の連続観測から6時間換算して比較する。'
        '日次方式では短窓の増加を比較できない場合がある。個別参照も欠測し、急増の実数は不明。0/0は評価不可。',
        '## 失敗',
        markdown_table(['時刻 UTC', '取得先', 'HTTP', 'エラー'],
                       [[x['utc'], x['route'], x['status'], x['error']] for x in s['errors'][-30:]]),
        f'全失敗 **{len(s["errors"])}件**。計画記録に対する未試行 **{len(s["unattempted_planned_routes"])}件**。'
        '終了時の停止理由は summary.json、各試行は runs/*.jsonl。'
        '取得失敗の数値は欠測。0への置換は行わない。',
    ]
    (out / 'REPORT.md').write_text('\n\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=pathlib.Path, required=True)
    parser.add_argument('--out', type=pathlib.Path, required=True)
    args = parser.parse_args()
    build_report(args.data, args.out)
