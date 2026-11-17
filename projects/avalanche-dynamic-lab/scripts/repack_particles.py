"""Rebuild the current public particle cloud without rerunning AvaFrame.

This migration reads the arrival/peak-speed state from an existing snow.bin,
then emits the new uniform, terrain-fitted particle pool.  Future simulations
use pack.py directly and do not need this script.
"""

import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from pack import PARTICLE_LAYOUT, PARTICLE_STRIDE, _build_particles, json_save


def main(run_id="jiangjunshan_r01"):
    public = ROOT / "data" / "public" / run_id
    manifest_path = public / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows, cols = manifest["height"], manifest["width"]
    terrain = np.fromfile(public / "terrain.bin", dtype="<f4").reshape(rows, cols, 6)
    old_stride = manifest["particles"]["stride"]
    old = np.fromfile(public / "snow.bin", dtype="<f4").reshape(-1, old_stride)

    arrival = np.full((rows, cols), -1.0, dtype=np.float64)
    peak_speed = np.zeros((rows, cols), dtype=np.float32)
    old_col = np.clip(np.floor(old[:, 6] * cols).astype(np.int64), 0, cols - 1)
    old_row = np.clip(np.floor(old[:, 7] * rows).astype(np.int64), 0, rows - 1)
    for row, col, reached, speed in zip(old_row, old_col, old[:, 8], old[:, 9]):
        if reached >= 0 and (arrival[row, col] < 0 or reached < arrival[row, col]):
            arrival[row, col] = reached
        peak_speed[row, col] = max(peak_speed[row, col], speed)

    cell = float(manifest["nativeCellSize"])
    stride = int(manifest["displayStride"])
    row_ids = np.arange(rows, dtype=np.int64) * stride
    col_ids = np.arange(cols, dtype=np.int64) * stride
    # The final sampled row/column can be one native cell closer than `stride`;
    # the display-grid reconstruction still preserves its exact position.
    display_cell = cell * stride
    display_x0 = float(manifest["bounds"][0])
    display_y0 = float(manifest["bounds"][1])
    x0 = display_x0
    y0 = display_y0
    center = manifest["origin"]

    # _build_particles expects a north-to-south DEM with native-grid spacing.
    # Treat the displayed terrain itself as the native grid for this one-time
    # migration, then use a unit row/column stride.
    particles = _build_particles(
        terrain[:, :, 2] + center[2], terrain[:, :, 3:6],
        np.arange(rows), np.arange(cols), arrival, peak_speed, center,
        display_cell, display_x0, display_y0, seed=20261117,
    )
    particles.tofile(public / "snow.bin")
    manifest["particles"] = {
        "count": int(particles.shape[0]),
        "stride": PARTICLE_STRIDE,
        "layout": PARTICLE_LAYOUT,
        "bytes": int(particles.nbytes),
        "url": f"/data/{run_id}/snow.bin",
        "distribution": "uniform-r2",
        "perDisplayCell": int(particles.shape[0] // (rows * cols)),
        "surfaceFitted": True,
    }
    json_save(manifest_path, manifest)
    print(f"{particles.shape[0]} particles, {particles.nbytes / 1024 / 1024:.1f} MiB")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "jiangjunshan_r01")
