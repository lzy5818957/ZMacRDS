#!/usr/bin/env python3
"""Start, stop, restart or inspect the installed ZMacRDS services."""
import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess

def command(*args):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['status', 'start', 'stop', 'restart'])
    p.add_argument('--desktop', help='Operate only this local desktop account')
    p.add_argument('--broker', action='store_true', help='Operate only the network service (requires sudo to change)')
    args = p.parse_args()
    if args.desktop and args.broker:
        p.error('choose a desktop or broker')
    root = os.geteuid() == 0
    if args.desktop:
        account = pwd.getpwnam(args.desktop)
        if not root and account.pw_uid != os.geteuid():
            p.error('sudo is required for another account')
        accounts = [account]
    elif not root:
        accounts = [] if args.broker else [pwd.getpwuid(os.geteuid())]
    else:
        cfg = json.loads(Path('/Library/Application Support/ZMacRDS/server.json').read_text())
        accounts = [] if args.broker else [pwd.getpwuid(u['uid']) for u in cfg['users']]
    jobs = []
    if not args.desktop and (root or args.broker or args.action == 'status'):
        jobs.append(('system', 'org.zmacrds.server', Path('/Library/LaunchDaemons/org.zmacrds.server.plist')))
    for account in accounts:
        jobs.append((f'gui/{account.pw_uid}', 'org.zmacrds.desktop', Path(account.pw_dir) / 'Library/LaunchAgents/org.zmacrds.desktop.plist'))
    for domain, label, path in jobs:
        target = domain + '/' + label
        if domain == 'system' and not root and args.action != 'status':
            p.error('sudo is required to change the network service')
        if args.action == 'stop':
            result = command('bootout', target)
        elif args.action == 'start':
            result = command('print', target)
            if result.returncode:
                result = command('bootstrap', domain, str(path))
            else:
                result = command('kickstart', target)
        elif args.action == 'restart':
            result = command('kickstart', '-k', target)
        else:
            result = command('print', target)
        print(target, 'OK' if result.returncode == 0 else 'NOT LOADED / FAILED')
        if args.action == 'status' and result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.strip().startswith(('state =', 'pid =', 'last exit code =', 'runs =')):
                    print(' ', line.strip())
        if args.action != 'status' and result.returncode:
            print(result.stderr.strip())
            raise SystemExit(result.returncode)

if __name__ == '__main__':
    main()
