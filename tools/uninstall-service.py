"""Remove only installed product components; preserve accounts and private configuration."""
import json
import os
import pathlib
import pwd
import shutil
import subprocess

BASE = pathlib.Path('/Library/Application Support/ZMacRDS')


def unload(target):
    subprocess.run(['/bin/launchctl', 'bootout', target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    if os.geteuid() != 0: raise ValueError('administrator required')
    prepare = subprocess.run(['/usr/local/libexec/zmacrds-control', '--client', 'prepare-upgrade'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    if prepare.returncode and subprocess.run(['/usr/bin/pgrep', '-f', '[z]macrds-configure.py configure'],
                                             stdout=subprocess.DEVNULL).returncode == 0:
        raise ValueError('configuration is still running')
    unload('system/org.zmacrds.server')
    # Enumerate ordinary accounts, not paths supplied by a request or config.
    for account in pwd.getpwall():
        if account.pw_uid < 501 or not account.pw_dir.startswith('/Users/'): continue
        unload(f'gui/{account.pw_uid}/org.zmacrds.desktop')
        pid = os.fork()
        if pid == 0:
            try:
                os.setgroups([account.pw_gid]);os.setgid(account.pw_gid);os.setuid(account.pw_uid)
                (pathlib.Path(account.pw_dir)/'Library/LaunchAgents/org.zmacrds.desktop.plist').unlink(missing_ok=True)
                os._exit(0)
            except Exception: os._exit(1)
        os.waitpid(pid, 0)
    unload('system/org.zmacrds.control')
    subprocess.run(['/usr/bin/killall', 'ZMacRDSManager'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for name in ['server', 'control']:
        pathlib.Path(f'/Library/LaunchDaemons/org.zmacrds.{name}.plist').unlink(missing_ok=True)
    for name in ['ZMacRDS Desktop.app', 'ZMacRDS Manager.app']:
        path = pathlib.Path('/Applications')/name
        if path.is_symlink(): path.unlink()
        elif path.exists(): shutil.rmtree(path)
    for name in ['control', 'server', 'start-desktop', 'configure.py', 'install-service.py', 'uninstall.py']:
        pathlib.Path('/usr/local/libexec/zmacrds-'+name).unlink(missing_ok=True)
    subprocess.run(['/usr/sbin/pkgutil', '--forget', 'org.zmacrds.suite'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Credentials, TLS identity, logs, desktop documents, TCC and the optional
    # separately installed SSH tunnel are deliberately retained.
    print('Software removed. Local accounts, documents, private settings and existing tunnel were preserved.')


if __name__ == '__main__': main()
