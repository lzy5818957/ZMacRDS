#!/usr/bin/env python3
"""Read-only pilot: check one managed account in an Apple Business tenant."""

import argparse
import base64
import json
import os
import stat
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

TOKEN_ENDPOINT = 'https://account.apple.com/auth/oauth2/v2/token'
USERS_ENDPOINT = 'https://api-business.apple.com/v1/users'
FIELDS = 'managedAppleAccount,status,isExternalUser'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def read_json(request):
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError('Apple returned an oversized response')
    return json.loads(data)


def base64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=')


def client_assertion(client_id, key_id, key_path):
    fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as pem:
        info = os.fstat(pem.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid() or info.st_size > 16384):
            raise ValueError('Use a private PEM file owned by the current user (chmod 600)')
        key = serialization.load_pem_private_key(pem.read(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError('The Apple Business API key must be a P-256 private key')
    now = int(time.time())
    header = {'alg': 'ES256', 'kid': key_id}
    claims = {'iss': client_id, 'sub': client_id, 'aud': TOKEN_ENDPOINT,
              'iat': now, 'exp': now + 120, 'jti': str(uuid.uuid4())}
    message = b'.'.join(base64url(json.dumps(part).encode()) for part in (header, claims))
    r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    return (message + b'.' + base64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))).decode()


def get_token(client_id, key_id, key_path):
    data = urlencode({
        'grant_type': 'client_credentials', 'client_id': client_id, 'scope': 'business.api',
        'client_assertion_type': 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
        'client_assertion': client_assertion(client_id, key_id, key_path),
    }).encode()
    response = read_json(Request(TOKEN_ENDPOINT, data=data))
    token = response['access_token']
    if not isinstance(token, str) or not token or response['token_type'].lower() != 'bearer':
        raise ValueError('Unexpected Apple Business token response')
    return token


def check_account(account, token):
    url = USERS_ENDPOINT + '?' + urlencode({'fields[users]': FIELDS, 'limit': 100})
    visited = set()
    match = None
    while url:
        address = urlsplit(url)
        # Never forward this tenant's token to a URL outside the Users API.
        if (address.scheme, address.netloc, address.path) != ('https', 'api-business.apple.com', '/v1/users'):
            raise ValueError('Unexpected Apple Business pagination URL')
        if url in visited or len(visited) >= 100:
            raise ValueError('Apple Business pagination did not complete')
        visited.add(url)
        page = read_json(Request(url, headers={'Authorization': 'Bearer ' + token}))
        for record in page['data']:
            attributes = record['attributes']
            managed_account = attributes.get('managedAppleAccount')
            if isinstance(managed_account, str) and managed_account.casefold() == account.casefold():
                if match is not None:
                    raise ValueError('More than one directory entry matched this account')
                match = attributes
        url = page.get('links', {}).get('next')
    if match is None:
        return 'not_found'
    return 'active' if match.get('status') == 'ACTIVE' and match.get('isExternalUser') is False else 'ineligible'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-id', required=True)
    parser.add_argument('--key-id', required=True)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--account', required=True, help='Full Managed Apple Account address')
    args = parser.parse_args()
    try:
        result = check_account(args.account.strip(), get_token(args.client_id, args.key_id, args.key_file))
    except HTTPError as error:
        print(f'Apple request failed (HTTP {error.code})', file=sys.stderr)
        return 2
    except (URLError, OSError, ValueError, TypeError, KeyError, AttributeError):
        print('Could not verify the account. Check the private key, API access and connectivity.', file=sys.stderr)
        return 2
    print(json.dumps({'directory_result': result}))
    return 0 if result == 'active' else 1


if __name__ == '__main__':
    sys.exit(main())
