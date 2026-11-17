"""Check scientific data orientation, quantization and time-series integrity."""
import json
from pathlib import Path
import unittest
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


class FrameDataTests(unittest.TestCase):
    def test_all_published_runs_are_complete_and_finite(self):
        entries = json.loads((ROOT / 'data/public/index.json').read_text(encoding='utf-8'))
        self.assertEqual([entry['id'] for entry in entries], ['jiangjunshan_r01'])
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

    def test_packed_r01_is_the_entrainment_result(self):
        base = ROOT / 'data/public/jiangjunshan_r01'
        m = json.loads((base / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(m['scenario'], 'ent')
        self.assertEqual(m['simName'], 'JJS-R01_36a9b51c77_com1_C_L_ent_dfa')
        self.assertAlmostEqual(m['snowpack']['entrainableDepthM'], 0.35)
        self.assertGreater(m['mass']['entrainedMassKg'], 0)
        self.assertGreater(m['mass']['massGainPercent'], 0)
        rgba = np.memmap(base / 'frames.bin', dtype=np.uint8, mode='r',
                         shape=(len(m['frames']), m['height'], m['width'], 4))
        decoded_depth = (rgba[:, :, :, 0].astype(np.uint16) * 256 + rgba[:, :, :, 1]) / 65535 * m['heightScale']
        self.assertTrue(np.isfinite(decoded_depth).all())
        self.assertLessEqual(float(decoded_depth.max()), m['heightScale'] + 1e-6)


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

    def test_cloud_is_one_uniform_surface_fitted_pool(self):
        info = self.manifest['particles']
        self.assertEqual(info['distribution'], 'uniform-r2')
        self.assertTrue(info['surfaceFitted'])
        self.assertEqual(info['count'], self.manifest['width'] * self.manifest['height']
                         * info['perDisplayCell'])
        cloud = np.fromfile(self.base / 'snow.bin', dtype='<f4').reshape(-1, info['stride'])
        normal_length = np.linalg.norm(cloud[:, 3:6], axis=1)
        np.testing.assert_allclose(normal_length, 1.0, rtol=0, atol=2e-5)

        rows, cols = self.manifest['height'], self.manifest['width']
        terrain = np.fromfile(self.base / 'terrain.bin', dtype='<f4').reshape(rows, cols, 6)
        dx = float(terrain[0, 1, 0] - terrain[0, 0, 0])
        dy = float(terrain[0, 0, 1] - terrain[1, 0, 1])
        col_f = np.clip((cloud[:, 0] - terrain[0, 0, 0]) / dx, 0, cols - 1)
        row_f = np.clip((terrain[0, 0, 1] - cloud[:, 1]) / dy, 0, rows - 1)
        r0 = np.floor(row_f).astype(int)
        c0 = np.floor(col_f).astype(int)
        r1 = np.minimum(r0 + 1, rows - 1)
        c1 = np.minimum(c0 + 1, cols - 1)
        fr, fc = row_f - r0, col_f - c0
        expected_z = ((1-fr)*(1-fc)*terrain[r0, c0, 2] + (1-fr)*fc*terrain[r0, c1, 2]
                      + fr*(1-fc)*terrain[r1, c0, 2] + fr*fc*terrain[r1, c1, 2])
        np.testing.assert_allclose(cloud[:, 2], expected_z, rtol=0, atol=2e-3)

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
