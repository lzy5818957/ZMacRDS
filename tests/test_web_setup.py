"""Verify LAN setup with the same cryptography used by the installer."""

import importlib.util
import ipaddress
import os
import pathlib
import ssl
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography import x509
from cryptography.hazmat.primitives import serialization


spec = importlib.util.spec_from_file_location(
    'web_admin', pathlib.Path(__file__).resolve().parents[1]/'tools/web-lan-admin.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


class WebSetupTests(unittest.TestCase):
    def test_quick_setup_reissues_for_a_new_ip_without_changing_the_exported_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = pathlib.Path(tmp)/'web'
            original = web.trusted_dir
            with patch.object(web, 'WEB', store), patch.object(
                    web, 'trusted_dir', side_effect=lambda p, _, m, **kw: original(p, os.getuid(), m, **kw)):
                first, _ = web.quick_materials('192.168.12.20')
                second, key = web.quick_materials('192.168.15.24')
                with patch.object(web, 'saved', return_value={'quick_tls': True}):
                    exported = web.operation('export-web', {}, None, None)
            first_leaf, first_root = x509.load_pem_x509_certificates(first)
            second_leaf, second_root = x509.load_pem_x509_certificates(second)
            first_leaf.verify_directly_issued_by(first_root)
            second_leaf.verify_directly_issued_by(second_root)
            self.assertEqual(first_root, second_root)
            self.assertEqual(second_root.public_bytes(serialization.Encoding.DER),
                             ssl.PEM_cert_to_DER_cert((store/'quick-ca/root.pem').read_text()))
            self.assertEqual(web.base64.b64decode(exported['certificate_der']),
                             second_root.public_bytes(serialization.Encoding.DER))
            self.assertEqual((store/'quick-ca/key.pem').stat().st_mode & 0o777, 0o600)
            self.assertEqual(second_leaf.public_key(), serialization.load_pem_private_key(key, None).public_key())
            self.assertEqual(second_leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                             .value.get_values_for_type(x509.IPAddress), [ipaddress.IPv4Address('192.168.15.24')])

    def test_quick_setup_only_accepts_valid_capacity_and_private_bound_subnets(self):
        setup = SimpleNamespace(local_addresses=lambda: ['192.168.10.5'])
        request = dict(address='192.168.10.5', subnet='192.168.10.0/24', port=8443, max_sessions=32)
        with patch.object(web, 'quick_materials', return_value=(b'cert', b'key')) as material, \
                patch.object(web, 'start', return_value='https://192.168.10.5:8443') as start:
            for value in (dict(max_sessions=True), dict(max_sessions=65), dict(port=3390),
                          dict(subnet='0.0.0.0/0'), dict(address='192.168.10.8')):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    web.quick_start(dict(request, **value), setup, Mock())
            material.assert_not_called()
            start.assert_not_called()
            self.assertEqual(web.quick_start(request, setup, Mock()), 'https://192.168.10.5:8443')
            self.assertEqual(start.call_args.args[0].max_sessions, 32)
