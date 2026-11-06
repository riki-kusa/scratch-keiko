#!/usr/bin/env python3
"""Recompute conclusions from a small ACTUAL Scratch API sample bundled with this archive.

The source data was collected through a proxy, not by this script; this script
independently recomputes counts and list/detail disagreements from the record.
"""
import json
from pathlib import Path

p=Path(__file__).parent/'pilot_observation_2026-11-05.json'
x=json.loads(p.read_text(encoding='utf-8'))
lists=x['lists']
all_ids=set().union(*(set(ids) for ids in lists.values()))
record_count=sum(len(ids) for ids in lists.values())
multiple=sum(sum(pid in ids for ids in lists.values()) > 1 for pid in all_ids)
print('Records:', record_count, 'Unique project IDs:',len(all_ids),'Seen across >=2 routes:',multiple)
seen=set()
for name,ids in lists.items():
    s=set(ids)
    other=set().union(*(set(v) for k,v in lists.items() if k!=name))
    print(f'{name:24} found={len(ids):3} new_in_order={len(s-seen):3} unique_to_this_probe_route={len(s-other):3} stats_present={x["list_stats_present_count"][name]:3}')
    seen|=s
print('\nSelected list/detail comparisons (not simultaneous):')
for pid, a in x['selected_list_samples'].items():
    b=x['individual_api_results'].get(pid)
    if not b:continue
    print(pid, 'title_equal=',a['title']==b['title'], 'views_delta=',b['views']-a['views'],
          'loves_delta=',b['loves']-a['loves'],'favorites_delta=',b['favorites']-a['favorites'])
assert record_count==124 and len(all_ids)==116 and multiple==8
print('\nChecks passed. Reminder: this does NOT estimate Scratch-wide coverage or freshness.')
