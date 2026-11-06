"""Package postflight: verify installed components and preserve private settings."""
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
from types import SimpleNamespace

BASE = pathlib.Path('/Library/Application Support/ZMacRDS')
FILES = {
    '/Applications/ZMacRDS Desktop.app/Contents/MacOS/M1Agent',
    '/Applications/ZMacRDS Manager.app/Contents/MacOS/ZMacRDSManager',
    '/usr/local/libexec/zmacrds-control', '/usr/local/libexec/zmacrds-server',
    '/usr/local/libexec/zmacrds-start-desktop', '/usr/local/libexec/zmacrds-configure.py',
    '/usr/local/libexec/zmacrds-install-service.py', '/usr/local/libexec/zmacrds-uninstall.py',
}


def main():
    if os.geteuid() != 0 or BASE.is_symlink() or BASE.stat().st_uid != 0:
        raise ValueError('root installation required')
    os.umask(0o077); BASE.chmod(0o700)
    manifest = json.loads((BASE/'installation.json').read_text())
    if set(manifest['files']) != FILES:
        raise ValueError('unexpected package manifest')
    for name, digest in manifest['files'].items():
        path = pathlib.Path(name)
        if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022 or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('package integrity check failed')
    for path in ['/Applications/ZMacRDS Desktop.app', '/Applications/ZMacRDS Manager.app', '/usr/local/libexec/zmacrds-control']:
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', path], check=True)
    spec = importlib.util.spec_from_file_location('installed_service', '/usr/local/libexec/zmacrds-install-service.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    config = BASE/'server.json'
    if os.path.lexists(config):
        module.private_json(config)  # Corruption is not a first-install reset.
        module.install(SimpleNamespace(config=config, app=pathlib.Path('/Applications/ZMacRDS Desktop.app'),
                       server=pathlib.Path('/usr/local/libexec/zmacrds-server'), launcher=pathlib.Path('/usr/local/libexec/zmacrds-start-desktop'),
                       controller=None, manager=None))
    command = pathlib.Path('/var/run/zmacrds-control')
    if command.is_symlink(): raise ValueError('invalid control directory')
    command.mkdir(exist_ok=True)
    if command.stat().st_uid != 0: raise ValueError('invalid control owner')
    command.chmod(0o711)
    subprocess.run(['/usr/local/libexec/zmacrds-control', '--install-right'], check=True)
    plist = pathlib.Path('/Library/LaunchDaemons/org.zmacrds.control.plist')
    import plistlib
    module.replace(plist, plistlib.dumps(module.control_plist()), 0o644)
    module.unload('system/org.zmacrds.control')
    subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(plist)], check=True)
    (BASE/'.installer-restore').unlink(missing_ok=True)
    print('ZMacRDS installation verified. Open ZMacRDS Manager to configure or manage accounts.')


if __name__ == '__main__': main()
