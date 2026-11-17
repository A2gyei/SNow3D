"""Check scientific data orientation, quantization and time-series integrity."""
import json
from pathlib import Path
import unittest
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


class FrameDataTests(unittest.TestCase):
    def test_all_published_runs_are_complete_and_finite(self):
        entries = json.loads((ROOT / 'data/public/index.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(entries), 2)
        for entry in entries:
            with self.subTest(run=entry['id']):
                base = ROOT / 'data/public' / entry['id']
                m = json.loads((base / 'manifest.json').read_text(encoding='utf-8'))
                # The rendered mesh may be finer than the frame textures.
                mesh_width = m.get('meshWidth', m['width'])
                mesh_height = m.get('meshHeight', m['height'])
                n_mesh = mesh_width * mesh_height
                n = m['width'] * m['height']
                terrain = np.fromfile(base / 'terrain.bin', dtype='<f4').reshape(n_mesh, 6)
                self.assertTrue(np.isfinite(terrain).all())
                np.testing.assert_allclose(np.linalg.norm(terrain[:, 3:], axis=1), 1, atol=1e-6)
                self.assertEqual((base / 'frames.bin').stat().st_size, n * 4 * len(m['frames']))
                times = [f['t'] for f in m['frames']]
                self.assertEqual(times[0], 0)
                self.assertTrue(all(b > a for a, b in zip(times, times[1:])))
                self.assertEqual(times[-1], m['duration'])
                self.assertGreater(m['peakSpeed'], 0)
                if m.get('terrain') == 'jiangjunshan':
                    # Real terrain: declared georeferenced, shown with a vertical
                    # stretch that must not leak into the packaged elevations.
                    self.assertFalse(m['synthetic'])
                    self.assertTrue(m['georeferenced'])
                    self.assertEqual(m['verticalExaggeration'], 1.5)
                    self.assertEqual(m['crs'], 'EPSG:32645')
                    self.assertIn('zone', m)
                    self.assertIsNotNone(m['zone'])
                else:
                    self.assertTrue(m['synthetic'])
                    self.assertFalse(m['georeferenced'])
                # File row zero is north. This catches a flipped texture/terrain convention.
                grid = terrain.reshape(mesh_height, mesh_width, 6)
                self.assertGreater(grid[0, 0, 1], grid[-1, 0, 1])
                self.assertLess(grid[0, 0, 0], grid[0, -1, 0])

    def test_packed_depth_matches_actual_solver_snapshot(self):
        import sys
        sys.path.insert(0, str(ROOT / 'scripts'))
        from pack import read_grid
        base = ROOT / 'data/public/baseline'
        m = json.loads((base / 'manifest.json').read_text(encoding='utf-8'))
        idx = min(20, len(m['frames']) - 1)
        frame = m['frames'][idx]
        source = ROOT / 'data/runs/baseline/Outputs/com1DFA/peakFiles/timeSteps'
        ft = next(source.glob(f"*_FT_t{frame['t']:.2f}.asc"))
        _, actual = read_grid(ft)
        rows = np.unique(np.append(np.arange(0, actual.shape[0], 3), actual.shape[0] - 1))
        cols = np.unique(np.append(np.arange(0, actual.shape[1], 3), actual.shape[1] - 1))
        rgba = np.memmap(base / 'frames.bin', dtype=np.uint8, mode='r', shape=(len(m['frames']), m['height'], m['width'], 4))[idx]
        decoded = (rgba[:, :, 0].astype(np.uint16) * 256 + rgba[:, :, 1]) / 65535 * m['heightScale']
        np.testing.assert_allclose(decoded, actual[np.ix_(rows, cols)], atol=m['heightScale'] / 65535 / 2 + 1e-6, rtol=0)
        self.assertAlmostEqual(frame['maxDepth'], float(actual.max()), places=5)
        self.assertAlmostEqual(frame['area'], float((actual > .05).sum() * 25), places=2)


class ReleaseZoneTests(unittest.TestCase):
    """The screening must stay a raw-elevation, terrain-only product."""

    def setUp(self):
        self.summary = json.loads(
            (ROOT / 'data/analysis/jiangjunshan/release-zones.json').read_text(encoding='utf-8'))

    def test_candidates_are_ranked_and_bounded(self):
        candidates = self.summary['candidates']
        self.assertGreaterEqual(len(candidates), 1)
        self.assertTrue(self.summary['usesRawElevation'])
        self.assertEqual([item['rank'] for item in candidates], list(range(1, len(candidates) + 1)))
        ranked = [item['rankScore'] for item in candidates]
        self.assertEqual(ranked, sorted(ranked, reverse=True))
        low, high = self.summary['parameters']['slopeRangeDeg']
        for item in candidates:
            with self.subTest(zone=item['id']):
                self.assertEqual(item['id'], f"JJS-R{item['rank']:02d}")
                self.assertGreater(item['areaM2'], 0)
                self.assertGreaterEqual(item['meanSlopeDeg'], low - 1e-6)
                self.assertLessEqual(item['maxSlopeDeg'], high + 1e-6)
                self.assertGreaterEqual(item['maxScore'], item['meanScore'])
                ring = item['ring']
                self.assertGreaterEqual(len(ring), 4)
                self.assertEqual(ring[0], ring[-1], 'release ring must be closed')
                lon, lat, z = item['representativePoint']
                west, south, east, north = self.summary['source']['bounds']
                self.assertTrue(west <= lon <= east and south <= lat <= north)


class SnowMeshTests(unittest.TestCase):
    """The rendered mesh is finer than the solver grid and must stay consistent."""

    def setUp(self):
        self.base = ROOT / 'data/public/jjs_ent_v1'
        self.manifest = json.loads((self.base / 'manifest.json').read_text(encoding='utf-8'))

    def test_mesh_is_finer_than_the_frames(self):
        self.assertGreater(self.manifest['meshWidth'], self.manifest['width'])
        self.assertGreater(self.manifest['meshHeight'], self.manifest['height'])
        self.assertAlmostEqual(self.manifest['meshCellSize'], 15.0, places=3)
        # Frames stay at the solver grid, so the payload does not grow 4x with the mesh.
        self.assertEqual(self.manifest['frameBytes'], self.manifest['width'] * self.manifest['height'] * 4)

    def test_mesh_extent_matches_the_frame_extent(self):
        mesh_width = self.manifest['meshWidth']
        mesh_height = self.manifest['meshHeight']
        terrain = np.fromfile(self.base / 'terrain.bin', dtype='<f4').reshape(mesh_width * mesh_height, 6)
        xs = terrain[:, 0]
        ys = terrain[:, 1]
        span_x = float(xs.max() - xs.min())
        span_y = float(ys.max() - ys.min())
        expected_x = (self.manifest['bounds'][2] - self.manifest['bounds'][0]) * 0.999
        expected_y = (self.manifest['bounds'][3] - self.manifest['bounds'][1]) * 0.999
        self.assertAlmostEqual(span_x / expected_x, 1.0, delta=0.02)
        self.assertAlmostEqual(span_y / expected_y, 1.0, delta=0.02)

    def test_winter_imagery_is_packaged(self):
        self.assertTrue(self.manifest['albedo'])
        albedo = self.base / 'albedo.webp'
        self.assertTrue(albedo.is_file())
        self.assertGreater(albedo.stat().st_size, 1024)


if __name__ == '__main__':
    unittest.main()
