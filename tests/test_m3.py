import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('m3_verify', Path(__file__).resolve().parents[1] / 'tools/m3-verify.py')
m3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m3)


class M3EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.route = dict(uid=502, nonce=31, lease=81, prefix='A', cycles=1, actions=3,
                          drag_from=[10, 20], drag_to=[30, 40], editor_click=[100, 200])
        self.client = [dict(event='marker', uid=502, nonce=31, tick=i, elapsed_ms=i * 900)
                       for i in range(1, 4)]
        self.client += [dict(event='action_sent', uid=502, index=i) for i in range(1, 4)]
        self.client += [dict(event='check_complete', uid=502, result='PASS', elapsed_ms=3000,
                             unique_ticks=3, actions=3)]
        common = dict(uid=502, gui_uid=502, audit_session_id=21, nonce=31, boot_id='test-boot')
        self.agent = [dict(common, kind='bridge_connected', lease=81, uptime_ms=100)]
        for at, kind, xy in [(120, 1, (10, 20)), (130, 6, (30, 40)), (140, 2, (30, 40))]:
            self.agent.append(dict(common, kind='input_received', event_type=kind, x=xy[0], y=xy[1],
                                   tag='0000001f00000003', uptime_ms=at))
        self.agent.append(dict(common, kind='bridge_disconnected', lease=81, uptime_ms=3200))
        self.editor = [dict(common, kind='event', event_type=1, x=100, y=200,
                            tag='0000001f00000007', uptime_ms=150),
                       dict(common, kind='document', text='A-000-中文', uptime_ms=160)]

    def check(self):
        return m3.check_route(self.route, self.client, self.agent, self.editor, 3)

    def test_complete_received_evidence(self):
        self.assertEqual(self.check()['received_drags'], 1)

    def test_foreign_pixels_fail_even_if_client_claims_pass(self):
        self.client[1]['uid'] = 503
        with self.assertRaisesRegex(ValueError, 'foreign pixels'):
            self.check()

    def test_foreign_input_tag_fails(self):
        self.editor[0]['tag'] = '0000002f00000007'
        with self.assertRaisesRegex(ValueError, 'foreign input'):
            self.check()

    def test_sent_drag_without_received_drag_fails(self):
        self.agent = [r for r in self.agent if r.get('event_type') != 6]
        with self.assertRaisesRegex(ValueError, 'complete drags'):
            self.check()

    def test_untagged_local_events_cannot_prove_remote_delivery(self):
        self.agent[2]['tag'] = '0'
        with self.assertRaisesRegex(ValueError, 'complete drags'):
            self.check()
        self.setUp()
        self.editor[0]['tag'] = '0'
        with self.assertRaisesRegex(ValueError, 'editor click'):
            self.check()

    def test_transient_foreign_text_fails_even_if_final_document_is_correct(self):
        self.editor.insert(1, dict(self.editor[-1], text='B-000-中文', uptime_ms=155))
        with self.assertRaisesRegex(ValueError, 'workload appeared'):
            m3.check_route(self.route, self.client, self.agent, self.editor, 3, ['B'])

    def test_incomplete_client_is_not_pass(self):
        self.client.pop()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.check()

    def test_missing_text_cycle_fails(self):
        self.editor[-1]['text'] = 'A-000-'
        with self.assertRaisesRegex(ValueError, 'missing received text'):
            self.check()

    def test_replayed_or_stale_pixels_fail(self):
        self.client[1]['tick'] = self.client[0]['tick']
        with self.assertRaisesRegex(ValueError, 'old tick'):
            self.check()
        self.setUp()
        self.client[1]['elapsed_ms'] = 10000
        with self.assertRaisesRegex(ValueError, 'stale or unordered'):
            self.check()

    def test_old_lease_events_cannot_fill_missing_current_drag(self):
        old = copy.deepcopy(self.agent[2])
        old['uptime_ms'] = 99
        self.agent[2] = old
        with self.assertRaisesRegex(ValueError, 'complete drags'):
            self.check()

    def test_two_successful_sequential_runs_are_not_concurrent(self):
        first = self.check()
        second = dict(first, start_ms=3201, end_ms=6301)
        with self.assertRaisesRegex(ValueError, 'simultaneous'):
            m3.overlap([first, second], 3)

    def test_different_server_boots_are_not_concurrent(self):
        first = self.check()
        with self.assertRaisesRegex(ValueError, 'different server boots'):
            m3.overlap([first, dict(first, boot_id='another-boot')], 3)


if __name__ == '__main__':
    unittest.main()
