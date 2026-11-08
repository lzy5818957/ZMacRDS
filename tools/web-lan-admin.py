#!/usr/bin/env python3
"""Manage the optional ZMacRDS HTTPS gateway on this Mac."""

import argparse
import base64
import contextlib
import datetime
import importlib.util
import ipaddress
import json
import os
import pathlib
import plistlib
import pwd
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time

BASE = pathlib.Path('/Library/Application Support/ZMacRDS')
WEB = pathlib.Path('/var/db/zmacrds-web')
PLIST = pathlib.Path('/Library/LaunchDaemons/org.zmacrds.web.plist')
LABEL = 'system/org.zmacrds.web'
SERVER_LABEL = 'system/org.zmacrds.server'
GATEWAY = '/usr/local/libexec/zmacrds-web'
WORKER = '_zmacrdsweb'
LAUNCHCTL = '/bin/launchctl'
DEFAULT_SESSIONS = 5
PRIVATE_NETWORKS = tuple(map(ipaddress.IPv4Network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))


def read_regular(path, limit=256 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('expected a bounded regular file')
        data = stream.read(limit + 1)
        if not data or len(data) > limit:
            raise ValueError('unexpected file size')
        return data


def setup_module():
    spec = importlib.util.spec_from_file_location('zmacrds_setup', '/usr/local/libexec/zmacrds-configure.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worker():
    try:
        user = pwd.getpwnam(WORKER)
    except KeyError:
        numbers = [item.pw_uid for item in pwd.getpwall() if 501 <= item.pw_uid < 100000]
        number = max(numbers, default=500) + 1
        if number >= 100000:
            raise ValueError('no worker ID available')
        node = '/Users/' + WORKER
        for key, value in {'UniqueID': str(number), 'PrimaryGroupID': '20',
                           'NFSHomeDirectory': '/var/empty', 'UserShell': '/usr/bin/false',
                           'RealName': 'ZMacRDS Web service', 'IsHidden': '1', 'Password': '*'}.items():
            subprocess.run(['/usr/bin/dscl', '.', '-create', node, key, value], check=True, timeout=5)
        user = pwd.getpwnam(WORKER)
    if user.pw_uid < 501 or user.pw_dir != '/var/empty' or user.pw_shell != '/usr/bin/false':
        raise ValueError('reserved web worker has an unexpected identity')
    return user


def lan_settings(args, interfaces):
    address = ipaddress.IPv4Address(args.address)
    subnet = ipaddress.IPv4Network(args.subnet, strict=True)
    if (str(address) not in interfaces or address not in subnet or subnet.prefixlen < 16
            or not any(subnet.subnet_of(network) for network in PRIVATE_NETWORKS)
            or not 1024 <= args.port <= 65535 or not 1024 <= args.media_port <= 65535):
        raise ValueError('provide a local private IPv4 address, its private subnet, and ports from 1024 to 65535')
    return str(address), str(subnet), args.hostname or str(address)


def bootstrap():
    subprocess.run([LAUNCHCTL, 'bootstrap', 'system', str(PLIST)], check=True, timeout=10)


def status():
    proc = subprocess.run([LAUNCHCTL, 'print', LABEL], capture_output=True, text=True, timeout=5)
    print('运行中' if not proc.returncode and re.search(r'^\s*pid = [0-9]+', proc.stdout, re.M) else '未运行')
    if (WEB/'settings.json').exists():
        current = json.loads(read_regular(WEB/'settings.json'))
        print(current['url'])


def wait_https(address, hostname, port, leaf):
    # Verify this exact leaf before reporting a successful start.
    expected = ssl.PEM_cert_to_DER_cert(leaf.decode().split('-----END CERTIFICATE-----')[0] + '-----END CERTIFICATE-----')
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((address, port), timeout=.25) as sock:
                with client.wrap_socket(sock, server_hostname=hostname) as tls:
                    if tls.getpeercert(binary_form=True) == expected:
                        return
        except OSError:
            pass
        time.sleep(.2)
    raise RuntimeError('HTTPS 未能启动。请检查端口是否被占用、局域网地址和防火墙。')


def job_loaded(label):
    return subprocess.run([LAUNCHCTL, 'print', label], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=5).returncode == 0


def job_running(label):
    result = subprocess.run([LAUNCHCTL, 'print', label], capture_output=True, text=True, timeout=5)
    return result.returncode == 0 and re.search(r'^\s*state = running\s*$', result.stdout, re.M) is not None


def expiry(path):
    return subprocess.check_output(['/usr/bin/openssl', 'x509', '-in', str(path),
                                    '-noout', '-enddate'], timeout=5, text=True).strip().partition('=')[2]


def trusted_dir(path, owner, mode, *, create=False):
    if create:
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            pass
        else:
            if owner:
                os.chown(path, owner, -1)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise ValueError('网页服务的本地目录权限无效')
    path.chmod(mode)


def allow_managed_runtime(account):
    # Permit only traversing the private parent to reach its packaged Runtime.
    # The worker cannot list the directory or read root-owned service secrets.
    info = BASE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('服务目录的所有权或权限无效')
    acl = subprocess.check_output(['/bin/ls', '-lde', str(BASE)], timeout=3, text=True)
    if not re.search(r'^\s*\d+: user:' + re.escape(account.pw_name) + r' allow search\s*$', acl, re.M):
        subprocess.run(['/bin/chmod', '+a', f'{account.pw_name} allow search', str(BASE)], check=True, timeout=5)


def saved():
    path = WEB/'settings.json'
    if not path.exists():
        return None
    record = json.loads(read_regular(path))
    # 0.3.6 saved a URL and launchd arguments separately.
    if 'tls_dir' not in record:
        if not PLIST.exists():
            return None
        args = plistlib.loads(read_regular(PLIST))['ProgramArguments']
        def argument(name):
            return args[args.index(name)+1]
        address, port = argument('-listen').split(':')
        hostname = argument('-authority').rsplit(':', 1)[0]
        record.update(address=address, port=int(port), hostname=hostname,
                      tls_dir=pathlib.Path(argument('-tls-cert')).parent.name)
    if not re.fullmatch(r'tls-[0-9]+', record['tls_dir']):
        raise ValueError('网页服务的证书目录无效')
    return record


def migrate_running_definition(installer):
    # Package upgrade: update the 0.3.6 job in place without rotating TLS.
    record = saved()
    if not record:
        return
    account = worker()
    allow_managed_runtime(account)
    trusted_dir(WEB, 0, 0o711)
    runtime = WEB/'runtime'
    trusted_dir(runtime, account.pw_uid, 0o700, create=True)
    definition = plistlib.loads(read_regular(PLIST))
    command = definition['ProgramArguments']
    if command[0] != GATEWAY or definition.get('UserName') != account.pw_name:
        raise ValueError('unexpected web gateway launch definition')
    if '-management-socket' not in command:
        command.extend(['-management-socket', str(runtime/'status.sock')])
    definition.setdefault('SoftResourceLimits', {}).setdefault('NumberOfFiles', 4096)
    if not record.get('expires'):
        record['expires'] = expiry(WEB/record['tls_dir']/'https.pem')
    installer.replace(PLIST, plistlib.dumps(definition), 0o644)
    installer.replace(WEB/'settings.json', json.dumps(record).encode())


def materials(request):
    if set(request)-{'max_sessions'} != {'address', 'subnet', 'hostname', 'port', 'certificate', 'private_key'}:
        raise ValueError('网页配置字段不完整')
    if any(not isinstance(request[key], str) for key in ('address', 'subnet', 'hostname', 'certificate', 'private_key')):
        raise ValueError('网页配置格式无效')
    if type(request['port']) is not int or not 1024 <= request['port'] <= 65535 or request['port'] == 3390:
        raise ValueError('请输入 1024–65535 的 HTTPS 端口，3390 是本机保留端口。')
    certificate, key = (base64.b64decode(request[name], validate=True) for name in ('certificate', 'private_key'))
    if not 0 < len(certificate) <= 24576 or not 0 < len(key) <= 8192:
        raise ValueError('证书链或私钥文件的大小无效')
    return argparse.Namespace(address=request['address'], subnet=request['subnet'], hostname=request['hostname'].lower(),
                              port=request['port'], media_port=request['port'],
                              max_sessions=request.get('max_sessions')), certificate, key


def quick_materials(address):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_dir = WEB/'quick-ca'
    trusted_dir(WEB, 0, 0o711, create=True)

    def identity(name, days, deadline):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        builder = (x509.CertificateBuilder().subject_name(subject).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now-datetime.timedelta(minutes=5))
                   .not_valid_after(min(now+datetime.timedelta(days=days), deadline))
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
        return builder, subject, key

    def pem_key(key):
        return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())

    if not ca_dir.exists():
        deadline = now+datetime.timedelta(days=1825)
        builder, issuer, ca_key = identity('ZMacRDS 局域网证书 '+os.urandom(4).hex().upper(), 1825, deadline)
        ca = (builder.issuer_name(issuer)
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
              .add_extension(x509.NameConstraints([x509.IPAddress(n) for n in PRIVATE_NETWORKS], None), critical=True)
              .sign(ca_key, hashes.SHA256()))
        stage = pathlib.Path(tempfile.mkdtemp(prefix='quick-ca-', dir=WEB))
        try:
            (stage/'root.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
            (stage/'key.pem').write_bytes(pem_key(ca_key))
            (stage/'key.pem').chmod(0o600)
            stage.rename(ca_dir)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    trusted_dir(ca_dir, 0, 0o700)
    root = read_regular(ca_dir/'root.pem')
    ca = x509.load_pem_x509_certificate(root)
    ca_key = serialization.load_pem_private_key(read_regular(ca_dir/'key.pem'), password=None)
    ca.verify_directly_issued_by(ca)
    if ca_key.public_key() != ca.public_key() or ca.not_valid_after_utc <= now+datetime.timedelta(days=1):
        raise ValueError('局域网根证书不可用或即将过期，请在高级设置中导入新证书。')
    builder, _, key = identity('ZMacRDS '+address, 365, ca.not_valid_after_utc)
    leaf = (builder.issuer_name(ca.subject)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address(address))]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return leaf.public_bytes(serialization.Encoding.PEM)+root, pem_key(key)


def quick_start(request, setup, installer):
    if set(request)-{'max_sessions'} != {'address', 'subnet', 'port'} or any(
            not isinstance(request[key], str) for key in ('address', 'subnet')) or type(request['port']) is not int:
        raise ValueError('请选择局域网地址、网段和端口。')
    args = argparse.Namespace(**request, hostname='', media_port=request['port'], quick_tls=True)
    if args.port == 3390:
        raise ValueError('3390 是本机保留端口。')
    if 'max_sessions' in request and (type(args.max_sessions) is not int or not 1 <= args.max_sessions <= 64):
        raise ValueError('同时连接数必须在 1–64 之间。')
    try:
        address, _, _ = lan_settings(args, setup.local_addresses())
    except ValueError as error:
        raise ValueError('请选择此 Mac 上可用的局域网 IPv4 地址，并核对允许访问的网段。') from error
    return start(args, setup, installer, *quick_materials(address))


def start(args, setup, installer, https_cert, https_key):
    try:
        address, subnet, hostname = lan_settings(args, setup.local_addresses())
    except ValueError as error:
        raise ValueError('请选择此 Mac 上可用的局域网 IPv4 地址，并核对允许访问的网段。') from error
    if b'PRIVATE KEY' in https_cert:
        raise ValueError('请分别选择证书链和私钥文件。')
    previous_record = saved()
    max_sessions = getattr(args, 'max_sessions', None)
    if max_sessions is None:
        max_sessions = (previous_record or {}).get('max_sessions', DEFAULT_SESSIONS)
    if type(max_sessions) is not int or not 1 <= max_sessions <= 64:
        raise ValueError('同时连接数必须在 1–64 之间，并与本机验收容量匹配。')
    account = worker()
    allow_managed_runtime(account)
    trusted_dir(WEB, 0, 0o711, create=True)
    runtime = WEB/'runtime'
    trusted_dir(runtime, account.pw_uid, 0o700, create=True)
    stage = WEB/('tls-' + str(time.time_ns()))
    trusted_dir(stage, 0, 0o711, create=True)
    prev = read_regular(PLIST) if PLIST.exists() else None
    old_config = setup.existing()
    changed = False
    rdp_disabled = False
    try:
        def put(name, data):
            path = stage/name
            installer.replace(path, data, 0o400)
            subprocess.run(['/bin/chmod', '+a', f'{account.pw_name} allow read,readattr', str(path)],
                           check=True, timeout=5)
        put('https.pem', https_cert)
        put('https-key.pem', https_key)
        authority = f'{hostname}:{args.port}'
        cmd = [GATEWAY, '-listen', f'{address}:{args.port}', '-target', '127.0.0.1',
               '-port', str((old_config or {}).get('port', 3390)), '-certificate', str(stage/'rdp.pem'),
               '-lan-cidr', subnet, '-authority', authority, '-media-port', str(args.media_port),
               '-tls-cert', str(stage/'https.pem'), '-tls-key', str(stage/'https-key.pem'),
               '-management-socket', str(runtime/'status.sock'), '-max-sessions', str(max_sessions)]
        check = subprocess.run(cmd + ['-check-config'], timeout=5, capture_output=True,
                               user=account.pw_uid, group=account.pw_gid, extra_groups=())
        if check.returncode:
            error = check.stderr.decode(errors='replace')
            if 'validity dates' in error:
                raise ValueError('HTTPS 证书已过期或尚未生效。')
            if 'Subject Alternative Name' in error:
                raise ValueError('证书未包含访问地址。请核对域名或 IP 与证书上的名称。')
            if 'hostname' in error:
                raise ValueError('请输入完整的证书域名，或留空使用 IP 证书。')
            raise ValueError('无法读取匹配的 HTTPS 证书和私钥。请选择 PEM 文件与未加密的对应私钥。')
        # Check both transports before changing a running configuration.
        for transport, setting, port in ((socket.SOCK_STREAM, 'port', args.port),
                                         (socket.SOCK_DGRAM, 'udp_port', args.media_port)):
            if prev and previous_record and (previous_record.get('address'), previous_record.get(setting)) == (address, port):
                continue
            try:
                with socket.socket(socket.AF_INET, transport) as port_check:
                    port_check.bind((address, port))
            except OSError as error:
                raise ValueError('网页的 TCP 或 UDP 端口不可用，请选择其他端口。') from error
        setup.allow_firewall(GATEWAY)
        if not old_config or not old_config.get('local_auth'):
            setup.configure_local({'enabled': False, 'port': 3389})
        elif old_config.get('lan_port') is not None:
            setup.configure_network({'enabled': False, 'port': old_config['lan_port']})
            rdp_disabled = True
        elif not job_loaded(SERVER_LABEL):
            setup.bootstrap_network(installer)
            setup.wait_listener(old_config.get('port', 3390))
        rdp_cert = read_regular(BASE/'certificate.pem')
        if b'PRIVATE KEY' in rdp_cert:
            raise ValueError('本地桌面证书无效')
        put('rdp.pem', rdp_cert)
        installer.unload(LABEL)
        changed = True
        installer.replace(PLIST, plistlib.dumps(dict(Label=LABEL.split('/')[1], ProgramArguments=cmd,
                          UserName=account.pw_name, RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
                          SoftResourceLimits={'NumberOfFiles': 4096},
                          ProcessType='Background', WorkingDirectory='/var/empty')), 0o644)
        bootstrap()
        wait_https(address, hostname, args.port, https_cert)
        # Read only the public leaf expiry; no private key or credentials enter status.
        expires = expiry(stage/'https.pem')
        settings = dict(url='https://' + authority, address=address, subnet=subnet, hostname=hostname,
                        port=args.port, udp_port=args.media_port, max_sessions=max_sessions, tls_dir=stage.name,
                        expires=expires, quick_tls=getattr(args, 'quick_tls', False))
        installer.replace(WEB/'settings.json', json.dumps(settings).encode())
    except Exception:
        if changed:
            installer.unload(LABEL)
            if prev:
                installer.replace(PLIST, prev, 0o644)
                with contextlib.suppress(subprocess.CalledProcessError):
                    bootstrap()
            else:
                PLIST.unlink(missing_ok=True)
        for path in stage.iterdir():
            path.unlink()
        stage.rmdir()
        if rdp_disabled:
            with contextlib.suppress(OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                setup.configure_network({'enabled': True, 'port': old_config['lan_port']})
        raise
    if previous_record and previous_record['tls_dir'] != stage.name:
        previous_dir = WEB/previous_record['tls_dir']
        if previous_dir.is_dir() and not previous_dir.is_symlink():
            for name in ('rdp.pem', 'https.pem', 'https-key.pem'):
                (previous_dir/name).unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                previous_dir.rmdir()
    return settings['url']


def stop(installer):
    settings = saved()
    if settings:
        installer.replace(WEB/'settings.json', json.dumps(settings).encode())
    installer.unload(LABEL)
    PLIST.unlink(missing_ok=True)


def operation(verb, request, setup, installer):
    if verb == 'quick-web':
        return {'ok': True, 'message': '网页服务已启动：'+quick_start(request, setup, installer)}
    if verb == 'configure-web':
        args, certificate, key = materials(request)
        url = start(args, setup, installer, certificate, key)
        return {'ok': True, 'message': '网页服务已启动：' + url}
    if request:
        raise ValueError('此操作不接受参数')
    if verb == 'export-web':
        if not (saved() or {}).get('quick_tls'):
            raise ValueError('当前服务未使用自动生成的局域网证书。')
        trusted_dir(WEB/'quick-ca', 0, 0o700)
        root = read_regular(WEB/'quick-ca'/'root.pem')
        return {'ok': True, 'certificate_der': base64.b64encode(ssl.PEM_cert_to_DER_cert(root.decode())).decode()}
    if verb == 'start-web':
        if job_running(LABEL):
            return {'ok': True, 'message': '网页服务已在运行。'}
        settings = saved()
        if not settings:
            raise ValueError('请先在「网页设置」选择访问地址并导入 HTTPS 证书和私钥。')
        stage = WEB/settings['tls_dir']
        if stage.is_symlink():
            raise ValueError('已保存的证书目录无效')
        args = argparse.Namespace(address=settings['address'], subnet=settings['subnet'], hostname=settings['hostname'],
                                  port=settings['port'], media_port=settings['udp_port'],
                                  quick_tls=settings.get('quick_tls', False))
        if args.quick_tls and subprocess.run(['/usr/bin/openssl','x509','-in',str(stage/'https.pem'),
                '-checkend','2592000','-noout'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5).returncode:
            return {'ok': True, 'message': '网页服务已启动：'+quick_start(
                dict(address=args.address, subnet=args.subnet, port=args.port,
                     max_sessions=settings.get('max_sessions', DEFAULT_SESSIONS)), setup, installer)}
        url = start(args, setup, installer, read_regular(stage/'https.pem'), read_regular(stage/'https-key.pem'))
        return {'ok': True, 'message': '网页服务已启动：' + url}
    if verb == 'stop-web':
        stop(installer)
        return {'ok': True, 'message': '网页服务已停止。用户的桌面和应用仍保留。'}
    raise ValueError('不支持的网页管理操作')


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--control-json':
        try:
            if os.geteuid() != 0:
                raise ValueError('需要管理员权限')
            raw = sys.stdin.buffer.read(65537)
            if len(raw) > 65536:
                raise ValueError('网页设置请求过长')
            setup = setup_module()
            reply = operation(sys.argv[2], json.loads(raw), setup, setup.installer_module())
        except ValueError as error:
            reply = {'ok': False, 'error': 'web_configuration', 'message': str(error)}
        except RuntimeError as error:
            reply = {'ok': False, 'error': 'web_operation', 'message': str(error)}
        except (OSError, KeyError, subprocess.SubprocessError):
            reply = {'ok': False, 'error': 'web_operation', 'message': '网页服务未能完成操作。请检查网络或重新安装。'}
        print(json.dumps(reply))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest='verb', required=True)
    verbs.add_parser('status')
    verbs.add_parser('stop', help='stop HTTPS and prevent it from starting at the next boot')
    start_cmd = verbs.add_parser('start', help='configure HTTPS and start it now and at boot')
    start_cmd.add_argument('--address', required=True, help='local private IPv4 address')
    start_cmd.add_argument('--subnet', required=True, help='permitted local IPv4 subnet in CIDR notation')
    start_cmd.add_argument('--hostname', help='HTTPS hostname; defaults to the local IPv4 address')
    start_cmd.add_argument('--port', type=int, default=8443)
    start_cmd.add_argument('--media-port', type=int, default=8443)
    start_cmd.add_argument('--max-sessions', type=int, help='tested simultaneous desktop limit for this Mac')
    start_cmd.add_argument('--certificate', type=pathlib.Path, required=True, help='HTTPS leaf followed by intermediates')
    start_cmd.add_argument('--private-key', type=pathlib.Path, required=True, help='HTTPS private key PEM')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run with sudo')
    if args.verb == 'status':
        status()
        return
    setup = setup_module()
    installer = setup.installer_module()
    if args.verb == 'stop':
        stop(installer)
        print('网页服务已停止。')
    else:
        print(start(args, setup, installer, read_regular(args.certificate), read_regular(args.private_key)))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'ZMacRDS Web: {error}', file=sys.stderr)
        sys.exit(1)
