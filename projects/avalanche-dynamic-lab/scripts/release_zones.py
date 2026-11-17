"""Detect potential avalanche release zones from the raw Jiangjunshan terrain.

This is an explainable terrain-susceptibility screen, not an operational
avalanche forecast. It uses only raw elevations (`heights.f32`), never the
display exaggeration: slope, local convexity and relative elevation are
computed in metres on the source grid before anything is drawn.

Score = 0.75 * slope + 0.15 * convexity + 0.10 * relative elevation, with the
slope term peaked at 37 degrees and hard limits (28-50 degrees by default).
Candidates are then ranked by mean score, peak score and area.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from scipy import ndimage

from jiangjunshan import ANALYSIS, ROOT, load_source

DEFAULTS = {
    "threshold": 0.62,
    "minAreaM2": 3000.0,
    "maxCandidates": 8,
    "minSlopeDeg": 28.0,
    "maxSlopeDeg": 50.0,
    "analysisCentreLonLat": [88.158, 47.8319],
    "analysisRadiusM": 4500.0,
    "model": "terrain-susceptibility-v2",
}
ZONES_PATH = ANALYSIS / "release-zones.json"


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def cell_size_metres(bounds, width, height):
    # 1 arc-second GLO-30 cells are not square: longitude shrinks with latitude.
    latitude = math.radians((bounds[1] + bounds[3]) / 2)
    dx = (bounds[2] - bounds[0]) * 111_320.0 * math.cos(latitude) / max(width - 1, 1)
    dy = (bounds[3] - bounds[1]) * 111_132.0 / max(height - 1, 1)
    return dx, dy


def score_terrain(elevation, dx, dy, min_slope, max_slope):
    """Slope / convexity / relative-elevation score on raw elevations."""
    # A one-cell Gaussian suppresses GLO-30 pixel noise before derivatives.
    smooth = ndimage.gaussian_filter(elevation.astype(np.float64), sigma=1.0, mode="nearest")
    dz_dy, dz_dx = np.gradient(smooth, dy, dx)
    slope = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))

    slope_score = np.exp(-0.5 * ((slope - 37.0) / 7.0) ** 2)
    slope_score[(slope < min_slope) | (slope > max_slope)] = 0.0

    regional = ndimage.gaussian_filter(smooth, sigma=5.0, mode="nearest")
    convexity = np.maximum(smooth - regional, 0.0)
    convexity_scale = float(np.percentile(convexity, 95))
    convexity_score = np.clip(convexity / max(convexity_scale, 1e-6), 0.0, 1.0)

    low, high = np.percentile(smooth, (5, 95))
    elevation_score = np.clip((smooth - low) / max(high - low, 1e-6), 0.0, 1.0)

    score = 0.75 * slope_score + 0.15 * convexity_score + 0.10 * elevation_score
    score[(slope < min_slope) | (slope > max_slope)] = 0.0
    return score.astype(np.float32), slope.astype(np.float32), convexity_score.astype(np.float32)


def _analysis_mask(bounds, width, height, centre_lonlat, radius_m):
    west, south, east, north = bounds
    centre_lon, centre_lat = centre_lonlat
    col_lon = west + (np.arange(width) + 0.5) / width * (east - west)
    row_lat = north - (np.arange(height) + 0.5) / height * (north - south)
    x_distance = (col_lon - centre_lon) * 111_320.0 * math.cos(math.radians(centre_lat))
    y_distance = (row_lat - centre_lat) * 111_132.0
    mask = np.hypot(y_distance[:, None], x_distance[None, :]) <= radius_m
    # Derivative edges use incomplete neighbourhoods and create fake steep strips.
    edge_rows = max(2, int(math.ceil(120.0 / max((north - south) * 111_132.0 / height, 1e-6))))
    edge_cols = max(2, int(math.ceil(120.0 / max((east - west) * 111_320.0 / width, 1e-6))))
    mask[:edge_rows, :] = False
    mask[-edge_rows:, :] = False
    mask[:, :edge_cols] = False
    mask[:, -edge_cols:] = False
    return mask


def _largest_ring(geometry):
    """Outer ring of the largest part, so the release polygon stays simple."""
    if geometry["type"] == "Polygon":
        return geometry["coordinates"][0]
    parts = [polygon[0] for polygon in geometry["coordinates"]]
    return max(parts, key=len)


def derive(parameters=None, elevation=None, bounds=None, write=True):
    """Compute candidates and (by default) publish them to data/analysis."""
    params = dict(DEFAULTS)
    if parameters:
        params.update(parameters)

    if elevation is None or bounds is None:
        elevation, _meta, bounds = load_source()
    height, width = elevation.shape
    dx, dy = cell_size_metres(bounds, width, height)
    cell_area = dx * dy
    score, slope, convexity = score_terrain(elevation, dx, dy, params["minSlopeDeg"], params["maxSlopeDeg"])

    analysis_mask = _analysis_mask(bounds, width, height,
                                   params["analysisCentreLonLat"], params["analysisRadiusM"])
    score[~analysis_mask] = 0.0

    mask = (score >= params["threshold"]) & analysis_mask
    structure = np.ones((3, 3), dtype=bool)
    mask = ndimage.binary_opening(mask, structure=structure)
    mask = ndimage.binary_closing(mask, structure=structure, iterations=2)
    mask = ndimage.binary_fill_holes(mask)
    mask &= analysis_mask

    labels, label_count = ndimage.label(mask, structure=structure)
    raw = []
    for label_id in range(1, label_count + 1):
        rows, cols = np.where(labels == label_id)
        area = float(rows.size * cell_area)
        if area < params["minAreaM2"]:
            labels[labels == label_id] = 0
            continue
        values = score[rows, cols]
        peak = int(np.argmax(values))
        peak_row, peak_col = int(rows[peak]), int(cols[peak])
        raw.append({
            "label": label_id,
            "areaM2": area,
            "meanScore": float(values.mean()),
            "maxScore": float(values.max()),
            "meanSlopeDeg": float(slope[rows, cols].mean()),
            "maxSlopeDeg": float(slope[rows, cols].max()),
            "centroidRowCol": [float(rows.mean()), float(cols.mean())],
            "peakRowCol": [peak_row, peak_col],
        })
    if raw:
        largest = max(item["areaM2"] for item in raw)
        for item in raw:
            item["rankScore"] = (0.55 * item["meanScore"] + 0.30 * item["maxScore"]
                                 + 0.15 * math.sqrt(item["areaM2"] / largest))
        raw.sort(key=lambda item: item["rankScore"], reverse=True)
    selected = raw[: params["maxCandidates"]]
    final_mask = np.isin(labels, [item["label"] for item in selected])

    # Polygonise in lon/lat so results stay independent of the display grid.
    from rasterio.features import shapes
    from rasterio.transform import from_bounds
    transform = from_bounds(*bounds, width=width, height=height)
    geometries = {}
    for geometry, value in shapes(labels.astype(np.int32), mask=final_mask, transform=transform):
        label_id = int(value)
        if label_id not in geometries:
            geometries[label_id] = geometry

    def sample_height(lon, lat):
        from jiangjunshan import sample_bilinear
        return float(sample_bilinear(elevation, bounds, np.array([lon]), np.array([lat]))[0])

    west, south, east, north = bounds
    candidates = []
    for rank, item in enumerate(selected, start=1):
        geometry = geometries.get(item["label"])
        if geometry is None:
            continue
        ring = [[float(x), float(y), round(sample_height(x, y), 2)] for x, y, *_ in _largest_ring(geometry)]
        peak_row, peak_col = item["peakRowCol"]
        lon = west + (peak_col + 0.5) / width * (east - west)
        lat = north - (peak_row + 0.5) / height * (north - south)
        candidates.append({
            "id": f"JJS-R{rank:02d}",
            "rank": rank,
            "areaM2": round(item["areaM2"], 1),
            "meanScore": round(item["meanScore"], 4),
            "maxScore": round(item["maxScore"], 4),
            "meanSlopeDeg": round(item["meanSlopeDeg"], 2),
            "maxSlopeDeg": round(item["maxSlopeDeg"], 2),
            "rankScore": round(item["rankScore"], 4),
            "representativePoint": [round(lon, 6), round(lat, 6), round(sample_height(lon, lat), 2)],
            "ring": ring,
        })

    summary = {
        "model": params["model"],
        "warning": "仅基于地形(坡度/凸度/相对高程)的静态敏感性筛查,未叠加积雪、风、温度、植被与现场观测,不构成预警。",
        "usesRawElevation": True,
        "source": {
            "terrain": "data/source/jiangjunshan/heights.f32",
            "dem": "Copernicus DEM GLO-30 (EGM2008 -> WGS84 ellipsoid)",
            "width": width, "height": height,
            "bounds": [float(value) for value in bounds],
            "cellSizeM": [round(dx, 4), round(dy, 4)],
            "analysisCentreLonLat": params["analysisCentreLonLat"],
            "analysisRadiusM": params["analysisRadiusM"],
        },
        "parameters": {
            "threshold": params["threshold"],
            "slopeRangeDeg": [params["minSlopeDeg"], params["maxSlopeDeg"]],
            "minimumAreaM2": params["minAreaM2"],
            "maximumCandidates": params["maxCandidates"],
            "weights": {"slope": 0.75, "convexity": 0.15, "relativeElevation": 0.10},
        },
        "candidateCount": len(candidates),
        "candidates": candidates,
    }

    if write:
        ANALYSIS.mkdir(parents=True, exist_ok=True)
        score.astype("<f4").tofile(ANALYSIS / "terrain-risk.f32")
        slope.astype("<f4").tofile(ANALYSIS / "slope-degrees.f32")
        convexity.astype("<f4").tofile(ANALYSIS / "convexity.f32")
        final_mask.astype(np.uint8).tofile(ANALYSIS / "high-risk-mask.u8")
        _write_json(ZONES_PATH, summary)
    return summary


def ensure(path=ZONES_PATH):
    """Load published candidates, recomputing them when missing or unusable."""
    path = Path(path)
    if path.exists():
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            if summary.get("candidates"):
                return summary
        except (OSError, ValueError):
            pass
    return derive()


def by_id(zone_id, path=ZONES_PATH):
    for candidate in ensure(path)["candidates"]:
        if candidate["id"] == zone_id:
            return candidate
    raise KeyError(f"未识别的起崩区: {zone_id}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Detect release zones from the Jiangjunshan terrain.")
    parser.add_argument("--threshold", type=float, default=DEFAULTS["threshold"])
    parser.add_argument("--min-area", type=float, default=DEFAULTS["minAreaM2"])
    parser.add_argument("--max-candidates", type=int, default=DEFAULTS["maxCandidates"])
    parser.add_argument("--min-slope", type=float, default=DEFAULTS["minSlopeDeg"])
    parser.add_argument("--max-slope", type=float, default=DEFAULTS["maxSlopeDeg"])
    parser.add_argument("--centre-lon", type=float, default=DEFAULTS["analysisCentreLonLat"][0])
    parser.add_argument("--centre-lat", type=float, default=DEFAULTS["analysisCentreLonLat"][1])
    parser.add_argument("--radius", type=float, default=DEFAULTS["analysisRadiusM"])
    args = parser.parse_args()
    result = derive({
        "threshold": args.threshold,
        "minAreaM2": args.min_area,
        "maxCandidates": args.max_candidates,
        "minSlopeDeg": args.min_slope,
        "maxSlopeDeg": args.max_slope,
        "analysisCentreLonLat": [args.centre_lon, args.centre_lat],
        "analysisRadiusM": args.radius,
    })
    print(json.dumps({
        "candidateCount": result["candidateCount"],
        "output": str(ZONES_PATH),
        "top": result["candidates"][0] if result["candidates"] else None,
    }, ensure_ascii=False, indent=2)[:1200])
