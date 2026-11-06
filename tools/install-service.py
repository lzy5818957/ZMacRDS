#!/usr/bin/env python3
"""Install the signed GUI app and loopback broker; never changes TCC or login policy."""
import argparse
import json
import os
import pathlib
import plistlib
import pwd
import shutil
import signal
import stat
import subprocess
import tempfile
import time

BASE = pathlib.Path('/Library/Application Support/ZMacRDS')
DAEMON = pathlib.Path('/Library/LaunchDaemons/org.zmacrds.server.plist')
LABEL = 'org.zmacrds.desktop'

def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_size > 65536:
            raise ValueError('configuration must be a bounded private regular file')
        return json.load(f)

def replace(path, data, mode=0o600):
    if path.is_symlink():
        raise ValueError('refusing symlink destination')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.install-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
            os.fchmod(out.fileno(), mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def unload(target):
    subprocess.run(['launchctl', 'bootout', target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        result = subprocess.run(['launchctl', 'print', target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            return
        time.sleep(0.1)
    raise RuntimeError('service did not finish unloading: ' + target)

def agent_plist():
    return dict(Label=LABEL, ProgramArguments=['/usr/local/libexec/zmacrds-start-desktop', '--service'],
                RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
                LimitLoadToSessionType='Aqua', ProcessType='Interactive',
                StandardOutPath='/dev/null', StandardErrorPath='/dev/null')

def control_plist():
    return dict(Label='org.zmacrds.control',
                ProgramArguments=['/usr/local/libexec/zmacrds-control', '--daemon'],
                RunAtLoad=True, KeepAlive=True, ThrottleInterval=10, ProcessType='Interactive',
                Sockets=dict(Control=dict(SockPathName='/var/run/org.zmacrds.control.sock',
                                          SockPathMode=0o666, SockType='stream')),
                StandardOutPath='/dev/null', StandardErrorPath='/dev/null')

def install_management(controller_data, manager):
    folder = pathlib.Path('/var/run/zmacrds-control')
    if folder.is_symlink():
        raise ValueError('control directory must not be a symlink')
    folder.mkdir(exist_ok=True)
    if folder.stat().st_uid != 0:
        raise ValueError('control directory must be owned by root')
    folder.chmod(0o711)
    unload('system/org.zmacrds.control')
    executable = pathlib.Path('/usr/local/libexec/zmacrds-control')
    replace(executable, controller_data, 0o755)
    subprocess.run([str(executable), '--install-right'], check=True)
    stage = pathlib.Path(tempfile.mkdtemp(prefix='.manager-', dir='/Applications'))
    shutil.copytree(manager, stage / 'ZMacRDS Manager.app', symlinks=True)
    target = pathlib.Path('/Applications/ZMacRDS Manager.app')
    if target.is_symlink():
        raise ValueError('manager destination must not be a symlink')
    if target.exists():
        target.rename(BASE / ('previous-manager-' + str(time.time_ns()) + '.app'))
    (stage / 'ZMacRDS Manager.app').rename(target)
    stage.rmdir()
    path = pathlib.Path('/Library/LaunchDaemons/org.zmacrds.control.plist')
    replace(path, plistlib.dumps(control_plist()), 0o644)
    subprocess.run(['launchctl', 'bootstrap', 'system', str(path)], check=True)

def install_user(account):
    # Writing the per-user LaunchAgent is deliberately unprivileged: a user-
    # controlled symlink under their home cannot redirect a privileged write.
    pid = os.fork()
    if pid == 0:
        try:
            os.setgroups([account.pw_gid]); os.setgid(account.pw_gid); os.setuid(account.pw_uid)
            path = pathlib.Path(account.pw_dir) / 'Library/LaunchAgents' / (LABEL + '.plist')
            replace(path, plistlib.dumps(agent_plist()))
            os._exit(0)
        except Exception:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    if status:
        raise RuntimeError('could not install user LaunchAgent')
    unload(f'gui/{account.pw_uid}/{LABEL}')
    result = subprocess.run(['launchctl', 'bootstrap', f'gui/{account.pw_uid}',
                             str(pathlib.Path(account.pw_dir) / 'Library/LaunchAgents' / (LABEL + '.plist'))],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f'GUI service UID {account.pw_uid}: ' + ('started' if result.returncode == 0 else 'starts on next graphical login'))

def install(args):
    if os.geteuid() != 0:
        raise ValueError('run installer with sudo')
    cfg = private_json(args.config)
    broker = pwd.getpwuid(cfg['broker_uid'])
    if broker.pw_uid < 501 or not 1 <= len(cfg['users']) <= 8:
        raise ValueError('ordinary broker and 1..8 desktop accounts required')
    accounts = []
    for route in cfg['users']:
        account = pwd.getpwuid(route['uid'])
        if account.pw_uid < 501 or account.pw_uid == broker.pw_uid or account.pw_name != route['credentials']['username']:
            raise ValueError('route must match an ordinary local account')
        if account.pw_uid in [a.pw_uid for a in accounts]:
            raise ValueError('duplicate account')
        route['service'] = True
        route['bridge'] = str(pathlib.Path(account.pw_dir) / 'Library/Application Support/ZMacRDS/Service/bridge.json')
        accounts.append(account)
    # Fail on missing build artifacts or an invalid signature before stopping
    # the currently working service. Keep private TLS bytes out of diagnostics.
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(args.app)], check=True)
    server_data = args.server.read_bytes()
    launcher_data = args.launcher.read_bytes()
    tls_data = {field: pathlib.Path(cfg[field]).read_bytes() for field in ('certificate', 'private_key')}
    controller = getattr(args, 'controller', None)
    manager = getattr(args, 'manager', None)
    if bool(controller) != bool(manager):
        raise ValueError('provide both --controller and --manager')
    controller_data = None
    if controller:
        subprocess.run(['codesign', '--verify', '--strict', str(controller)], check=True)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(manager)], check=True)
        controller_data = controller.read_bytes()
    for directory in (BASE, pathlib.Path('/usr/local/libexec'), pathlib.Path('/Library/Logs/ZMacRDS')):
        if directory.is_symlink():
            raise ValueError('service destination must not be a symlink')
        directory.mkdir(parents=True, exist_ok=True)
    BASE.chmod(0o700)
    # Stop only these configured services and this installed executable.
    unload('system/org.zmacrds.server')
    for account in accounts:
        unload(f'gui/{account.pw_uid}/{LABEL}')
    rows = subprocess.check_output(['ps', '-axo', 'pid=,uid=,comm='], text=True).splitlines()
    managed = '/Applications/ZMacRDS Desktop.app/Contents/MacOS/M1Agent'
    stopped = []
    for row in rows:
        fields = row.split(None, 2)
        if len(fields) == 3 and fields[2] == managed and int(fields[1]) in [a.pw_uid for a in accounts]:
            pid = int(fields[0])
            try:
                os.kill(pid, signal.SIGTERM); stopped.append(pid)
            except ProcessLookupError:
                pass
    for _ in range(50):
        alive = []
        for pid in stopped:
            try:
                os.kill(pid, 0); alive.append(pid)
            except ProcessLookupError:
                pass
        if not alive:
            break
        stopped = alive
        time.sleep(0.1)
    else:
        raise RuntimeError('desktop agent did not exit normally; update stopped')
    logs = pathlib.Path('/Library/Logs/ZMacRDS')
    os.chown(logs, broker.pw_uid, broker.pw_gid); logs.chmod(0o700)
    stage = pathlib.Path(tempfile.mkdtemp(prefix='.app-', dir='/Applications'))
    shutil.copytree(args.app, stage / 'ZMacRDS Desktop.app', symlinks=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(stage / 'ZMacRDS Desktop.app')], check=True)
    target = pathlib.Path('/Applications/ZMacRDS Desktop.app')
    if target.is_symlink():
        raise ValueError('application destination must not be a symlink')
    if target.exists():
        target.rename(BASE / ('previous-desktop-' + str(time.time_ns()) + '.app'))
    (stage / 'ZMacRDS Desktop.app').rename(target)
    stage.rmdir()
    replace(pathlib.Path('/usr/local/libexec/zmacrds-server'), server_data, 0o755)
    replace(pathlib.Path('/usr/local/libexec/zmacrds-start-desktop'), launcher_data, 0o755)
    for field, name in [('certificate', 'certificate.pem'), ('private_key', 'private-key.pem')]:
        replace(BASE / name, tls_data[field])
        cfg[field] = str(BASE / name)
    replace(BASE / 'server.json', json.dumps(cfg).encode())
    if controller_data:
        install_management(controller_data, manager)
    daemon = dict(Label='org.zmacrds.server', ProgramArguments=['/usr/local/libexec/zmacrds-server', '--multi', str(BASE / 'server.json')],
                  RunAtLoad=True, KeepAlive=True, ThrottleInterval=10, ProcessType='Interactive',
                  EnvironmentVariables={'ZMACRDS_LOG_PATH': str(logs / 'server.log')},
                  StandardOutPath='/dev/null', StandardErrorPath='/dev/null')
    unload('system/org.zmacrds.server')
    replace(DAEMON, plistlib.dumps(daemon), 0o644)
    for attempt in range(5):
        result = subprocess.run(['launchctl', 'bootstrap', 'system', str(DAEMON)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if result.returncode == 0:
            break
        if attempt == 4:
            raise RuntimeError('could not bootstrap broker: ' + result.stderr.decode(errors='replace'))
        time.sleep(0.2)
    for account in accounts:
        install_user(account)
    print('Installed loopback RDP service. Graphical login and normal OS unlock remain required.')

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True, type=pathlib.Path)
    p.add_argument('--app', required=True, type=pathlib.Path)
    p.add_argument('--server', required=True, type=pathlib.Path)
    p.add_argument('--launcher', required=True, type=pathlib.Path)
    p.add_argument('--controller', type=pathlib.Path, help='Signed native management service')
    p.add_argument('--manager', type=pathlib.Path, help='Signed management application; use with --controller')
    install(p.parse_args())

if __name__ == '__main__':
    main()
