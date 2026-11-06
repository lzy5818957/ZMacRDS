#!/usr/bin/env python3
"""Verify synthetic M3 RDP evidence. Never reads credentials or pixel files."""
import argparse
import json
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def rows(path):
    require(path.is_file() and path.stat().st_size <= 100_000_000, f"invalid log: {path.name}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def check_route(route, client, agent, editor, minimum_seconds, foreign_prefixes=()):
    uid, nonce, lease = route['uid'], route['nonce'], route['lease']
    done = [r for r in client if r.get('event') == 'check_complete']
    require(len(done) == 1 and done[0].get('result') == 'PASS', f'{uid}: client incomplete')
    require(done[0].get('uid') == uid and done[0].get('elapsed_ms', 0) >= minimum_seconds * 1000,
            f'{uid}: insufficient duration or wrong identity')
    markers = [r for r in client if r.get('event') == 'marker']
    require(len(markers) >= 3, f'{uid}: no sustained pixel evidence')
    require(0 <= markers[0]['elapsed_ms'] < 6000, f'{uid}: initial pixels missing')
    require(all(r['uid'] == uid and r['nonce'] == nonce for r in markers), f'{uid}: foreign pixels')
    gaps = [b['elapsed_ms'] - a['elapsed_ms'] for a, b in zip(markers, markers[1:])]
    require(all(0 < gap < 6000 for gap in gaps), f'{uid}: stale or unordered pixels')
    require(all(a['tick'] < b['tick'] for a, b in zip(markers, markers[1:])), f'{uid}: repeated/old tick')
    require(done[0]['unique_ticks'] == len(markers), f'{uid}: pixel count mismatch')
    require(done[0]['elapsed_ms'] - markers[-1]['elapsed_ms'] < 6000, f'{uid}: stale final pixels')
    sent = [r for r in client if r.get('event') == 'action_sent']
    require([r['index'] for r in sent] == list(range(1, route['actions'] + 1)), f'{uid}: missing actions')
    require(all(r['uid'] == uid for r in sent) and done[0]['actions'] == len(sent), f'{uid}: wrong action identity')
    starts = [r for r in agent if r.get('kind') == 'bridge_connected' and r.get('lease') == lease]
    ends = [r for r in agent if r.get('kind') == 'bridge_disconnected' and r.get('lease') == lease]
    require(len(starts) == 1 and len(ends) == 1, f'{uid}: incomplete lease lifecycle')
    start, end = starts[0]['uptime_ms'], ends[0]['uptime_ms']
    require(end - start >= minimum_seconds * 1000, f'{uid}: insufficient actual lease duration')
    a = [r for r in agent if start <= r['uptime_ms'] <= end]
    e = [r for r in editor if start <= r['uptime_ms'] <= end]
    require(e and all(r.get('uid') == uid and r.get('gui_uid') == uid for r in a + e),
            f'{uid}: wrong OS session evidence')
    require(len({r['audit_session_id'] for r in a + e}) == 1, f'{uid}: audit session changed')
    require(len({r['boot_id'] for r in a + e}) == 1, f'{uid}: server rebooted')
    require(all(r.get('nonce') == nonce for r in a), f'{uid}: agent changed')
    received = [r for r in a if r['kind'] == 'input_received']
    events = [r for r in e if r['kind'] == 'event']
    require(all(int(r['tag'], 16) >> 32 in (0, nonce) for r in received + events), f'{uid}: foreign input tag')
    documents = [r for r in e if r['kind'] == 'document']
    require(not any(prefix + '-' in r['text'] for r in documents for prefix in foreign_prefixes),
            f'{uid}: another user\'s workload appeared in this document')
    expected = {f"{route['prefix']}-{i:03d}-中文" for i in range(route['cycles'])}
    require(expected <= {r['text'] for r in documents}, f'{uid}: missing received text')
    require(documents[-1]['text'] == f"{route['prefix']}-{route['cycles'] - 1:03d}-中文",
            f'{uid}: final document differs')
    # Each drag must have its own down -> displaced drag -> up sequence in the
    # receiving application. Input packets sent by the driver are not enough.
    down = moved = False
    drags = 0
    for r in received:
        if int(r['tag'], 16) >> 32 != nonce:
            continue  # Locally generated events do not prove remote delivery.
        point = (r.get('x'), r.get('y'))
        if r['event_type'] == 1:
            down, moved = point == tuple(route['drag_from']), False
        elif r['event_type'] == 6 and down and point == tuple(route['drag_to']):
            moved = True
        elif r['event_type'] == 2:
            if down and moved and point == tuple(route['drag_to']):
                drags += 1
            down = moved = False
    require(drags == route['cycles'], f'{uid}: received {drags} complete drags, expected {route["cycles"]}')
    clicks = sum(int(r['tag'], 16) >> 32 == nonce and r['event_type'] == 1 and
                 (r.get('x'), r.get('y')) == tuple(route['editor_click']) for r in events)
    require(clicks == route['cycles'], f'{uid}: editor click count differs')
    return dict(uid=uid, duration_ms=done[0]['elapsed_ms'], markers=len(markers),
                max_marker_gap_ms=max(gaps), actions=len(sent), text_cycles=len(expected),
                received_drags=drags, received_editor_clicks=clicks, foreign_input_tags=0,
                audit_session_id=starts[0]['audit_session_id'], lease=lease,
                boot_id=starts[0]['boot_id'], start_ms=start, end_ms=end)


def overlap(results, minimum_seconds):
    require(len({r['boot_id'] for r in results}) == 1, 'sessions are on different server boots')
    simultaneous = min(r['end_ms'] for r in results) - max(r['start_ms'] for r in results)
    require(simultaneous >= minimum_seconds * 1000, 'insufficient simultaneous connection time')
    return simultaneous


def verify(manifest_path):
    manifest = json.loads(manifest_path.read_text())
    root = manifest_path.parent.resolve()
    def log(name):
        path = (root / name).resolve()
        require(root in path.parents, 'log must be inside evidence directory')
        return rows(path)
    routes = manifest['routes']
    require(len(routes) == 2 and len({r['uid'] for r in routes}) == 2, 'two distinct UIDs required')
    require(len({r['prefix'] for r in routes}) == 2, 'two distinct document prefixes required')
    results = [check_route(r, log(r['client']), log(r['agent']), log(r['editor']),
                           manifest.get('minimum_seconds', 1800),
                           [s['prefix'] for s in routes if s['uid'] != r['uid']]) for r in routes]
    simultaneous = overlap(results, manifest.get('minimum_seconds', 1800))
    negatives = manifest['negative_logs']
    require(len(negatives) >= 3 and len(set(negatives)) == len(negatives), 'negative evidence missing or duplicated')
    for name in negatives:
        records = log(name)
        require(any(r.get('event') == 'tls_verified' for r in records), 'TLS was not verified')
        done = [r for r in records if r.get('event') == 'check_complete']
        require(len(done) == 1 and done[0].get('result') == 'PASS' and
                done[0].get('access_denied') is True and done[0].get('graphics') == 0,
                'expected explicit denial without graphics')
    return dict(result='PASS', simultaneous_ms=simultaneous, routes=results, negative_checks=len(negatives))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.manifest), indent=2))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f'FAIL: {error}\n')
