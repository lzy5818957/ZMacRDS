#!/usr/bin/env python3
"""Optional per-user tunnel: pinned server key, no password, loopback listeners."""
import argparse
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--key', type=Path, required=True)
    p.add_argument('--host-public-key', type=Path, required=True)
    p.add_argument('--mac', required=True)
    p.add_argument('--fallback-ip', default='192.168.64.6')
    p.add_argument('--prepare-only', type=Path, help='Write reviewable files here without loading launchd')
    args = p.parse_args()
    if os.geteuid() == 0:
        p.error('run as the host desktop user')
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', args.mac) or not re.fullmatch(r'[0-9.]+', args.fallback_ip):
        p.error('invalid MAC or IP')
    destination = Path.home() / 'Library/Application Support/ZMacRDS/Tunnel'
    output = args.prepare_only or destination
    output.mkdir(parents=True, exist_ok=True); output.chmod(0o700)
    host = args.host_public_key.read_text().split()
    if len(host) < 2 or host[0] != 'ssh-ed25519':
        p.error('a verified Ed25519 host public key is required')
    (output / 'known_hosts').write_text('zmacrds-vm03 ' + ' '.join(host[:2]) + '\n')
    (output / 'known_hosts').chmod(0o600)
    if not args.prepare_only:
        shutil.copyfile(args.key, output / 'id_ed25519'); (output / 'id_ed25519').chmod(0o600)
    ssh = ['/usr/bin/ssh', '-F', '/dev/null', '-NT', '-i', str(destination / 'id_ed25519'),
           '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
           '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
           '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=zmacrds-vm03',
           # ssh parses -o values as configuration syntax too; shell quoting
           # alone does not protect the space in "Application Support".
           '-o', 'UserKnownHostsFile="' + str(destination / 'known_hosts').replace('\\', '\\\\').replace('"', '\\"') + '"', '-o', 'GlobalKnownHostsFile=/dev/null',
           '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-o', 'ConnectTimeout=10',
           '-L', '127.0.0.1:3390:127.0.0.1:3390', '-L', '127.0.0.1:5903:127.0.0.1:5900']
    # Resolve only the intended VM's MAC. The pinned host key remains mandatory
    # even if an IP lease is stale or assigned to another VM.
    script = '''#!/bin/sh
set -eu
umask 077
cd ''' + shlex.quote(str(destination)) + '''
if [ -f tunnel.log ] && [ "$(wc -c < tunnel.log)" -gt 1048576 ]; then mv -f tunnel.log tunnel.log.1; fi
exec 2>>tunnel.log
ip=''' + shlex.quote(args.fallback_ip) + '''
if [ -r /var/db/dhcpd_leases ]; then
 resolved=$(/usr/bin/awk -F= -v target=''' + shlex.quote(args.mac.lower()) + ''' '
 function norm(s, a,n,i,v){sub(/^1,/,"",s);n=split(tolower(s),a,":");v="";for(i=1;i<=n;i++)v=v substr("00" a[i],length("00" a[i])-1);return v}
 /^\\{/ {ip="";mac=""}
 /ip_address=/ {ip=$2;gsub(/[;"[:space:]]/,"",ip)}
 /hw_address=/ {mac=$2;gsub(/[;"[:space:]]/,"",mac)}
 /^\\}/ {if(norm(mac)==norm(target))print ip}
 ' /var/db/dhcpd_leases | /usr/bin/tail -1)
 case "$resolved" in ''|*[!0-9.]*) ;; *) ip=$resolved;; esac
fi
exec ''' + shlex.join(ssh) + ''' "_zmacrds_tunnel@$ip"
'''
    (output / 'run').write_text(script); (output / 'run').chmod(0o700)
    label = 'org.zmacrds.tunnel'
    data = dict(Label=label, ProgramArguments=[str(destination / 'run')], KeepAlive=True, RunAtLoad=True,
                ThrottleInterval=10, ProcessType='Interactive', StandardOutPath='/dev/null', StandardErrorPath='/dev/null')
    path = output / (label + '.plist') if args.prepare_only else Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(data)); path.chmod(0o600)
    if not args.prepare_only:
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
    print('Prepared tunnel files.' if args.prepare_only else 'Installed per-user persistent desktop tunnel.')

if __name__ == '__main__':
    main()
