#!/usr/bin/env python3
"""Optional, restricted SSH identity for a persistent desktop-only tunnel."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

ACCOUNT = '_zmacrds_tunnel'
BEGIN = '# BEGIN ZMacRDS restricted tunnel'
END = '# END ZMacRDS restricted tunnel'

def run(*args):
    return subprocess.check_output(args, text=True).strip()

def atomic(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix='.tunnel-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno()); os.fchmod(f.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--public-key', type=Path, required=True)
    p.add_argument('--expected-host-key', required=True)
    p.add_argument('--expected-mac', required=True)
    args = p.parse_args()
    if os.geteuid() != 0:
        p.error('run in the intended VM using sudo')
    host_key = Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()[1]
    macs = re.findall(r'ether\s+([0-9a-f:]+)', run('ifconfig', 'en0'))
    if host_key != args.expected_host_key or args.expected_mac.lower() not in macs:
        raise ValueError('VM identity mismatch; nothing changed')
    key = args.public_key.read_text().strip().split()
    if len(key) < 2 or key[0] != 'ssh-ed25519' or not re.fullmatch(r'[A-Za-z0-9+/=]+', key[1]):
        raise ValueError('expected one Ed25519 public key')
    base = Path('/Library/Application Support/ZMacRDS')
    receipt = base / 'tunnel.json'
    exists = subprocess.run(['dscl', '.', '-read', '/Users/' + ACCOUNT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if exists:
        if receipt.is_symlink() or not receipt.exists():
            raise ValueError('existing identity is not owned by this installer')
        saved = json.loads(receipt.read_text())
        if saved.get('account') != ACCOUNT or saved.get('mac') != args.expected_mac or saved.get('public_key') != key[1]:
            raise ValueError('existing tunnel identity does not match this installation')
    config = Path('/etc/ssh/sshd_config')
    if config.is_symlink():
        raise ValueError('refusing symlink SSH configuration')
    old = config.read_text()
    content = re.sub(re.escape(BEGIN) + r'.*?' + re.escape(END) + r'\n?', '', old, flags=re.S).rstrip() + '\n'
    keys = Path('/etc/ssh/zmacrds-tunnel-keys')
    restrictions = 'restrict,port-forwarding,command="/usr/bin/false",permitopen="127.0.0.1:3390",permitopen="127.0.0.1:5900" '
    key_line = restrictions + ' '.join(key[:2]) + ' zmacrds-vm03-tunnel\n'
    if keys.is_symlink():
        raise ValueError('refusing symlink key file')
    old_keys = keys.read_bytes() if keys.exists() else None
    block = f'''{BEGIN}
Match User {ACCOUNT}
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AuthorizedKeysFile /etc/ssh/zmacrds-tunnel-keys
    ForceCommand /usr/bin/false
    PermitTTY no
    PermitUserRC no
    X11Forwarding no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:3390 127.0.0.1:5900
    PermitListen none
{END}
'''
    fd, candidate = tempfile.mkstemp(prefix='.zmacrds-', dir='/etc/ssh')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content + block); os.fchmod(f.fileno(), 0o600)
        subprocess.run(['/usr/sbin/sshd', '-t', '-f', candidate], check=True)
        # Validate before creating the identity or replacing any active files.
        backup = base / ('sshd-backup-' + str(time.time_ns()))
        atomic(backup, old.encode(), 0o600)
        created = False
        group_added = False
        try:
            if not exists:
                rows = run('dscl', '.', '-list', '/Users', 'UniqueID').splitlines()
                used = {int(row.split()[-1]) for row in rows}
                uid = next(uid for uid in range(507, 1000) if uid not in used)
                record = '/Users/' + ACCOUNT
                created = True
                for field, value in [('UniqueID', str(uid)), ('PrimaryGroupID', '20'), ('NFSHomeDirectory', '/var/empty'),
                                     ('UserShell', '/bin/sh'), ('RealName', 'ZMacRDS restricted tunnel'), ('IsHidden', '1'), ('UserPassword', '*')]:
                    subprocess.run(['dscl', '.', '-create', record, field, value], check=True)
            atomic(keys, key_line.encode(), 0o644)
            os.replace(candidate, config); config.chmod(0o644)
            if subprocess.run(['dseditgroup', '-o', 'read', 'com.apple.access_ssh'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                subprocess.run(['dseditgroup', '-o', 'edit', '-a', ACCOUNT, '-t', 'user', 'com.apple.access_ssh'], check=True)
                group_added = not exists
            atomic(receipt, json.dumps(dict(account=ACCOUNT, mac=args.expected_mac, public_key=key[1],
                                           ports=[3390, 5900], shell_access=False, reverse_forwarding=False)).encode(), 0o600)
        except Exception:
            atomic(config, old.encode(), 0o644)
            if old_keys is None:
                keys.unlink(missing_ok=True)
            else:
                atomic(keys, old_keys, 0o644)
            if created:
                if group_added:
                    subprocess.run(['dseditgroup', '-o', 'edit', '-d', ACCOUNT, '-t', 'user', 'com.apple.access_ssh'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['dscl', '.', '-delete', '/Users/' + ACCOUNT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raise
    finally:
        if os.path.exists(candidate):
            os.unlink(candidate)
    print('Installed restricted tunnel identity; existing SSH sessions and users unchanged.')

if __name__ == '__main__':
    main()
