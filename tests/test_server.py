"""Exercise local API validation without requiring a network connection."""
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


class MemoryHandler(server.Handler):
    def __init__(self, path, body=None, origin=None):
        self.path = path
        raw = json.dumps(body).encode() if body is not None else b''
        self.rfile = BytesIO(raw)
        self.headers = {'Content-Length': str(len(raw)), 'Host': '127.0.0.1:8765'}
        if origin:
            self.headers['Origin'] = origin
        self.result = None

    def json_response(self, data, status=200):
        self.result = (status, data)


class LocalApiTests(unittest.TestCase):
    def test_scenarios_match_published_data(self):
        handler = MemoryHandler('/api/scenarios')
        handler.do_GET()
        status, data = handler.result
        self.assertEqual(status, 200)
        self.assertIn('baseline', [item['id'] for item in data])
        self.assertIn('resistance', [item['id'] for item in data])

    def test_zones_endpoint_publishes_the_screening(self):
        handler = MemoryHandler('/api/zones')
        handler.do_GET()
        status, data = handler.result
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data['candidates']), 1)
        self.assertTrue(data['usesRawElevation'])
        self.assertIn('warning', data)

    def test_invalid_parameters_never_start_a_process(self):
        with patch('server.subprocess.run') as run:
            for params in ({'thickness': float('nan')}, {'thickness': 0}, {'thickness': 4},
                           {'thickness': 'abc'}, {'terrain': 'mars'}, {'zone': '../../bad'},
                           {'terrain': 'parabola', 'scenario': '../../bad'}):
                with self.subTest(params=params):
                    handler = MemoryHandler('/api/run', params)
                    handler.do_POST()
                    self.assertEqual(handler.result[0], 400)
            run.assert_not_called()

    def test_missing_zone_falls_back_to_the_highest_ranked_candidate(self):
        captured = {}

        class FakeThread:
            def __init__(self, target=None, daemon=None):
                self.target = target

            def start(self):
                captured['thread_started'] = True

        def fake_command(job_id, terrain, zone, scenario, thickness):
            captured['command'] = (terrain, zone, thickness)
            return [sys.executable, 'scripts/simulate.py']

        with patch('server.simulate_command', side_effect=fake_command), \
                patch('server.threading.Thread', FakeThread):
            handler = MemoryHandler('/api/run', {'thickness': 1.2, 'terrain': 'jiangjunshan'})
            handler.do_POST()
        self.assertEqual(handler.result[0], 202)
        self.assertTrue(captured.get('thread_started'))
        terrain, zone, thickness = captured['command']
        self.assertEqual(terrain, 'jiangjunshan')
        self.assertEqual(thickness, 1.2)
        published = server.load_zones()['candidates']
        self.assertEqual(zone, published[0]['id'])

    def test_command_carries_terrain_and_thickness(self):
        command = server.simulate_command('run_x', 'jiangjunshan', 'JJS-R03', 'null', 1.5)
        self.assertEqual(command[command.index('--terrain') + 1], 'jiangjunshan')
        self.assertEqual(command[command.index('--zone') + 1], 'JJS-R03')
        self.assertEqual(command[command.index('--thickness') + 1], '1.5')
        legacy = server.simulate_command('run_y', 'parabola', None, 'res', 1.0)
        self.assertEqual(legacy[legacy.index('--scenario') + 1], 'res')
        self.assertNotIn('--zone', legacy)

    def test_cross_origin_submission_is_rejected(self):
        handler = MemoryHandler('/api/run', {'thickness': 1}, origin='https://other.example')
        handler.do_POST()
        self.assertEqual(handler.result[0], 403)

    def test_job_path_traversal_is_rejected(self):
        handler = MemoryHandler('/api/jobs/..')
        handler.do_GET()
        self.assertEqual(handler.result[0], 400)

    def test_static_paths_cannot_escape_allowed_folders(self):
        handler = MemoryHandler('/')
        forbidden = handler.translate_path('/data/../../server.py')
        self.assertEqual(Path(forbidden).name, '_not_found_')
        self.assertEqual(Path(handler.translate_path('/')).name, 'index.html')


if __name__ == '__main__':
    unittest.main()
