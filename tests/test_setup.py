"""Administrator setup must preserve identities and reject unsafe requests before mutation."""
import importlib.util
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('setup_backend', pathlib.Path(__file__).resolve().parents[1]/'tools/configure-service.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.accounts = {value: SimpleNamespace(pw_uid=value, pw_name=f'user{value}', pw_dir=f'/Users/user{value}')
                         for value in [501, 505, 506, 507]}
        self.lookup = patch.object(setup.pwd, 'getpwuid', side_effect=self.accounts.__getitem__)
        self.lookup.start(); self.addCleanup(self.lookup.stop)
        self.old = {'broker_uid': 501, 'users': [{'uid': 505, 'credentials': {'username': 'user505', 'password': 'preserve-existing-secret'}}]}

    def test_invalid_requests_cannot_stop_or_reconfigure_services(self):
        for request in [
            {'broker_uid': True, 'desktop_uids': [505]},
            {'broker_uid': 0, 'desktop_uids': [505]},
            {'broker_uid': 501, 'desktop_uids': [True]},
            {'broker_uid': 501, 'desktop_uids': [505, 505]},
            {'broker_uid': 501, 'desktop_uids': [501]},
            {'broker_uid': 501, 'desktop_uids': []},
            {'broker_uid': 501, 'desktop_uids': [999]},
            {'broker_uid': 501, 'desktop_uids': [505], 'command': '/bin/sh'},
            {'broker_uid': 501, 'desktop_uids': [505], 'certificate': '/tmp/injected'},
        ]:
            with self.subTest(request=request), patch.object(setup, 'existing', return_value=None), \
                 patch.object(setup, 'installer_module') as install, patch.object(setup, 'certificate') as cert:
                with self.assertRaises((ValueError, KeyError)):
                    setup.configure(request)
                install.assert_not_called(); cert.assert_not_called()

    def test_preserves_existing_broker_and_desktop_access(self):
        for request in [{'broker_uid': 507, 'desktop_uids': [505]},
                        {'broker_uid': 501, 'desktop_uids': [506]}]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                setup.validate(request, self.old)
        broker, accounts = setup.validate({'broker_uid': 501, 'desktop_uids': [505, 506]}, self.old)
        self.assertEqual(broker.pw_uid, 501)
        self.assertEqual([account.pw_uid for account in accounts], [505, 506])

    def test_hidden_service_accounts_are_not_desktop_accounts(self):
        self.accounts[507].pw_name = '_service'
        with self.assertRaises(ValueError):
            setup.validate({'broker_uid': 501, 'desktop_uids': [507]}, None)
        self.accounts[507].pw_name = 'user507'; self.accounts[507].pw_dir = '/var/service'
        with self.assertRaises(ValueError):
            setup.validate({'broker_uid': 501, 'desktop_uids': [507]}, None)

    def test_bad_existing_configuration_is_never_treated_as_first_install(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(setup, 'BASE', pathlib.Path(directory)):
            path = pathlib.Path(directory)/'server.json'
            self.assertIsNone(setup.existing())
            path.symlink_to(pathlib.Path(directory)/'missing')
            with self.assertRaises(OSError):
                setup.existing()

    def test_connection_secret_is_bound_to_exact_configured_local_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            cert = pathlib.Path(directory)/'certificate.pem'
            # ssl accepts a PEM-encoded bounded DER byte string for fingerprinting.
            cert.write_text('-----BEGIN CERTIFICATE-----\nAQID\n-----END CERTIFICATE-----\n')
            cfg = dict(self.old, certificate=str(cert))
            with patch.object(setup, 'existing', return_value=cfg):
                reply = setup.connection_info(505)
                self.assertEqual(reply['username'], 'user505')
                self.assertEqual(reply['password'], 'preserve-existing-secret')
                self.assertEqual(len(reply['sha256']), 95)
                with self.assertRaises(ValueError):
                    setup.connection_info(506)
                cfg['users'][0]['credentials']['username'] = 'different'
                with self.assertRaises(ValueError):
                    setup.connection_info(505)


if __name__ == '__main__':
    unittest.main()
