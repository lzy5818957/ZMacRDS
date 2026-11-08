#!/usr/bin/env python3
"""Root-side Apple web authentication and just-in-time Mac account creation."""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import plistlib
import pwd
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

ISSUER = 'https://appleid.apple.com'
PRIVATE = pathlib.Path('/var/db/zmacrds-managed')
GROUP = 'com.apple.access_screensharing'
KEY = re.compile(r'[A-Za-z0-9_-]{1,128}')
SHORT = re.compile(r'zr[a-f0-9]{24}')
PIN = re.compile(r'[a-f0-9]{64}')
DNS = re.compile(r'(?:[a-z0-9][a-z0-9-]*\.)+[a-z][a-z0-9-]*')
ACCOUNT_LIMIT = 24


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def apple_json(path, data=None):
    request = Request(ISSUER + path, data=None if data is None else urlencode(data).encode())
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('Apple response is too large')
    return json.loads(raw)


def encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=')


def decode(raw):
    if not re.fullmatch(rb'[A-Za-z0-9_-]+', raw):
        raise ValueError('invalid JWT encoding')
    return base64.urlsafe_b64decode(raw + b'=' * (-len(raw) % 4))


def compact(mapping):
    return json.dumps(mapping, separators=(',', ':')).encode()


def config(raw):
    required = {'service_id', 'team_id', 'key_id', 'org_id', 'email_domain', 'redirect_uri', 'screen_key_sha256'}
    if set(raw) != required or any(not isinstance(raw[k], str) or len(raw[k]) > 512 for k in required):
        raise ValueError('invalid Apple configuration')
    url = urlsplit(raw['redirect_uri'])
    if (url.scheme != 'https' or not DNS.fullmatch(url.hostname or '') or url.username is not None
            or url.query or url.fragment or url.path != '/apple/callback'
            or (url.port is not None and not 1024 <= url.port <= 65535 and url.port != 443)):
        raise ValueError('Apple requires a registered HTTPS hostname and /apple/callback')
    if (not re.fullmatch(r'[A-Za-z0-9.\-]{3,128}', raw['service_id'])
            or any(not re.fullmatch(r'[A-Z0-9]{10}', raw[k]) for k in ('team_id', 'key_id'))
            or not DNS.fullmatch(raw['email_domain']) or not PIN.fullmatch(raw['screen_key_sha256'])
            or not re.fullmatch(r'[A-Za-z0-9-]{3,128}', raw['org_id'])):
        raise ValueError('invalid Apple service, company, or Screen Sharing settings')
    return raw


def read_private(path, size=16384):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        meta = os.fstat(stream.fileno())
        if meta.st_uid != 0 or not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077 or not 0 < meta.st_size <= size:
            raise ValueError('private file must be owned by root and mode 0600')
        raw = stream.read(size + 1)
    if len(raw) > size:
        raise ValueError('private file exceeds size limit')
    return raw


def directory(path, create=False):
    if create:
        with contextlib.suppress(FileExistsError):
            path.mkdir(mode=0o700)
    info = path.lstat()
    if info.st_uid != 0 or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('private directory must be owned by root and mode 0700')


def save(path, record):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(compact(record))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        pathlib.Path(name).unlink(missing_ok=True)


def client_secret(settings, pem, now):
    key = serialization.load_pem_private_key(pem, None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError('expected Apple Sign in with Apple P-256 key')
    claims = {'iss': settings['team_id'], 'iat': now, 'exp': now + 120,
              'aud': ISSUER, 'sub': settings['service_id']}
    signed = b'.'.join(encode(compact(part)) for part in ({'alg': 'ES256', 'kid': settings['key_id']}, claims))
    r, s = decode_dss_signature(key.sign(signed, ec.ECDSA(hashes.SHA256())))
    return (signed + b'.' + encode(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))).decode()


def identity(token, jwks, settings, nonce, now):
    if not isinstance(token, str) or not 16 < len(token) <= 12288:
        raise ValueError('invalid identity token')
    header, claims, signature = token.encode('ascii').split(b'.')
    details = json.loads(decode(header))
    kid = details.get('kid')
    if details.get('alg') != 'RS256' or not isinstance(kid, str) or not KEY.fullmatch(kid):
        raise ValueError('invalid identity algorithm')
    matches = [item for item in jwks['keys'] if item.get('kid') == kid and item.get('kty') == 'RSA'
               and item.get('use') == 'sig' and item.get('alg') == 'RS256']
    if len(matches) != 1:
        raise ValueError('no matching Apple signing key')
    public = matches[0]
    def integer(field):
        return int.from_bytes(decode(public[field].encode('ascii')), 'big')
    modulus, exponent = integer('n'), integer('e')
    if not 2048 <= modulus.bit_length() <= 4096 or not 3 <= exponent <= 65537:
        raise ValueError('invalid signing key')
    rsa.RSAPublicNumbers(exponent, modulus).public_key().verify(
        decode(signature), header + b'.' + claims, padding.PKCS1v15(), hashes.SHA256())
    values = json.loads(decode(claims))
    subject, email, tenant = (values.get(field) for field in ('sub', 'email', 'org_id'))
    issued, expiry = values.get('iat'), values.get('exp')
    if (values.get('iss') != ISSUER or values.get('aud') != settings['service_id']
            or values.get('nonce') != nonce or type(issued) is not int or type(expiry) is not int
            or not now - 600 <= issued <= now + 60 or not now < expiry <= issued + 86400
            or not isinstance(subject, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,255}', subject)
            or tenant != settings['org_id'] or not isinstance(email, str) or len(email) > 254
            or email.count('@') != 1 or not email.split('@')[0]
            or email.rsplit('@', 1)[1].lower() != settings['email_domain']
            or values.get('is_private_email') in (True, 'true')):
        raise ValueError('not an eligible identity from this Apple Business organization')
    # The signed organization claim is required. A matching email suffix alone
    # would also allow personal Apple Accounts registered with a company email.
    return subject


def lookup(username):
    result = subprocess.run(['/usr/bin/dscl', '-plist', '.', '-read', '/Users/' + username],
                            capture_output=True, timeout=5)
    if result.returncode != 0:
        if b'eDSRecordNotFound' in result.stdout + result.stderr or b'-14136' in result.stdout + result.stderr:
            return None
        raise RuntimeError('cannot query local accounts')
    return plistlib.loads(result.stdout)


def local_account(username, record):
    data = lookup(username)
    if data is None:
        return None
    field = lambda key: data.get('dsAttrTypeStandard:' + key)
    if (field('GeneratedUID') != [record['guid']] or field('UniqueID') != [str(record['uid'])]
            or field('NFSHomeDirectory') != ['/Users/' + username] or field('PrimaryGroupID') != ['20']):
        raise ValueError('the existing local account is not owned by ZMacRDS')
    return data


def run(args, text=None):
    subprocess.run(args, input=text, text=True, check=True, timeout=8,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def membership(username, group):
    result = subprocess.run(['/usr/bin/dsmemberutil', 'checkmembership', '-U', username, '-G', group],
                            capture_output=True, text=True, check=True, timeout=5)
    if result.stdout.strip() == 'user is a member of the group':
        return True
    if result.stdout.strip() == 'user is not a member of the group':
        return False
    raise RuntimeError('unexpected local membership response')


def provision(settings, subject):
    # Stable by issuer, service and immutable subject; no merge by email or name.
    name = 'zr' + hashlib.sha256(compact([ISSUER, settings['service_id'], subject])).hexdigest()[:24]
    profiles = PRIVATE/'accounts'
    directory(profiles, True)
    path = profiles/(name + '.json')
    if path.exists():
        record = json.loads(read_private(path))
    else:
        if len(list(profiles.glob('zr*.json'))) >= ACCOUNT_LIMIT:
            raise ValueError('managed desktop account limit reached')
        if lookup(name) is not None or os.path.lexists('/Users/' + name):
            raise ValueError('local account or home collision')
        used = {entry.pw_uid for entry in pwd.getpwall()}
        number = next((candidate for candidate in range(10000, 20000) if candidate not in used), None)
        if number is None:
            raise ValueError('no local UID available')
        record = {'guid': str(uuid.uuid4()).upper(), 'uid': number,
                  'password': 'A9-' + secrets.token_urlsafe(32), 'ready': False}
        save(path, record)
    home = pathlib.Path('/Users')/name
    data = local_account(name, record)
    if data is None:
        if record['ready'] or os.path.lexists(home):
            raise ValueError('managed account or home was removed or altered')
        try:
            pwd.getpwuid(record['uid'])
        except KeyError:
            pass
        else:
            raise ValueError('local UID collision')
        node = '/Users/' + name
        for field, value in (('GeneratedUID', record['guid']), ('UniqueID', str(record['uid'])), ('PrimaryGroupID', '20'),
                             ('NFSHomeDirectory', str(home)), ('UserShell', '/bin/zsh'), ('RealName', name), ('IsHidden', '1')):
            run(['/usr/bin/dscl', '.', '-create', node, field, value])
        if local_account(name, record) is None:
            raise RuntimeError('new account is missing')
    if membership(name, 'admin'):
        raise ValueError('managed desktop account must not be an administrator')
    if not record['ready']:
        password = record['password']
        if not re.fullmatch(r'A9-[A-Za-z0-9_-]{43}', password):
            raise ValueError('invalid generated credential')
        # dscl interactive mode reads stdin. Never put a password in argv.
        run(['/usr/bin/dscl', '-q', '.'], f'passwd /Users/{name} {password}\nquit\n')
        if not home.exists():
            run(['/usr/sbin/createhomedir', '-c', '-u', name])
    meta = home.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != record['uid']:
        raise ValueError('managed account home does not belong to this account')
    fd = os.open(home, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        if os.fstat(fd).st_uid != record['uid']:
            raise ValueError('managed home changed during creation')
        if not record['ready']:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    if not membership(name, GROUP):
        if record['ready']:
            raise ValueError('access to this managed desktop was removed')
        # Only add to a pre-existing restricted Screen Sharing group. No change
        # to the global switch, unrestricted guest VNC or other users.
        run(['/usr/sbin/dseditgroup', '-o', 'edit', '-a', name, '-t', 'user', GROUP])
        if not membership(name, GROUP):
            raise RuntimeError('managed user not allowed by Screen Sharing')
    if not record['ready']:
        record['ready'] = True
        save(path, record)
    return {'ok': True, 'username': name, 'password': record['password'],
            'screen_key_sha256': settings['screen_key_sha256']}


@contextlib.contextmanager
def provisioning_lock(timeout=35):
    # Network verification happens before this lock. Only local account and
    # UID allocation is serialized across simultaneous Apple callbacks.
    fd = os.open(PRIVATE/'provision.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('invalid provisioning lock')
        limit = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= limit:
                    raise RuntimeError('local account provisioning queue timed out')
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def operation(verb, data):
    directory(PRIVATE)
    cfg = config(json.loads(read_private(PRIVATE/'apple.json')))
    if verb == 'info' and set(data) == {'origin'}:
        if data['origin'] != cfg['redirect_uri'].removesuffix('/apple/callback'):
            raise ValueError('Apple callback does not match this web address')
        return {'ok': True, 'service_id': cfg['service_id'], 'redirect_uri': cfg['redirect_uri']}
    if verb != 'exchange' or set(data) != {'code', 'nonce'}:
        raise ValueError('unsupported Apple login operation')
    code, nonce = data['code'], data['nonce']
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{1,2048}', code) or not isinstance(nonce, str) or not KEY.fullmatch(nonce):
        raise ValueError('invalid authorization response')
    token = apple_json('/auth/token', {'client_id': cfg['service_id'],
                      'client_secret': client_secret(cfg, read_private(PRIVATE/'apple.p8'), int(time.time())),
                      'code': code, 'grant_type': 'authorization_code', 'redirect_uri': cfg['redirect_uri']})['id_token']
    subject = identity(token, apple_json('/auth/keys'), cfg, nonce, int(time.time()))
    with provisioning_lock():
        return provision(cfg, subject)


def main():
    if os.geteuid() != 0 or len(sys.argv) != 2:
        return 2
    try:
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise ValueError('request too large')
        result = operation(sys.argv[1], json.loads(raw))
    except FileNotFoundError:
        result = {'ok': False, 'error': 'not_configured'}
    except (InvalidSignature, URLError, KeyError, TypeError, ValueError, OSError,
            RuntimeError, subprocess.SubprocessError):
        result = {'ok': False, 'error': 'apple_login_failed'}
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
