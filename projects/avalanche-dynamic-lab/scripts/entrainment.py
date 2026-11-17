"""Derive an entrainment polygon from an existing com1DFA run.

AvaFrame expects the entrainment area as a polygon in `Inputs/ENT/` with a
thickness attribute. Rather than drawing it by hand, this module uses the flow
footprint of a finished run: every cell the avalanche actually wetted, minus the
release area, becomes the corridor that can be eroded in a follow-up `ent` run.

Only the geometry is data-driven; the erodible depth still has to be chosen by
the user, because GLO-30 carries no snow-cover information.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import shapefile
from scipy import ndimage

AREA_THRESHOLD = 0.05


def read_header(path):
    with Path(path).open() as handle:
        return {key.lower(): float(value) for key, value in (next(handle).split() for _ in range(6))}


def flow_footprint(run_dir, area_threshold=AREA_THRESHOLD):
    """Boolean mask of every cell the run wetted, plus per-cell arrival time."""
    run_dir = Path(run_dir)
    frame_dir = run_dir / "Outputs" / "com1DFA" / "peakFiles" / "timeSteps"
    frames = sorted(frame_dir.glob("*_FT_t*.asc"), key=lambda p: float(p.stem.rsplit("_t", 1)[1]))
    if not frames:
        raise FileNotFoundError(f"No FT time steps in {frame_dir}")
    header = read_header(next((run_dir / "Inputs").glob("*.asc")))
    wet = None
    arrival = None
    for frame in frames:
        timestamp = float(frame.stem.rsplit("_t", 1)[1])
        h = np.loadtxt(frame, skiprows=6, dtype=np.float32)
        if wet is None:
            wet = np.zeros(h.shape, dtype=bool)
            arrival = np.full(h.shape, np.nan, dtype=np.float64)
        touched = h > area_threshold
        arrival[touched & ~np.isfinite(arrival)] = timestamp
        wet |= touched
    return header, wet, arrival


def _mask_to_ring(mask, header):
    """Largest exact cell-edge exterior ring of a mask, in the DEM's own CRS."""
    from rasterio.features import shapes
    from rasterio.transform import from_origin
    from shapely.geometry import Polygon

    cell = header["cellsize"]
    x0 = header.get("xllcenter", header.get("xllcorner", 0) + cell / 2)
    y0 = header.get("yllcenter", header.get("yllcorner", 0) + cell / 2)
    nrows = int(header["nrows"])
    filled = ndimage.binary_fill_holes(mask)
    transform = from_origin(x0 - cell / 2, y0 + (nrows - 0.5) * cell, cell, cell)
    best, best_area = None, 0.0
    for geometry, value in shapes(filled.astype(np.uint8), mask=filled, transform=transform):
        if value != 1:
            continue
        rings = ([geometry["coordinates"][0]] if geometry["type"] == "Polygon"
                 else [polygon[0] for polygon in geometry["coordinates"]])
        for ring in rings:
            polygon = Polygon(ring)
            if polygon.is_valid and polygon.area > best_area:
                best, best_area = [[float(x), float(y)] for x, y in polygon.exterior.coords], polygon.area
    return best


def write_entrainment(path, rings, thickness, name="corridor"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = shapefile.Writer(str(path), shapeType=shapefile.POLYGON)
    writer.field("ID", "C", 40)
    writer.field("Name", "C", 40)
    writer.field("thickness", "N", 11, 3)
    for index, ring in enumerate(rings, start=1):
        writer.poly([ring])
        writer.record(str(index), name, round(float(thickness), 3))
    writer.close()
    return len(rings)


def build_corridor(run_dir, target_dir, thickness=0.3, min_area_ha=0.2, max_features=4,
                   release_shape=None, report_path=None):
    """Write `Inputs/ENT/corridor.shp` for a follow-up entrainment run.

    `release_shape` (a shapefile path) is excluded from the corridor so the
    primary release mass is not counted twice.
    """
    run_dir = Path(run_dir)
    header, wet, arrival = flow_footprint(run_dir)
    cell = header["cellsize"]
    footprint = wet.copy()
    if release_shape:
        release = np.zeros(wet.shape, dtype=bool)
        for shape in shapefile.Reader(str(release_shape)).shapes():
            for x, y in shape.points:
                col = int(round((x - header.get("xllcenter", 0)) / cell))
                row = int(header["nrows"] - 1 - round((y - header.get("yllcenter", 0)) / cell))
                if 0 <= row < wet.shape[0] and 0 <= col < wet.shape[1]:
                    release[row, col] = True
        # Grow the release a little so rasterised cracks do not leak into the corridor.
        release = ndimage.binary_dilation(release, structure=np.ones((3, 3)), iterations=1)
        footprint &= ~release

    labels, count = ndimage.label(footprint, structure=np.ones((3, 3)))
    parts = []
    for label_id in range(1, count + 1):
        mask = labels == label_id
        area_ha = float(mask.sum()) * cell * cell / 1e4
        if area_ha >= min_area_ha:
            parts.append((area_ha, mask))
    parts.sort(key=lambda item: item[0], reverse=True)
    rings = [ring for _, mask in parts[:max_features] for ring in [_mask_to_ring(mask, header)] if ring]
    written = write_entrainment(Path(target_dir) / "Inputs" / "ENT" / "corridor.shp", rings, thickness)

    summary = {
        "sourceRun": run_dir.name,
        "cellSize": cell,
        "corridorCells": int(footprint.sum()),
        "corridorHa": round(float(footprint.sum()) * cell * cell / 1e4, 2),
        "features": written,
        "thickness": thickness,
        "arrivalMedian": float(np.nanmedian(arrival[footprint])) if footprint.any() else None,
    }
    if report_path:
        Path(report_path).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="finished run directory, e.g. data/runs/jiangjunshan_r01")
    parser.add_argument("--target", required=True, help="run directory that should receive Inputs/ENT")
    parser.add_argument("--thickness", type=float, default=0.3)
    parser.add_argument("--min-area", type=float, default=0.2)
    parser.add_argument("--max-features", type=int, default=4)
    args = parser.parse_args()
    result = build_corridor(Path(args.run), Path(args.target), args.thickness,
                            args.min_area, args.max_features)
    print(json.dumps(result, ensure_ascii=False, indent=2))
