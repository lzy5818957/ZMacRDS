#!/usr/bin/env python3
"""Fixed-path administrator setup backend. Requests arrive on stdin, never argv."""
import contextlib
import ctypes
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
import pathlib
import pwd
import re
import select
import secrets
import ssl
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

BASE = pathlib.Path('/Library/Application Support/ZMacRDS')
INSTALLER = pathlib.Path('/usr/local/libexec/zmacrds-install-service.py')
SERVER = '/usr/local/libexec/zmacrds-server'
FIREWALL = '/usr/libexec/ApplicationFirewall/socketfilterfw'


def local_accounts():
    accounts = []
    for account in pwd.getpwall():
        try:
            uid(account.pw_uid)
        except (KeyError, ValueError):
            continue
        accounts.append(account)
    if not 1 <= len(accounts) <= 128:
        raise ValueError('one to 128 local desktop accounts required')
    return sorted(accounts, key=lambda account: account.pw_uid)


def local_addresses():
    raw = subprocess.check_output(['/sbin/ifconfig', '-a'], timeout=2, text=True)
    addresses = set()
    for address in re.findall(r'\binet (\d+\.\d+\.\d+\.\d+)\b', raw):
        ip = ipaddress.IPv4Address(address)
        if not ip.is_loopback and (ip in ipaddress.ip_network('10.0.0.0/8') or
                ip in ipaddress.ip_network('172.16.0.0/12') or ip in ipaddress.ip_network('192.168.0.0/16') or
                ip in ipaddress.ip_network('100.64.0.0/10') or ip.is_link_local):
            addresses.add(address)
    return sorted(addresses)


def network_request(request):
    if set(request) != {'enabled', 'port'} or type(request['enabled']) is not bool or \
            type(request['port']) is not int or not 1024 <= request['port'] <= 65535 or request['port'] == 3390:
        raise ValueError('invalid LAN connection settings')
    return request['port'] if request['enabled'] else None


def connection_settings(config):
    port = config.get('lan_port') if config else None
    addresses = local_addresses()
    return {'enabled': port is not None, 'port': port or 3389, 'addresses': addresses,
            'endpoints': [f'{address}:{port}' for address in addresses] if port else [],
            'local_auth': bool(config and config.get('local_auth'))}


def preflight_port(port, old):
    if port is None:
        return
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(('0.0.0.0', port))
            listener.listen(1)  # Also detects an existing SO_REUSEADDR listener.
        except OSError:
            if old and old.get('lan_port') == port and own_listener(port,old['broker_uid']):
                return
            raise  # Fail before stopping a working server or changing config.


def own_listener(port, broker_uid):
    try:
        listing=subprocess.check_output(['/usr/sbin/lsof','-nP',f'-iTCP:{port}','-sTCP:LISTEN','-Fpu'],text=True,timeout=3)
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired):
        return False
    libproc=ctypes.CDLL('/usr/lib/libproc.dylib');found=False;pid=None
    for line in listing.splitlines():
        if line.startswith('p'):pid=int(line[1:])
        elif line.startswith('u'):
            path=ctypes.create_string_buffer(4096)
            if not pid or int(line[1:])!=broker_uid or libproc.proc_pidpath(pid,path,len(path))<=0 or path.value.decode()!=SERVER:
                return False
            found=True
    return found


def allow_firewall():
    console = os.stat('/dev/console').st_uid
    prefix = ['/bin/launchctl', 'asuser', str(console)] if console >= 501 else []
    state = subprocess.check_output(prefix + [FIREWALL, '--getglobalstate'], timeout=10, text=True)
    if '(State = 0)' in state:
        return  # Do not wait on app-rule code assessment while the firewall is off.
    if '(State = 2)' in state:
        raise ValueError('系统防火墙正在阻止所有传入连接，请先在系统设置调整此策略。')
    if '(State = 1)' not in state:
        raise ValueError('无法读取系统防火墙状态。')
    for action in ['--add', '--unblockapp']:
        subprocess.run(prefix + [FIREWALL, action, SERVER], check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def broker_account():
    # An unloginable worker identity keeps every ordinary Mac account available
    # as a desktop. No remote login key or reusable password is created.
    name = '_zmacrds'
    try:
        account = pwd.getpwnam(name)
        if account.pw_uid < 501 or account.pw_dir != '/var/empty' or account.pw_shell != '/usr/bin/false':
            raise ValueError('reserved service identity is not the expected worker')
        return account
    except KeyError:
        pass
    reserved = {account.pw_uid for account in pwd.getpwall()}
    number = max(501, max(value for value in reserved if value < 100000) + 1)
    node = '/Users/' + name
    values = {'UniqueID': str(number), 'PrimaryGroupID': '20', 'NFSHomeDirectory': '/var/empty',
              'UserShell': '/usr/bin/false', 'RealName': 'ZMacRDS service', 'IsHidden': '1',
              'Password': '*'}  # Directory Services creates its own immutable GeneratedUID.
    subprocess.run(['/usr/bin/dscl', '.', '-create', node], check=True, timeout=5,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for key, value in values.items():
        subprocess.run(['/usr/bin/dscl', '.', '-create', node, key, value], check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return pwd.getpwnam(name)


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
    return {'ok': True, 'accounts': accounts, 'network': connection_settings(config), 'broker_uid': config['broker_uid'] if config else None,
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
    names = 'DNS:localhost,IP:127.0.0.1' + ''.join(',IP:' + address for address in local_addresses())
    specification.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n'
                             '[dn]\nCN=localhost\n[ext]\nsubjectAltName=' + names + '\n'
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


def configure_local(request):
    port = network_request(request)
    accounts = local_accounts()
    old = existing()
    if old and old.get('local_auth') and {(a.pw_uid, a.pw_name) for a in accounts} == \
            {(a['uid'], a['credentials']['username']) for a in old['users']}:
        return configure_network(request)
    preflight_port(port, old)
    if port:
        allow_firewall()
    broker = broker_account()
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
            if route and route['credentials']['username'] == account.pw_name:
                users.append(route)
            else:
                users.append({'uid': account.pw_uid, 'credentials': {'username': account.pw_name, 'password': ''}})
        cfg = dict(old) if old else {}
        cfg.update(broker_uid=broker.pw_uid, users=users, certificate=str(cert), private_key=str(key),
                   local_auth=True, lan_port=port)
        path = folder / 'server.json'; path.write_text(json.dumps(cfg)); path.chmod(0o600)
        args = SimpleNamespace(config=path, app=pathlib.Path('/Applications/ZMacRDS Desktop.app'),
                               server=pathlib.Path(SERVER), launcher=pathlib.Path('/usr/local/libexec/zmacrds-start-desktop'),
                               controller=None, manager=None)
        with contextlib.redirect_stdout(io.StringIO()):
            module.install(args)
    return {'ok': True, 'message': '服务已启动。使用此 Mac 的本地用户名和登录密码连接。', 'network': connection_settings(cfg)}


def configure_network(request):
    port = network_request(request)
    old = existing()
    if not old:
        return configure_local(request)
    preflight_port(port, old)
    if port:
        allow_firewall()
    module = installer_module()
    cfg = dict(old, lan_port=port)
    module.replace(BASE/'server.json', json.dumps(cfg).encode())
    try:
        # Replacing the job resets launchd's crash throttle. Rapid -k restarts
        # can otherwise block for an entire throttle interval on macOS.
        module.unload('system/org.zmacrds.server')
        bootstrap_network(module)
        wait_listener(port or cfg.get('port', 3390))
    except Exception:
        module.replace(BASE/'server.json', json.dumps(old).encode())
        module.unload('system/org.zmacrds.server')
        with contextlib.suppress(subprocess.CalledProcessError):
            bootstrap_network(module)
        raise
    return {'ok': True, 'message': '连接设置已保存，请重新连接 RDP。', 'network': connection_settings(cfg)}


def bootstrap_network(module):
    # bootout can return shortly before launchd finishes removing the job.
    for attempt in range(5):
        try:
            subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(module.DAEMON)],
                           check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except subprocess.CalledProcessError:
            if attempt == 4:
                raise
            time.sleep(.2)


def wait_listener(port):
    for _ in range(50):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError('listener did not become ready')


def sync_local_users():
    cfg = existing()
    if not cfg or not cfg.get('local_auth'):
        return {'ok': True}
    accounts = local_accounts()
    expected = {(a.pw_uid, a.pw_name) for a in accounts}
    current = {(a['uid'], a['credentials']['username']) for a in cfg['users']}
    if expected == current:
        return {'ok': True}
    return configure_local({'enabled': cfg.get('lan_port') is not None, 'port': cfg.get('lan_port') or 3389})


def open_desktop(request):
    """Ask Apple's authenticated local server to establish this user's Aqua.

    No keyboard/password injection, UI scripting or TCC database changes.
    This requires native Screen Sharing to be enabled and allow this account.
    The socket stays open until the per-user agent reports its real identity.
    """
    deadline=time.monotonic()+80
    if set(request) != {'uid', 'username', 'password'}:
        raise ValueError('invalid desktop request')
    account = uid(request['uid'])
    if request['username'] != account.pw_name or not isinstance(request['password'], str) or \
            not 1 <= len(request['password'].encode()) <= 1024:
        raise ValueError('invalid desktop identity')
    maximum=int(subprocess.check_output(['/usr/sbin/sysctl','-n','kern.maxproc'],text=True,timeout=2))
    used=len(subprocess.check_output(['/bin/ps','-axo','pid='],text=True,timeout=2).splitlines())
    if maximum-used<512:
        return {'ok':False,'error':'desktop_capacity',
                'message':'此 Mac 的进程资源不足，暂时不能启动更多用户桌面。请注销不用的桌面后重试。'}
    # Do not send a Mac password to an arbitrary unprivileged service on 5900.
    listing = subprocess.check_output(['/usr/sbin/lsof', '-nP', '-iTCP:5900', '-sTCP:LISTEN', '-Fpcu'],
                                      text=True, timeout=3)
    libproc = ctypes.CDLL('/usr/lib/libproc.dylib')
    native = False; pid = None; owner = None
    for line in (listing + '\np0').splitlines():
        if line.startswith('p'):
            if pid and owner == 0:
                path = ctypes.create_string_buffer(4096)
                if libproc.proc_pidpath(pid, path, len(path)) > 0 and path.value.decode() == \
                        '/System/Library/CoreServices/RemoteManagement/screensharingd.bundle/Contents/MacOS/screensharingd':
                    native = True
            pid = int(line[1:]); owner = None
        elif line.startswith('u'):
            owner = int(line[1:])
    if not native:
        raise ValueError('native screen sharing is not available')
    # Bounded, root-private progress diagnostics contain no credentials or pixels.
    diagnostic = BASE/f'native-{account.pw_uid}-init.log'
    def stage(label, reset=False):
        fd = os.open(diagnostic, os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW|
                     (os.O_TRUNC if reset else os.O_APPEND), 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(label+'\n')
    stage('connecting', True)
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    def read(connection, size):
        result = bytearray()
        while len(result) < size:
            chunk = connection.recv(size - len(result))
            if not chunk:
                raise ValueError('native desktop initialization ended')
            result.extend(chunk)
        return bytes(result)
    with socket.create_connection(('127.0.0.1', 5900), timeout=5) as connection:
        stage('connected')
        connection.settimeout(20)
        if read(connection, 12) not in (b'RFB 003.008\n', b'RFB 003.889\n'):
            raise ValueError('unsupported native server')
        connection.sendall(b'RFB 003.008\n')
        stage('version accepted')
        types = read(connection, read(connection, 1)[0])
        if 33 not in types:
            raise ValueError('native account authentication unavailable')
        connection.sendall(b'\x21\x00\x00\x00\x0a\x01\x00RSA1\x00\x00\x00\x00')
        stage('requesting native key')
        length = int.from_bytes(read(connection, 4), 'big')
        if not 7 <= length <= 8192:
            raise ValueError('native key packet too large')
        packet = read(connection, length); size = int.from_bytes(packet[2:6], 'big')
        if packet[:2] not in (b'\x01\x00', b'\x00\x01') or size != len(packet)-7:
            raise ValueError('invalid native identity')
        key = load_der_public_key(packet[6:6+size])
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 2048:
            raise ValueError('invalid native identity')
        def pack(value):
            raw=value.encode('utf-8')
            if len(raw)>63:
                raise ValueError('native desktop credentials exceed Apple protocol limit')
            return raw+b'\0'+os.urandom(63-len(raw))
        aes=os.urandom(16); encryptor=Cipher(algorithms.AES(aes),modes.ECB()).encryptor()
        encrypted=encryptor.update(pack(account.pw_name)+pack(request['password']))+encryptor.finalize()
        payload=b'\x01\x00RSA1\0\x01'+encrypted+b'\0\x01'+key.encrypt(aes,padding.PKCS1v15())
        connection.sendall(len(payload).to_bytes(4,'big')+payload)
        stage('credentials encrypted and sent')
        read(connection,4)
        if read(connection,4)!=bytes(4):
            raise ValueError('native desktop login rejected')
        stage('native authentication accepted')
        # A cold Aqua login can take longer than the authentication handshake.
        connection.settimeout(min(50, max(1, deadline-time.monotonic())))
        connection.sendall(b'\1')  # shared ClientInit never disconnects another user's console.
        header=read(connection,24); name_length=struct.unpack('>I',header[20:24])[0]
        if name_length>8192:raise ValueError('native desktop name too large')
        read(connection,name_length)
        stage('native desktop initialized')
        # A virtual display may disappear when the Apple client disconnects.
        # Transfer the authenticated socket to an unprivileged native holder;
        # exec discards all password-containing Python memory in the child.
        ready_read,ready_write=os.pipe()
        def unprivileged():
            os.setgroups([account.pw_gid]);os.setgid(account.pw_gid);os.setuid(account.pw_uid)
        holder=None
        token=secrets.token_hex(16)
        log_fd=os.open(BASE/f'native-{account.pw_uid}-holder.log',os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        holder_log=os.fdopen(log_fd,'wb')
        try:
            width,height=struct.unpack('>HH',header[:4])
            holder=subprocess.Popen([SERVER,'--native-desktop-holder',str(connection.fileno()),str(account.pw_uid),
                                     str(ready_write),str(width),str(height),token],
                                    pass_fds=(connection.fileno(),ready_write),preexec_fn=unprivileged,
                                    start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=holder_log)
            os.close(ready_write);ready_write=None
            if not select.select([ready_read],[],[],min(20,max(0,deadline-time.monotonic())))[0] or os.read(ready_read,1)!=b'1':
                raise ValueError('native desktop did not produce an initial frame')
            # Bind the agent to the now-rendering display, rather than the
            # transient display advertised while Apple was starting Aqua.
            with contextlib.redirect_stdout(io.StringIO()):
                installer_module().install_user(account)
        except Exception:
            if holder and holder.poll() is None:
                holder.terminate();holder.wait(timeout=5)
            raise
        finally:
            holder_log.close()
            os.close(ready_read)
            if ready_write is not None:os.close(ready_write)
        state_path=pathlib.Path(account.pw_dir)/'Library/Application Support/ZMacRDS/Service/state.json'
        boot=subprocess.check_output(['/usr/sbin/sysctl','-n','kern.bootsessionuuid'],text=True,timeout=2).strip()
        while time.monotonic()<deadline:
            try:
                job=subprocess.run(['/bin/launchctl','print',f'gui/{account.pw_uid}/org.zmacrds.desktop'],
                                   capture_output=True,text=True,timeout=2)
                job_pid=re.search(r'^\s*pid = (\d+)\s*$',job.stdout,re.MULTILINE)
                # The root controller subsequently rechecks PID/UID/boot and
                # only the authenticated fixed-UID bridge may deliver pixels.
                fd=os.open(state_path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                with os.fdopen(fd) as f:
                    meta=os.fstat(f.fileno())
                    if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=account.pw_uid or meta.st_mode & 0o077 or meta.st_size>65536:
                        raise ValueError('invalid state file')
                    state=json.load(f)
                process=state.get('pid');path=ctypes.create_string_buffer(4096)
                age=time.clock_gettime(time.CLOCK_MONOTONIC_RAW)*1000-state.get('uptime_ms',0)
                if state.get('uid')==account.pw_uid and state.get('boot_id')==boot and 0<=age<6000 and \
                        type(process) is int and process>1 and job_pid and int(job_pid[1])==state.get('guardian_pid') and \
                        libproc.proc_pidpath(process,path,len(path))>0 and \
                        path.value.decode()=='/Applications/ZMacRDS Desktop.app/Contents/MacOS/M1Agent' and \
                        state.get('login_complete') and state.get('screen_capture') and state.get('accessibility') and \
                        0<state.get('last_capture_ms',0)<=time.clock_gettime(time.CLOCK_MONOTONIC_RAW)*1000 and \
                        time.clock_gettime(time.CLOCK_MONOTONIC_RAW)*1000-state['last_capture_ms']<3000:
                    command=pathlib.Path('/var/run/zmacrds-control')
                    if command.is_symlink() or command.stat().st_uid!=0 or command.stat().st_mode & 0o022:
                        raise ValueError('untrusted native display lease directory')
                    # This is a public generation label, not a credential.
                    # Only root can replace it; a new successful login retires
                    # older holders without trusting PID files or killing PIDs.
                    lease={'token':token,'uid':account.pw_uid,'audit_session_id':state['audit_session_id'],'boot_id':boot}
                    installer_module().replace(command/f'native-{account.pw_uid}.lease',json.dumps(lease).encode(),0o644)
                    return {'ok':True,'uid':account.pw_uid}
            except (OSError,ValueError):
                pass
            time.sleep(.25)
    if holder and holder.poll() is None:
        holder.terminate();holder.wait(timeout=5)
    return {'ok':False,'error':'desktop_setup_required',
            'message':'请先完成该用户的首次系统设置、录屏和辅助功能授权。'}


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
    settings = connection_settings(config)
    return {'ok': True, 'username': account.pw_name, 'password': '' if config.get('local_auth') else route['credentials']['password'],
            'local_auth': bool(config.get('local_auth')), 'endpoints': settings['endpoints'],
            'endpoint': settings['endpoints'][0] if settings['endpoints'] else '127.0.0.1:3390',
            'sha256': ':'.join(fingerprint[i:i+2] for i in range(0, 64, 2))}


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
    if operation == 'configure-local':
        return configure_local(request)
    if operation == 'configure-network':
        return configure_network(request)
    if operation == 'sync-local-users' and request == {}:
        return sync_local_users()
    if operation == 'open-desktop':
        return open_desktop(request)
    if operation == 'connection-info' and set(request) == {'uid'}:
        return connection_info(request['uid'])
    raise ValueError('unsupported operation')


if __name__ == '__main__':
    try:
        result = main()
    except Exception as error:
        # No passwords, private configuration, subprocess output or traceback.
        result = {'ok': False, 'error': type(error).__name__,
                  'message': '该连接端口已被占用，请在连接设置选择其他端口。' if isinstance(error, OSError) and error.errno == 48 else
                             str(error) if isinstance(error,ValueError) and str(error).startswith(('系统防火墙','无法读取系统防火墙')) else
                             '操作未完成，原有用户登录和文档保留。请检查服务状态或重新启动服务。'}
        if isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)) and isinstance(error.cmd, list):
            program = pathlib.Path(error.cmd[0]).name
            if program in {'dscl', 'socketfilterfw', 'codesign', 'launchctl'}:
                result['component'] = program + (':' + error.cmd[4] if program == 'dscl' and len(error.cmd)>4 else '')
                if program == 'launchctl': result['component'] += ':' + error.cmd[1]
    print(json.dumps(result))
    sys.exit(0 if result['ok'] else 1)
