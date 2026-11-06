#!/usr/bin/env python3
"""Fixed-path administrator setup backend. Requests arrive on stdin, never argv."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import pwd
import secrets
import ssl
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace

BASE = pathlib.Path('/Library/Application Support/ZMacRDS')
INSTALLER = pathlib.Path('/usr/local/libexec/zmacrds-install-service.py')


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_size > 65536:
            raise ValueError('private root configuration required')
        return json.load(stream)


def uid(value):
    if type(value) is not int or not 501 <= value <= 2147483647:
        raise ValueError('ordinary local account required')
    account = pwd.getpwuid(value)
    if account.pw_name.startswith('_') or not account.pw_dir.startswith('/Users/'):
        raise ValueError('ordinary local account required')
    return account


def existing():
    path = BASE / 'server.json'
    # A broken/symlink configuration is an error, not an empty first install.
    return private_json(path) if os.path.lexists(path) else None


def setup_info():
    config = existing()
    accounts = []
    for account in pwd.getpwall():
        try:
            uid(account.pw_uid)
        except (KeyError, ValueError):
            continue
        accounts.append({'uid': account.pw_uid, 'username': account.pw_name})
    accounts.sort(key=lambda item: item['uid'])
    if len(accounts) > 128:
        raise ValueError('too many local accounts')
    return {'ok': True, 'accounts': accounts, 'broker_uid': config['broker_uid'] if config else None,
            'desktop_uids': [item['uid'] for item in config['users']] if config else []}


def validate(request, config):
    if set(request) != {'broker_uid', 'desktop_uids'}:
        raise ValueError('unsupported setup fields')
    broker = uid(request['broker_uid'])
    selected = request['desktop_uids']
    if type(selected) is not list or not 1 <= len(selected) <= 8:
        raise ValueError('choose one to eight desktop accounts')
    accounts = [uid(value) for value in selected]
    if len({account.pw_uid for account in accounts}) != len(accounts) or broker.pw_uid in selected:
        raise ValueError('desktop accounts must be unique and separate from broker')
    if config:
        if config['broker_uid'] != broker.pw_uid or not {item['uid'] for item in config['users']}.issubset(set(selected)):
            raise ValueError('existing broker and desktop accounts are preserved')
    return broker, accounts


def certificate(folder):
    cert, key = folder / 'certificate.pem', folder / 'private-key.pem'
    specification = folder / 'openssl.cnf'
    specification.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n'
                             '[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n'
                             'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                             'extendedKeyUsage=serverAuth\n')
    subprocess.run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-nodes',
                    '-days', '365', '-config', str(specification), '-keyout', str(key), '-out', str(cert)],
                   check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=30)
    cert.chmod(0o600); key.chmod(0o600)
    return cert, key


def installer_module():
    spec = importlib.util.spec_from_file_location('zmacrds_installer', INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure(request):
    old = existing()
    broker, accounts = validate(request, old)  # Validate before creating files or stopping jobs.
    module = installer_module()
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if BASE.is_symlink() or BASE.stat().st_uid != 0 or BASE.stat().st_mode & 0o077:
        raise ValueError('private root service directory required')
    with tempfile.TemporaryDirectory(prefix='.setup-', dir=BASE) as temporary:
        folder = pathlib.Path(temporary)
        cert, key = (pathlib.Path(old['certificate']), pathlib.Path(old['private_key'])) if old else certificate(folder)
        previous = {item['uid']: item for item in old['users']} if old else {}
        users = []
        for account in accounts:
            route = previous.get(account.pw_uid)
            if route:
                if route['credentials']['username'] != account.pw_name:
                    raise ValueError('local account identity changed')
                users.append(route)
            else:
                users.append({'uid': account.pw_uid, 'credentials': {
                    'username': account.pw_name, 'password': secrets.token_urlsafe(24)}})
        cfg = dict(old) if old else {}
        cfg.update(broker_uid=broker.pw_uid, users=users, certificate=str(cert), private_key=str(key))
        path = folder / 'server.json'
        path.write_text(json.dumps(cfg)); path.chmod(0o600)
        # The package has already installed the management service. Do not
        # stop the caller's controller while it waits for this operation.
        args = SimpleNamespace(config=path, app=pathlib.Path('/Applications/ZMacRDS Desktop.app'),
                               server=pathlib.Path('/usr/local/libexec/zmacrds-server'),
                               launcher=pathlib.Path('/usr/local/libexec/zmacrds-start-desktop'),
                               controller=None, manager=None)
        with contextlib.redirect_stdout(io.StringIO()):
            module.install(args)
    return {'ok': True, 'message': '配置已保存。请在新增账号中登录一次，并授权录屏和键鼠控制。'}


def connection_info(account_uid):
    account = uid(account_uid)
    config = existing()
    if not config:
        raise ValueError('setup required')
    route = next((item for item in config['users'] if item['uid'] == account.pw_uid), None)
    if not route or route['credentials']['username'] != account.pw_name:
        raise ValueError('unknown desktop account')
    raw = pathlib.Path(config['certificate']).read_text()
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(raw)).hexdigest().upper()
    return {'ok': True, 'username': account.pw_name, 'password': route['credentials']['password'],
            'endpoint': '127.0.0.1:3390', 'sha256': ':'.join(fingerprint[i:i+2] for i in range(0, 64, 2))}


def main():
    os.umask(0o077)
    if os.geteuid() != 0 or len(sys.argv) != 2:
        raise ValueError('administrator service required')
    operation = sys.argv[1]
    data = sys.stdin.buffer.read(8193)
    if len(data) > 8192:
        raise ValueError('oversized request')
    request = json.loads(data)
    if operation == 'setup-info' and request == {}:
        return setup_info()
    if operation == 'configure':
        return configure(request)
    if operation == 'connection-info' and set(request) == {'uid'}:
        return connection_info(request['uid'])
    raise ValueError('unsupported operation')


if __name__ == '__main__':
    try:
        result = main()
    except Exception as error:
        # No passwords, private configuration, subprocess output or traceback.
        result = {'ok': False, 'error': type(error).__name__,
                  'message': '配置操作未完成。请检查账号选择、安装完整性和服务状态。'}
    print(json.dumps(result))
    sys.exit(0 if result['ok'] else 1)
