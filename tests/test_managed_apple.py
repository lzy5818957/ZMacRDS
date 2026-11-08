import importlib.util
import pathlib
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

spec = importlib.util.spec_from_file_location('managed_apple', pathlib.Path(__file__).resolve().parents[1]/'tools/managed-apple.py')
apple = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apple)


class ManagedAppleTests(unittest.TestCase):
    now = 1800000000

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = cls.key.public_key().public_numbers()
        cls.jwks = {'keys': [{'kid': 'fixture', 'kty': 'RSA', 'use': 'sig', 'alg': 'RS256',
                             'n': apple.encode(numbers.n.to_bytes(256, 'big')).decode(),
                             'e': apple.encode(numbers.e.to_bytes(3, 'big')).decode()}]}
        cls.settings = apple.config(dict(service_id='com.example.desktop', team_id='ABCDEFGHIJ',
                                         key_id='ABCDEFGHIJ', org_id='80a9b7dc-05d6-4d70-8417-30a5f4f80b1a',
                                         email_domain='example.org', redirect_uri='https://desktop.example.org:8443/apple/callback',
                                         screen_key_sha256='a'*64))

    def jwt(self, **replace):
        claims = dict(iss=apple.ISSUER, aud=self.settings['service_id'], sub='immutable.abc',
                      iat=self.now, exp=self.now+180, email='employee@example.org',
                      org_id=self.settings['org_id'], nonce='expected')
        claims.update(replace)
        body = b'.'.join(apple.encode(apple.compact(part)) for part in
                         (dict(kid='fixture', alg='RS256'), claims))
        return (body+b'.'+apple.encode(self.key.sign(body, padding.PKCS1v15(), hashes.SHA256()))).decode()

    def test_signed_tenant_membership_and_browser_nonce(self):
        def verify(**kw):
            return apple.identity(self.jwt(**kw), self.jwks, self.settings, 'expected', self.now)
        self.assertEqual(verify(), 'immutable.abc')
        for wrong in (dict(org_id=None), dict(org_id='another-tenant'), dict(nonce='replay'),
                      dict(email='employee@outside.example.org'), dict(aud='another-app'),
                      dict(exp=self.now), dict(is_private_email=True)):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                verify(**wrong)

    def test_apple_callback_cannot_be_an_ip_address(self):
        with self.assertRaises(ValueError):
            apple.config(dict(self.settings, redirect_uri='https://192.0.2.10:8443/apple/callback'))

    def test_only_one_helper_provisions_local_accounts_at_a_time(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        with TemporaryDirectory() as path, patch.object(apple, 'PRIVATE', pathlib.Path(path)):
            with apple.provisioning_lock():
                with self.assertRaisesRegex(RuntimeError, 'queue timed out'):
                    with apple.provisioning_lock(timeout=0):
                        self.fail('two helpers held the account creation lock')
            with apple.provisioning_lock(timeout=0):
                self.assertTrue((pathlib.Path(path)/'provision.lock').exists())
