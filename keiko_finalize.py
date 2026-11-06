#!/usr/bin/env python3
"""Finalize the elapsed campaign, then optionally publish its summary to Airtable."""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import urllib.error
import urllib.request

from keiko_probe import iso, load_json, utc_now, write_json
from report import as_jst, build_report

DATA = pathlib.Path('data/campaign')
FINAL = pathlib.Path('reports/final')
# Exact IDs of the existing KEIKO ledger / 検証計画 table.
BASE = 'appCIacJUxxCh8pHf'
TABLE = 'tblQnbO1MTTtoRQAR'
NAME, PURPOSE, METHOD, FINDINGS = (
    'fldKwDF7v5s8WGI6H', 'fldoQepvODpirc5Yi',
    'fldHxFHGMsz9pLVd9', 'fldS0Ontplc5CN2Wn',
)


def status(data=DATA, final=FINAL):
    state_path = data / 'campaign.json'
    if not state_path.exists():
        return 'inactive'
    state = load_json(state_path)
    end = dt.datetime.fromisoformat(state['started_utc']) + dt.timedelta(days=state['days'])
    if utc_now() < end:
        return 'observing'
    if not (final / 'completion.json').exists():
        return 'finalize'
    return 'done' if (final / 'airtable_receipt.json').exists() else 'airtable-pending'


def finalize(data=DATA, final=FINAL):
    if status(data, final) != 'finalize':
        return
    build_report(data, final)
    summary = load_json(final / 'summary.json')
    slots = sorted(set(range(112)) - set(summary['completed_slots']))
    completion = {
        'started_utc': summary['campaign_started'],
        'deadline_utc': summary['campaign_ends'],
        'finalized_utc': iso(),
        'completed_slots': len(summary['completed_slots']),
        'missing_slots': slots,
        'valid_http_attempts': summary['valid_attempts'],
        'total_http_attempts': summary['request_attempts'],
        'discovery_ids': summary['discovery_ids'],
    }
    report_path = final / 'REPORT.md'
    with report_path.open('a', encoding='utf-8') as stream:
        stream.write('\n## 観測期間の確定\n\n')
        stream.write(f'- 開始 {as_jst(completion["started_utc"])}\n')
        stream.write(f'- 観測期限 {as_jst(completion["deadline_utc"])}\n')
        stream.write(f'- 最終集計 {as_jst(completion["finalized_utc"])}\n')
        stream.write(f'- 終了記録あり {completion["completed_slots"]}/112枠\n')
        stream.write('- 終了記録なしの枠番号 ' + (', '.join(map(str, slots)) if slots else 'なし') + '\n')
        stream.write('\n終了記録がある枠にもHTTP失敗が含まれます。枠番号は0始まりです。\n')
    # Written last. An interrupted report generation is retried in the next job.
    write_json(final / 'completion.json', completion)


def airtable_payload(summary, completion, report_url):
    sources = '、'.join(
        f'{row["kind"]} {row["valid_attempts"]}/{row["attempts"]}有効'
        for row in summary['sources']
    )
    return {
        'performUpsert': {'fieldsToMergeOn': [NAME]},
        'records': [{'fields': {
            NAME: f'Scratch KEIKO 28日観測 最終集計 {completion["started_utc"]}',
            PURPOSE: 'Scratch APIの取得経路・成功率・発見範囲・統計の鮮度を比較した28日間の検証。',
            METHOD: (
                f'{as_jst(completion["started_utc"])} ～ {as_jst(completion["deadline_utc"])}。'
                f'終了記録 {completion["completed_slots"]}/112枠。\n最終レポート {report_url}'
            ),
            FINDINGS: (
                f'有効 {summary["valid_attempts"]}/{summary["request_attempts"]}試行、'
                f'異なる発見ID {summary["discovery_ids"]:,}件、一覧対個別 {summary["paired_rows"]}組。\n'
                f'{sources}。\n観測経路と標本に限った結果。Scratch全体の網羅率は未測定。'
            ),
        }}],
    }


def publish_airtable(final=FINAL):
    receipt = final / 'airtable_receipt.json'
    if receipt.exists():
        return
    token = os.getenv('AIRTABLE_PAT', '').strip()
    if not token:
        print('::notice::AIRTABLE_PAT is not configured. The final report is saved; Airtable publication remains pending.')
        return
    summary = load_json(final / 'summary.json')
    completion = load_json(final / 'completion.json')
    server = os.environ['GITHUB_SERVER_URL']
    repository = os.environ['GITHUB_REPOSITORY']
    # Use the commit containing the durable report rather than an artifact URL.
    commit = os.environ['KEIKO_REPORT_COMMIT']
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('KEIKO_REPORT_COMMIT must be a Git commit SHA')
    url = f'{server}/{repository}/blob/{commit}/reports/final/REPORT.md'
    request = urllib.request.Request(
        f'https://api.airtable.com/v0/{BASE}/{TABLE}',
        data=json.dumps(airtable_payload(summary, completion, url)).encode('utf-8'),
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        method='PATCH',
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            records = json.load(response)['records']
    except urllib.error.HTTPError as error:
        raise SystemExit(f'Airtable HTTP {error.code}. Check token scope and base access; the workflow can retry.') from None
    record_id = records[0]['id'] if len(records) == 1 else ''
    if not re.fullmatch(r'rec[a-zA-Z0-9]{14}', record_id):
        raise ValueError('Airtable did not return exactly one record ID')
    write_json(receipt, {'record_url': f'https://airtable.com/{BASE}/{TABLE}/{record_id}',
                         'recorded_utc': iso(), 'report_url': url})
    print(f'Airtable record {record_id} saved.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['status', 'finalize', 'airtable'])
    command = parser.parse_args().command
    if command == 'status':
        print(status())
    elif command == 'finalize':
        finalize()
    else:
        publish_airtable()
