import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('m3_case', Path(__file__).resolve().parents[1] / 'tools/m3-case.py')
m3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m3)


class M3CaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.users = []
        for uid in (502, 503):
            p = self.root / f'{uid}.json'
            p.write_text(json.dumps(dict(certificate='/test/cert.pem', uid=uid, nonce=uid + 111,
                                         username=f'test{uid}', password=f'synthetic-password-{uid}')))
            p.chmod(0o600)
            self.users.append(p)
        self.out = self.root / 'new-case'

    def test_distinct_inputs_spread_over_real_overlap_and_private_output(self):
        m3.prepare(*self.users, self.out, '30')
        for side in 'ab':
            case = json.loads((self.out / f'{side}.json').read_text())
            text = [a for a in case['actions'] if a['type'] == 'text']
            self.assertEqual(len({a['text'] for a in text}), 100)
            self.assertEqual(text[29]['text'], f'{side.upper()}30-029-中文')
            self.assertGreater(text[-1]['at_ms'] - text[0]['at_ms'], 29 * 60 * 1000)
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in self.out.iterdir()))
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)

    def test_identical_passwords_cannot_claim_cross_credential_coverage(self):
        second = json.loads(self.users[1].read_text())
        second['password'] = json.loads(self.users[0].read_text())['password']
        self.users[1].write_text(json.dumps(second))
        with self.assertRaisesRegex(ValueError, 'different'):
            m3.prepare(*self.users, self.out, '30')
        self.assertFalse(self.out.exists())

    def test_public_or_symlinked_base_is_rejected(self):
        self.users[0].chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private'):
            m3.prepare(*self.users, self.out, '30')
        self.users[0].chmod(0o600)
        link = self.root / 'linked'
        link.symlink_to(self.users[0])
        with self.assertRaises(OSError):
            m3.prepare(link, self.users[1], self.out, '30')
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
