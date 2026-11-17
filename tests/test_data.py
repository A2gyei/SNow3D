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
                n = m['width'] * m['height']
                terrain = np.fromfile(base / 'terrain.bin', dtype='<f4').reshape(n, 6)
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
                grid = terrain.reshape(m['height'], m['width'], 6)
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


class ParticleCloudTests(unittest.TestCase):
    """The snow cloud must live in the same local frame as the terrain mesh."""

    def setUp(self):
        self.base = ROOT / 'data/public/jiangjunshan_r01'
        self.manifest = json.loads((self.base / 'manifest.json').read_text(encoding='utf-8'))

    def test_cloud_shape_and_extent(self):
        info = self.manifest['particles']
        self.assertGreater(info['count'], 1000)
        raw = np.fromfile(self.base / 'snow.bin', dtype='<f4')
        self.assertEqual(raw.size, info['count'] * info['stride'])
        cloud = raw.reshape(-1, info['stride'])
        self.assertTrue(np.isfinite(cloud).all())
        n = self.manifest['width'] * self.manifest['height']
        terrain = np.fromfile(self.base / 'terrain.bin', dtype='<f4').reshape(n, 6)
        # Same local origin as the mesh: leaving absolute UTM coordinates in the
        # cloud would blow these bounds by two orders of magnitude.
        self.assertLess(np.abs(cloud[:, 0]).max(), np.abs(terrain[:, 0]).max() + 200)
        self.assertLess(np.abs(cloud[:, 1]).max(), np.abs(terrain[:, 1]).max() + 200)
        self.assertGreaterEqual(cloud[:, 2].min(), -1)
        self.assertLessEqual(cloud[:, 2].max(), self.manifest['heightMax'] + 1)

    def test_flow_arrival_marks_the_cascade(self):
        info = self.manifest['particles']
        cloud = np.fromfile(self.base / 'snow.bin', dtype='<f4').reshape(-1, info['stride'])
        arrival = cloud[:, 8]
        touched = arrival[arrival >= 0]
        self.assertGreater(touched.size, 0)
        self.assertGreaterEqual(touched.min(), 0)
        self.assertLessEqual(touched.max(), self.manifest['duration'])
        # Only flow cells get the dense body cloud; the untouched mountain keeps the
        # thin base sprinkle. Count cells, not particles, since the body is dense.
        cells = np.unique(np.stack([cloud[:, 6], cloud[:, 7]], axis=1), axis=0)
        flow_cells = np.unique(np.stack([cloud[arrival >= 0, 6], cloud[arrival >= 0, 7]], axis=1), axis=0)
        self.assertLess(flow_cells.shape[0], cells.shape[0] * 0.2)

    def test_winter_imagery_is_packaged(self):
        self.assertTrue(self.manifest['albedo'])
        albedo = self.base / 'albedo.webp'
        self.assertTrue(albedo.is_file())
        self.assertGreater(albedo.stat().st_size, 1024)


if __name__ == '__main__':
    unittest.main()
