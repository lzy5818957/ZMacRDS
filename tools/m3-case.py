#!/usr/bin/env python3
"""Prepare private, bounded VM03 two-user integration cases; does not connect."""
import argparse
import json
import os
from pathlib import Path
import re
import stat


def read_base(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        meta = os.fstat(f.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
            raise ValueError('input must be a private regular file owned by the caller')
        data = f.read(65537)
    if len(data) > 65536:
        raise ValueError('input exceeds 64 KiB')
    cfg = json.loads(data)
    cfg = {k: cfg[k] for k in ('certificate', 'uid', 'nonce', 'username', 'password')}
    if not (isinstance(cfg['uid'], int) and cfg['uid'] >= 501 and
            isinstance(cfg['nonce'], int) and 0 < cfg['nonce'] <= 0xffffffff and
            isinstance(cfg['username'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', cfg['username']) and
            isinstance(cfg['password'], str) and 16 <= len(cfg['password'].encode()) <= 1024 and
            Path(cfg['certificate']).is_absolute()):
        raise ValueError('invalid base configuration')
    return cfg


def workload(prefix):
    actions = []
    for i in range(100):
        t = 3000 + 18000 * i
        actions.extend([
            dict(at_ms=t, type='click', x=300, y=850),
            dict(at_ms=t + 400, type='drag', x=300, y=850, to_x=430, to_y=870),
            dict(at_ms=t + 800, type='click', x=1100, y=650),
            dict(at_ms=t + 1100, type='key', code=0x5b, extended=True, down=True),
            dict(at_ms=t + 1200, type='key', code=0x1e, down=True),
            dict(at_ms=t + 1300, type='key', code=0x1e, down=False),
            dict(at_ms=t + 1400, type='key', code=0x5b, extended=True, down=False),
            dict(at_ms=t + 1600, type='text', text=f'{prefix}-{i:03d}-中文'),
        ])
    return actions


def write_new(path, data):
    encoded = json.dumps(data, separators=(',', ':'), ensure_ascii=False).encode()
    if len(encoded) > 65536:
        raise ValueError('generated case exceeds client limit')
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as f:
        f.write(encoded)


def prepare(first, second, dest, label):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,12}', label):
        raise ValueError('label must be 1..12 ASCII letters, digits, underscore or hyphen')
    users = [read_base(first), read_base(second)]
    if any(users[0][k] == users[1][k] for k in ('uid', 'nonce', 'username', 'password')):
        raise ValueError('two different UIDs, nonces, usernames and test passwords are required')
    if users[0]['certificate'] != users[1]['certificate']:
        raise ValueError('both users must pin the same test certificate')
    parent = dest.parent.stat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ValueError('choose an output parent directory owned by the caller with mode 0700')
    dest.mkdir(mode=0o700, exist_ok=False)
    routes = []
    for side, user in zip('ab', users):
        prefix = side.upper() + label
        actions = workload(prefix)
        write_new(dest / f'{side}.json', dict(user, seconds=1830, actions=actions))
        routes.append(dict(uid=user['uid'], nonce=user['nonce'], lease=None, prefix=prefix,
                           cycles=100, actions=len(actions), drag_from=[150, 130], drag_to=[280, 110],
                           editor_click=[200, 300], client=f'{side}.jsonl', agent=f'{side}-agent.jsonl',
                           editor=f'{side}-editor.jsonl'))
    unknown = next(x for x in ('unknown_m3_a','unknown_m3_b','unknown_m3_c')
                   if x not in {u['username'] for u in users})
    negatives = [('unknown-a', dict(users[0], username=unknown), users[0]), ('wrong-a', users[0], users[1]),
                 ('cross-b', users[1], users[0])]
    for name, identity, password_source in negatives:
        write_new(dest / (name + '.json'), dict(identity, password=password_source['password'],
                                                seconds=8, expect_denied=True))
    negative_logs = [n + '.jsonl' for n, _, _ in negatives] + ['free-wrong-a.jsonl', 'free-cross-b.jsonl']
    write_new(dest / 'manifest.json', dict(minimum_seconds=1800, routes=routes, negative_logs=negative_logs))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('client_a', type=Path)
    parser.add_argument('client_b', type=Path)
    parser.add_argument('new_directory', type=Path)
    parser.add_argument('--label', default='M3')
    args = parser.parse_args()
    try:
        prepare(args.client_a, args.client_b, args.new_directory, args.label)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f'FAIL: {error}\n')
