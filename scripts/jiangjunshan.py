"""Jiangjunshan terrain pipeline: raw GLO-30 heights to a metric simulation grid.

The source crop (`data/source/jiangjunshan`) stores 1 arc-second elevations in
`heights.f32` with its lon/lat envelope in `terrain.json`. Every analysis and
every simulation must use these raw elevations; vertical exaggeration is a
display-only transform applied in the browser shader.

`heights.f32` rows run north to south and hold WGS84 ellipsoidal heights.
The simulation grid resamples them onto a square metric grid in UTM zone 45N,
because com1DFA needs equidistant metre cells. Resampling is bilinear and never
rescales elevations.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "source" / "jiangjunshan"
ANALYSIS = ROOT / "data" / "analysis" / "jiangjunshan"
# Jiangjunshan spans 88.06-88.25 E, inside UTM zone 45N (84-90 E).
CRS = "EPSG:32645"
DEFAULT_CELL = 30.0


def load_source(source=SOURCE):
    """Read the raw elevation crop exactly as published (north-to-south rows)."""
    source = Path(source)
    meta = json.loads((source / "terrain.json").read_text(encoding="utf-8"))
    width, height = int(meta["width"]), int(meta["height"])
    elevation = np.fromfile(source / "heights.f32", dtype="<f4")
    if elevation.size != width * height:
        raise ValueError(f"heights.f32 holds {elevation.size} cells; terrain.json requires {width * height}")
    elevation = elevation.reshape((height, width))
    if not np.isfinite(elevation).all():
        raise ValueError("heights.f32 contains NaN or infinite elevations")
    bounds = [float(value) for value in meta["bounds"]]
    return elevation, meta, bounds


def cell_centres(bounds, width, height):
    """Cell-centre lon/lat vectors in the file's own north-to-south order."""
    west, south, east, north = bounds
    lon = west + (np.arange(width) + 0.5) / width * (east - west)
    lat = north - (np.arange(height) + 0.5) / height * (north - south)
    return lon, lat


def sample_bilinear(elevation, bounds, lon, lat):
    """Bilinearly sample the raw crop at arbitrary lon/lat (no vertical scaling)."""
    west, south, east, north = bounds
    rows, cols = elevation.shape
    col = (np.asarray(lon, dtype=np.float64) - west) / (east - west) * cols - 0.5
    row = (north - np.asarray(lat, dtype=np.float64)) / (north - south) * rows - 0.5
    col = np.clip(col, 0, cols - 1.000001)
    row = np.clip(row, 0, rows - 1.000001)
    c0 = np.floor(col).astype(int)
    r0 = np.floor(row).astype(int)
    c1 = np.minimum(c0 + 1, cols - 1)
    r1 = np.minimum(r0 + 1, rows - 1)
    u, v = col - c0, row - r0
    top = elevation[r0, c0] * (1 - u) + elevation[r0, c1] * u
    bottom = elevation[r1, c0] * (1 - u) + elevation[r1, c1] * u
    return top * (1 - v) + bottom * v


def build_metric(elevation, bounds, cell=DEFAULT_CELL, crs=CRS):
    """Resample the crop onto a square metric grid covering the same ground.

    Returns a dict with:
        grid        float32 array, rows north to south, metres
        header      ESRI ASCII header values (cell centres)
        transform   pyproj transformers for lon/lat <-> grid CRS
        lonlat      lon/lat of the grid centre
    """
    from pyproj import Transformer

    to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    west, south, east, north = bounds
    # Sample the crop border so the grid covers every cell despite meridian
    # convergence; a plain corner check would clip the eastern edge.
    steps = np.linspace(0.0, 1.0, 129)
    edge_lon = np.concatenate([
        west + steps * (east - west),
        west + steps * (east - west),
        np.full_like(steps, west),
        np.full_like(steps, east),
    ])
    edge_lat = np.concatenate([
        np.full_like(steps, south),
        np.full_like(steps, north),
        south + steps * (north - south),
        south + steps * (north - south),
    ])
    edge_x, edge_y = to_crs.transform(edge_lon, edge_lat)
    x_min, x_max = float(np.min(edge_x)), float(np.max(edge_x))
    y_min, y_max = float(np.min(edge_y)), float(np.max(edge_y))

    ncols = int(math.floor((x_max - x_min) / cell))
    nrows = int(math.floor((y_max - y_min) / cell))
    if ncols < 8 or nrows < 8:
        raise ValueError("Metric grid is too small for the requested cell size")
    xs = x_min + (np.arange(ncols) + 0.5) * cell
    ys = y_min + (np.arange(nrows) + 0.5) * cell
    mesh_x, mesh_y = np.meshgrid(xs, ys)
    lon, lat = to_wgs.transform(mesh_x, mesh_y)
    # Rows must run north to south to match the ESRI ASCII and browser layout.
    grid = sample_bilinear(elevation, bounds, lon, lat)[::-1].astype(np.float32)
    if not np.isfinite(grid).all():
        raise ValueError("Resampled metric grid contains non-finite elevations")

    header = {
        "ncols": ncols,
        "nRows": nrows,
        "xllcenter": float(xs[0]),
        "yllcenter": float(ys[0]),
        "cellsize": float(cell),
        "nodata_value": -9999.0,
    }
    centre_x = float(xs[0] + (ncols - 1) * cell / 2)
    centre_y = float(ys[0] + (nrows - 1) * cell / 2)
    centre_lon, centre_lat = to_wgs.transform(centre_x, centre_y)
    return {
        "grid": grid,
        "header": header,
        "crs": crs,
        "cellSize": float(cell),
        "to_crs": to_crs,
        "to_wgs": to_wgs,
        "utm_bounds": [float(xs[0] - cell / 2), float(ys[0] - cell / 2),
                       float(xs[-1] + cell / 2), float(ys[-1] + cell / 2)],
        "centreXY": [centre_x, centre_y],
        "centreLonLat": [float(centre_lon), float(centre_lat)],
        "sourceBounds": [float(value) for value in bounds],
    }


def write_ascii(path, grid, header):
    """Write an ESRI ASCII grid with exactly the six header lines pack.py reads."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"ncols {header['ncols']}",
        f"nRows {header['nRows']}",
        f"xllcenter {header['xllcenter']:.6f}",
        f"yllcenter {header['yllcenter']:.6f}",
        f"cellsize {header['cellsize']:.6f}",
        f"nodata_value {header['nodata_value']:.2f}",
    ]
    body = "\n".join(" ".join(f"{value:.3f}" for value in row) for row in grid)
    path.write_text("\n".join(lines) + "\n" + body + "\n", encoding="ascii")


def lonlat_to_xy(metric, lon, lat):
    """Project lon/lat onto the simulation grid's metric CRS."""
    x, y = metric["to_crs"].transform(np.asarray(lon, dtype=np.float64),
                                      np.asarray(lat, dtype=np.float64))
    return np.asarray(x), np.asarray(y)


def xy_to_lonlat(metric, x, y):
    lon, lat = metric["to_wgs"].transform(np.asarray(x, dtype=np.float64),
                                          np.asarray(y, dtype=np.float64))
    return np.asarray(lon), np.asarray(lat)


def grid_indices(metric, x, y):
    """Column/row indices of metric coordinates, clipped to the grid."""
    header = metric["header"]
    col = np.rint((np.asarray(x) - header["xllcenter"]) / header["cellsize"]).astype(int)
    row = np.rint((header["yllcenter"] + (header["nRows"] - 1) * header["cellsize"] - np.asarray(y))
                  / header["cellsize"]).astype(int)
    return (np.clip(col, 0, header["ncols"] - 1), np.clip(row, 0, header["nRows"] - 1))
