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

    def test_bad_network_settings_fail_before_installation_or_firewall_changes(self):
        for request in [{'enabled': True, 'port': True}, {'enabled': 1, 'port': 3389},
                        {'enabled': True, 'port': 3390}, {'enabled': True, 'port': 22},
                        {'enabled': True, 'port': 65536}, {'enabled': True, 'port': 3389, 'bind': '0.0.0.0'}]:
            with self.subTest(request=request), patch.object(setup,'allow_firewall') as firewall, \
                 patch.object(setup,'installer_module') as installer:
                with self.assertRaises(ValueError): setup.configure_local(request)
                firewall.assert_not_called();installer.assert_not_called()

    def test_busy_lan_port_does_not_stop_or_change_a_running_server(self):
        import socket
        with socket.socket() as occupied:
            occupied.bind(('0.0.0.0',0));port=occupied.getsockname()[1]
            with patch.object(setup,'existing',return_value=self.old), \
                 patch.object(setup,'local_accounts',return_value=list(self.accounts.values())), \
                 patch.object(setup,'allow_firewall') as firewall, patch.object(setup,'installer_module') as installer:
                with self.assertRaises(OSError): setup.configure_local({'enabled':True,'port':port})
                firewall.assert_not_called();installer.assert_not_called()

    def test_failed_listener_restart_restores_private_configuration(self):
        import subprocess
        from unittest.mock import Mock
        writes=[]
        module=SimpleNamespace(DAEMON=pathlib.Path('/unused-server.plist'),unload=Mock(),
                               replace=lambda path,data:writes.append(json.loads(data)))
        failed=subprocess.CalledProcessError(5,['/bin/launchctl','bootstrap'])
        with patch.object(setup,'existing',return_value=self.old),patch.object(setup,'installer_module',return_value=module), \
             patch.object(setup.time,'sleep'), \
             patch.object(setup.subprocess,'run',side_effect=[failed]*5+[subprocess.CompletedProcess([],0)]):
            with self.assertRaises(subprocess.CalledProcessError):
                setup.configure_network({'enabled':False,'port':3389})
        self.assertEqual(writes[-1],self.old)
        self.assertEqual(writes[0]['users'],self.old['users'])

    def test_saved_port_occupied_by_another_app_is_not_mistaken_for_our_server(self):
        import socket
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            occupied.bind(('0.0.0.0',0));occupied.listen(1);port=occupied.getsockname()[1]
            with patch.object(setup,'own_listener',return_value=False) as owned:
                with self.assertRaises(OSError):setup.preflight_port(port,dict(self.old,lan_port=port))
                owned.assert_called_once_with(port,self.old['broker_uid'])
            with patch.object(setup,'own_listener',return_value=True):
                setup.preflight_port(port,dict(self.old,lan_port=port))

    def test_launchd_removal_race_is_retried_before_configuration_rollback(self):
        import subprocess
        failed=subprocess.CalledProcessError(5,['/bin/launchctl','bootstrap'])
        with patch.object(setup.time,'sleep'), \
             patch.object(setup.subprocess,'run',side_effect=[failed,subprocess.CompletedProcess([],0)]) as launch:
            setup.bootstrap_network(SimpleNamespace(DAEMON=pathlib.Path('/unused-server.plist')))
        self.assertEqual(launch.call_count,2)

    def test_native_bootstrap_never_sends_a_password_to_a_user_owned_listener(self):
        from unittest.mock import Mock
        def output(command,**kwargs):
            if command[0]=='/usr/sbin/sysctl':return '2000'
            if command[0]=='/bin/ps':return '1\n2\n'
            return 'p123\ncFakeServer\nu505\n'
        with patch.object(setup.subprocess,'check_output',side_effect=output), \
             patch.object(setup.ctypes,'CDLL',return_value=SimpleNamespace(proc_pidpath=Mock())), \
             patch.object(setup.socket,'create_connection') as connection:
            with self.assertRaisesRegex(ValueError,'native screen sharing is not available'):
                setup.open_desktop({'uid':505,'username':'user505','password':'private-fixture'})
            connection.assert_not_called()

    def test_session_admission_preserves_process_headroom_before_native_login(self):
        with patch.object(setup.subprocess,'check_output',side_effect=['2000','1\n'*1800]), \
             patch.object(setup.socket,'create_connection') as connection:
            result=setup.open_desktop({'uid':505,'username':'user505','password':'private-fixture'})
            self.assertFalse(result['ok']);self.assertEqual(result['error'],'desktop_capacity')
            connection.assert_not_called()

    def test_system_auth_connection_info_never_exposes_a_legacy_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            cert=pathlib.Path(directory)/'certificate.pem';cert.write_text('-----BEGIN CERTIFICATE-----\nAQID\n-----END CERTIFICATE-----\n')
            cfg=dict(self.old,certificate=str(cert),local_auth=True,lan_port=3389)
            with patch.object(setup,'existing',return_value=cfg), patch.object(setup,'local_addresses',return_value=['192.168.1.50']):
                reply=setup.connection_info(505)
            self.assertEqual(reply['endpoint'],'192.168.1.50:3389')
            self.assertEqual(reply['password'],'');self.assertTrue(reply['local_auth'])
            self.assertNotIn('preserve-existing-secret',json.dumps(reply))

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
