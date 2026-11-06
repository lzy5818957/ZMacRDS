"""Service file writes and launchd transition failures must fail safely."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('service_installer', Path(__file__).resolve().parents[1] / 'tools/install-service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

class ServiceTests(unittest.TestCase):
    def test_management_preflight_failure_preserves_live_services(self):
        for bad_input in ['unpaired', 'controller_signature', 'manager_signature']:
            with self.subTest(bad_input=bad_input), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                for name in ['server', 'launcher', 'cert', 'key', 'controller']:
                    (root/name).write_bytes(b'fixture')
                cfg = {'broker_uid': 501, 'certificate': str(root/'cert'), 'private_key': str(root/'key'),
                       'users': [{'uid': 505, 'credentials': {'username': 'test_a'}}]}
                accounts = {501: SimpleNamespace(pw_uid=501),
                            505: SimpleNamespace(pw_uid=505, pw_name='test_a', pw_dir=folder)}
                args = SimpleNamespace(config=root/'config', app=root/'app', server=root/'server',
                                       launcher=root/'launcher', controller=root/'controller',
                                       manager=None if bad_input == 'unpaired' else root/'manager')
                def verify(command, **kwargs):
                    if ((bad_input == 'controller_signature' and command[-1] == str(root/'controller')) or
                        (bad_input == 'manager_signature' and command[-1] == str(root/'manager'))):
                        raise subprocess.CalledProcessError(1, command)
                    return subprocess.CompletedProcess(command, 0)
                with patch.object(service.os, 'geteuid', return_value=0), \
                     patch.object(service, 'private_json', return_value=cfg), \
                     patch.object(service.pwd, 'getpwuid', side_effect=accounts.__getitem__), \
                     patch.object(service.subprocess, 'run', side_effect=verify), \
                     patch.object(service, 'unload') as unload:
                    with self.assertRaises(ValueError if bad_input == 'unpaired' else subprocess.CalledProcessError):
                        service.install(args)
                    unload.assert_not_called()

    def test_bad_update_keeps_the_running_service_loaded(self):
        for signature_valid in [False, True]:
            with self.subTest(signature_valid=signature_valid), tempfile.TemporaryDirectory() as folder:
                cfg = {'broker_uid': 501, 'certificate': folder + '/cert', 'private_key': folder + '/key',
                       'users': [{'uid': 505, 'credentials': {'username': 'test_a'}}]}
                users = {501: SimpleNamespace(pw_uid=501),
                         505: SimpleNamespace(pw_uid=505, pw_name='test_a', pw_dir=folder)}
                args = SimpleNamespace(config=Path(folder)/'config', app=Path(folder)/'app',
                                       server=Path(folder)/'missing-server', launcher=Path(folder)/'launcher')
                result = subprocess.CompletedProcess([], 0)
                failure = subprocess.CalledProcessError(1, ['codesign'])
                with patch.object(service.os, 'geteuid', return_value=0), \
                     patch.object(service, 'private_json', return_value=cfg), \
                     patch.object(service.pwd, 'getpwuid', side_effect=users.__getitem__), \
                     patch.object(service.subprocess, 'run', return_value=result, side_effect=None if signature_valid else failure), \
                     patch.object(service, 'unload') as unload:
                    with self.assertRaises(FileNotFoundError if signature_valid else subprocess.CalledProcessError):
                        service.install(args)
                    unload.assert_not_called()

    def test_replacement_does_not_follow_a_symlink_or_leave_a_partial_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'private.json'
            service.replace(target, b'{"complete":true}')
            self.assertEqual(json.loads(target.read_bytes()), {'complete': True})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            alias = Path(folder) / 'alias'
            alias.symlink_to(target)
            with self.assertRaises(ValueError):
                service.replace(alias, b'corrupted')
            self.assertEqual(json.loads(target.read_bytes()), {'complete': True})
            self.assertEqual(len(list(Path(folder).glob('.install-*'))), 0)

    def test_private_config_rejects_public_symlink_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config'
            path.write_text('{}'); path.chmod(0o600)
            self.assertEqual(service.private_json(path), {})
            alias = Path(folder) / 'alias'; alias.symlink_to(path)
            with self.assertRaises(OSError):
                service.private_json(alias)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                service.private_json(path)
            path.chmod(0o600); path.write_bytes(b' ' * 65537)
            with self.assertRaises(ValueError):
                service.private_json(path)

    def test_waits_for_launchd_to_finish_unloading(self):
        results = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1)]
        with patch.object(service.subprocess, 'run', side_effect=results) as run, patch.object(service.time, 'sleep') as sleep:
            service.unload('gui/505/org.zmacrds.desktop')
            self.assertEqual(run.call_count, 4)
            self.assertEqual(sleep.call_count, 2)

    def test_refuses_to_continue_if_a_service_never_unloads(self):
        with patch.object(service.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), patch.object(service.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                service.unload('system/org.zmacrds.server')

if __name__ == '__main__':
    unittest.main()
