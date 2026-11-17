"""Convert AvaFrame ASC time steps to compact, documented browser data.

Raster rows retain their north-to-south file order. Heights and speeds are
quantized independently to uint16, stored in RG and BA of each RGBA pixel.
The terrain grid and snow textures use the same sampled raster cells.

Elevations are packaged **exactly as the solver used them**. Vertical
exaggeration is a display transform applied in the browser shader, so the
numbers in this file remain the physical metres of the raw DEM.

Besides the two surfaces, this module bakes a snow-particle cloud covering the
whole terrain. Each particle carries the time at which the avalanche first
reached its cell, the peak speed there and its downslope direction, so the
browser can show snow resting across the entire mountain and start moving it
only once the flow actually arrives.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "data" / "source" / "jiangjunshan"

SOURCE_TEXT = {
    "jiangjunshan": "Copernicus DEM GLO-30 · 将军山(1 弧秒,EGM2008→WGS84)",
    "parabola": "AvaFrame 官方 avaParabola 示例地形",
}
DISPLAY_EXAGGERATION = {"jiangjunshan": 1.5, "parabola": 1.0}
PARTICLES_PER_CELL = 6
FLOW_PARTICLES_PER_CELL = 96
AREA_THRESHOLD = 0.05

# x,y,z,nx,ny,nz,u,v,arrivalSeconds,peakSpeed,columnFraction,sizeRandom,seed
PARTICLE_LAYOUT = "float32 LE x,y,z,nx,ny,nz,u,v,arrival,peakSpeed,column,sizeRand,seed"
PARTICLE_STRIDE = 13


def read_grid(path):
    with Path(path).open() as handle:
        header = {key.lower(): float(value) for key, value in (next(handle).split() for _ in range(6))}
    values = np.loadtxt(path, skiprows=6, dtype=np.float32)
    values[values == header["nodata_value"]] = np.nan
    return header, values


def json_save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _candidate_overlays(zones, header, dem, center, kind):
    """Project candidate rings (lon/lat) onto the run's local metric frame."""
    if not zones:
        return []
    from pyproj import Transformer

    from jiangjunshan import CRS

    to_crs = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
    cell = header["cellsize"]
    x0 = header.get("xllcenter", header.get("xllcorner", 0) + cell / 2)
    y0 = header.get("yllcenter", header.get("yllcorner", 0) + cell / 2)
    overlays = []
    for candidate in zones.get("candidates", []):
        ring = candidate.get("ring") or []
        if len(ring) < 4:
            continue
        xs, ys = to_crs.transform([point[0] for point in ring], [point[1] for point in ring])
        points = []
        for x, y in zip(xs, ys):
            col = int(np.clip(round((x - x0) / cell), 0, dem.shape[1] - 1))
            row = int(np.clip(dem.shape[0] - 1 - round((y - y0) / cell), 0, dem.shape[0] - 1))
            points.append([float(x - center[0]), float(y - center[1]), float(dem[row, col] - center[2] + 4)])
        overlays.append({
            "kind": kind, "id": candidate["id"], "rank": candidate.get("rank"),
            "areaM2": candidate.get("areaM2"), "points": points,
        })
    return overlays


def _build_particles(dem, normals, row_ids, col_ids, arrival, peak_speed, center, cell, x0, y0,
                     seed=20261115):
    """Bake a whole-terrain snow cloud from the solver time series.

    Every display cell gets a base sprinkle so the mountain reads as snow. Cells
    the avalanche actually reaches get extra particles, dense enough to show
    entrained snow spraying above the flowing surface.
    """
    rows, cols = len(row_ids), len(col_ids)
    rng = np.random.default_rng(seed)
    blocks = []
    display_stride = int(col_ids[1] - col_ids[0]) if len(col_ids) > 1 else 1

    sampled = dem[np.ix_(row_ids, col_ids)]
    sampled_normals = normals[np.ix_(row_ids, col_ids)]
    # The mesh is authored around the local origin, so the particle grid has to
    # use the same absolute-then-centred coordinates as terrain.bin.
    grid_x, grid_y = np.meshgrid(x0 + col_ids * cell, y0 + (dem.shape[0] - 1 - row_ids) * cell)

    for row in range(rows):
        for col in range(cols):
            native_row, native_col = row_ids[row], col_ids[col]
            arrival_s = float(arrival[native_row, native_col])
            in_flow = np.isfinite(arrival_s) and arrival_s >= 0
            if not in_flow:
                arrival_s = -1.0
            count = PARTICLES_PER_CELL + (FLOW_PARTICLES_PER_CELL if in_flow else 0)
            speed = float(peak_speed[native_row, native_col])
            # Each display vertex represents a stride-by-stride native block.
            # Spread the samples across that footprint to avoid the old 3x4 dots.
            footprint = cell * display_stride
            jitter_x = (rng.random(count) - 0.5) * footprint
            jitter_y = (rng.random(count) - 0.5) * footprint
            phase = rng.random(count)
            # column: where the particle sits inside the flow thickness (0 = bed,
            # 1 = top of the moving mass); sizeRand spreads sprite sizes.
            column = rng.random(count)
            size_rand = rng.random(count)
            nx, ny, nz = (float(value) for value in sampled_normals[row, col])
            blocks.append(np.stack([
                grid_x[row, col] + jitter_x - center[0],
                grid_y[row, col] + jitter_y - center[1],
                np.full(count, float(sampled[row, col] - center[2])),
                np.full(count, nx),
                np.full(count, ny),
                np.full(count, nz),
                np.full(count, (col + 0.5) / cols),
                np.full(count, (row + 0.5) / rows),
                np.full(count, arrival_s),
                np.full(count, speed),
                column,
                size_rand,
                phase,
            ], axis=1))
    return np.concatenate(blocks, axis=0).astype("<f4")


def pack_run(ava_dir, run_id, scenario, thickness, terrain="parabola", zone=None, zones=None,
             lonlat=None, snow_depth=0.35):
    ava_dir = Path(ava_dir)
    output = ava_dir / "Outputs" / "com1DFA" / "peakFiles"
    frame_dir = output / "timeSteps"
    files = sorted(frame_dir.glob("*_FT_t*.asc"))
    if len(files) < 3:
        raise ValueError(f"Expected at least three FT snapshots; found {len(files)} in {frame_dir}")
    prefixes = {p.name.rsplit("_FT_t", 1)[0] for p in files}
    if len(prefixes) != 1:
        raise ValueError("Each run directory must contain exactly one simulation")
    sim_name = prefixes.pop()
    frames = sorted(files, key=lambda p: float(p.stem.rsplit("_t", 1)[1]))
    header, dem = read_grid(next((ava_dir / "Inputs").glob("*.asc")))
    peak_header, peak_h = read_grid(output / f"{sim_name}_pft.asc")
    _, peak_v = read_grid(output / f"{sim_name}_pfv.asc")
    if dem.shape != peak_h.shape or header["cellsize"] != peak_header["cellsize"]:
        raise ValueError("DEM and computed rasters must share the same grid")
    if not np.isfinite(dem).all():
        raise ValueError("This demo packer requires a DEM without NoData cells")
    public = ROOT / "data" / "public" / run_id
    public.mkdir(parents=True, exist_ok=True)
    # Full DEM extent is retained; a 60 m display grid preserves terrain/flow
    # shape without shipping every 30 m solver cell to the browser.
    stride = 2
    row_ids = np.unique(np.append(np.arange(0, dem.shape[0], stride), dem.shape[0] - 1))
    col_ids = np.unique(np.append(np.arange(0, dem.shape[1], stride), dem.shape[1] - 1))
    sampled = dem[np.ix_(row_ids, col_ids)]
    cell = header["cellsize"]
    x0 = header.get("xllcenter", header.get("xllcorner", 0) + cell / 2)
    y0 = header.get("yllcenter", header.get("yllcorner", 0) + cell / 2)
    xs = x0 + col_ids * cell
    ys = y0 + (dem.shape[0] - 1 - row_ids) * cell
    bounds = [float(xs[0]), float(ys[-1]), float(xs[-1]), float(ys[0])]
    center = [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2, float(np.min(dem))]
    xx, yy = np.meshgrid(xs - center[0], ys - center[1])
    # The native FT is normal-to-slope thickness. Surface displacement follows
    # the terrain normal, rather than treating FT as vertical water depth.
    dzdy, dzdx = np.gradient(dem, -cell, cell)
    normals = np.stack((-dzdx, -dzdy, np.ones_like(dem)), axis=-1)
    normals /= np.linalg.norm(normals, axis=-1, keepdims=True)
    sn = normals[np.ix_(row_ids, col_ids)]
    positions = np.stack((xx, yy, sampled - center[2]), axis=-1)
    vertex_data = np.concatenate((positions, sn), axis=-1).astype("<f4")
    vertex_data.tofile(public / "terrain.bin")

    height_scale = max(float(np.nanmax(peak_h)) * 1.01, 0.1)
    speed_scale = max(float(np.nanmax(peak_v)) * 1.01, 0.1)
    frame_bytes = sampled.size * 4
    entries = []
    # The particle cloud needs per-cell arrival time and peak speed, accumulated
    # here so the ASC series is only read once.
    arrival = np.full(dem.shape, np.nan, dtype=np.float64)
    peak_speed = np.zeros(dem.shape, dtype=np.float32)
    wet_rows = []
    wet_cols = []
    with (public / "frames.bin").open("wb") as stream:
        for index, ft_path in enumerate(frames):
            timestamp = float(ft_path.stem.rsplit("_t", 1)[1])
            fv_path = ft_path.with_name(ft_path.name.replace("_FT_t", "_FV_t"))
            _, h = read_grid(ft_path)
            _, v = read_grid(fv_path)
            if h.shape != dem.shape or v.shape != dem.shape:
                raise ValueError("Time step grid dimensions differ from terrain")
            if not np.isfinite(h).all() or not np.isfinite(v).all():
                raise ValueError(f"Non-finite output in {ft_path.name}")
            hs = h[np.ix_(row_ids, col_ids)]
            vs = v[np.ix_(row_ids, col_ids)]
            qh = np.rint(np.clip(hs / height_scale, 0, 1) * 65535).astype(np.uint16)
            qv = np.rint(np.clip(vs / speed_scale, 0, 1) * 65535).astype(np.uint16)
            rgba = np.stack((qh >> 8, qh & 255, qv >> 8, qv & 255), axis=-1).astype(np.uint8)
            stream.write(rgba.tobytes())
            wet = h > AREA_THRESHOLD
            if np.any(wet):
                rr, cc = np.nonzero(wet)
                wet_rows.append(rr)
                wet_cols.append(cc)
                newly = wet & ~np.isfinite(arrival)
                arrival[newly] = timestamp
                centroid = [float(x0 + np.mean(cc) * cell - center[0]),
                            float(y0 + (dem.shape[0] - 1 - np.mean(rr)) * cell - center[1])]
            else:
                centroid = [0, 0]
            np.maximum(peak_speed, v, out=peak_speed)
            entries.append({"t": timestamp, "index": index, "maxDepth": float(h.max()),
                            "maxSpeed": float(v.max()), "area": float(wet.sum() * cell * cell),
                            "centroid": centroid})
    arrival[~np.isfinite(arrival)] = -1.0

    # Region the flow actually touched, used to frame the default camera.
    if wet_rows:
        rows_all = np.concatenate(wet_rows)
        cols_all = np.concatenate(wet_cols)
        flow_bounds = [
            float(x0 + cols_all.min() * cell - center[0]),
            float(y0 + (dem.shape[0] - 1 - rows_all.max()) * cell - center[1]),
            float(x0 + cols_all.max() * cell - center[0]),
            float(y0 + (dem.shape[0] - 1 - rows_all.min()) * cell - center[1]),
        ]
    else:
        flow_bounds = [bounds[0] - center[0], bounds[1] - center[1],
                       bounds[2] - center[0], bounds[3] - center[1]]

    particles = _build_particles(dem, normals, row_ids, col_ids, arrival, peak_speed, center, cell, x0, y0)
    particles.tofile(public / "snow.bin")

    albedo = None
    if terrain == "jiangjunshan":
        imagery = SOURCE_DIR / "sentinel2-winter.webp"
        if imagery.exists():
            (public / "albedo.webp").write_bytes(imagery.read_bytes())
            albedo = f"/data/{run_id}/albedo.webp"

    import shapefile
    overlays = []
    # REL/RES are operational overlays. ENT and SECREL are already represented
    # by the full snow layer and terrain-derived candidate outlines, so drawing
    # their shapefiles again would duplicate labels and obscure the mountain.
    for folder, kind in (("REL", "release"), ("RES", "resistance")):
        for shp in (ava_dir / "Inputs" / folder).glob("*.shp"):
            for shape in shapefile.Reader(str(shp)).shapes():
                starts = list(shape.parts) + [len(shape.points)]
                for a, b in zip(starts, starts[1:]):
                    points = []
                    for px, py in shape.points[a:b]:
                        c = int(np.clip(round((px - x0) / cell), 0, dem.shape[1] - 1))
                        r = int(np.clip(dem.shape[0] - 1 - round((py - y0) / cell), 0, dem.shape[0] - 1))
                        points.append([px - center[0], py - center[1], float(dem[r, c] - center[2] + 3)])
                    overlays.append({"kind": kind, "points": points})
    if terrain == "jiangjunshan":
        overlays.extend(_candidate_overlays(zones, header, dem, center, "candidate"))

    zone_summary = None
    if zone:
        zone_summary = {key: zone[key] for key in
                        ("id", "rank", "areaM2", "meanScore", "maxScore", "meanSlopeDeg",
                         "maxSlopeDeg", "rankScore", "representativePoint") if key in zone}
    candidate_summaries = []
    for candidate in (zones or {}).get("candidates", []):
        candidate_summaries.append({
            "id": candidate["id"], "rank": candidate.get("rank"),
            "areaM2": candidate.get("areaM2"), "meanSlopeDeg": candidate.get("meanSlopeDeg"),
            "maxSlopeDeg": candidate.get("maxSlopeDeg"), "meanScore": candidate.get("meanScore"),
            "rankScore": candidate.get("rankScore"),
            "representativePoint": candidate.get("representativePoint"),
        })

    mass_summary = None
    mass_files = list((ava_dir / "Outputs" / "com1DFA").glob("mass_*.txt"))
    if len(mass_files) == 1:
        mass = np.genfromtxt(mass_files[0], delimiter=",", names=True)
        initial_mass = float(mass["current"][0])
        final_mass = float(mass["current"][-1])
        entrained_mass = float(np.sum(mass["entrained"]))
        mass_summary = {
            "initialMassKg": initial_mass,
            "finalMassKg": final_mass,
            "entrainedMassKg": entrained_mass,
            "massGainPercent": (final_mass - initial_mass) / initial_mass * 100.0,
        }

    if terrain == "jiangjunshan":
        title = f"将军山 · 高危险起崩区 {zone_summary['id']}" if zone_summary else "将军山 · 雪崩过程"
    else:
        title = "基准情景" if scenario == "null" else "阻力区情景"
    manifest = {
        "id": run_id, "title": title, "terrain": terrain,
        "engine": "AvaFrame com1DFA", "simName": sim_name, "scenario": scenario,
        "initialThickness": thickness,
        "synthetic": terrain != "jiangjunshan", "georeferenced": terrain == "jiangjunshan",
        "source": SOURCE_TEXT.get(terrain, SOURCE_TEXT["parabola"]),
        "nativeCellSize": cell, "displayStride": stride, "bounds": bounds, "origin": center,
        "width": len(col_ids), "height": len(row_ids), "terrainLayout": "float32 LE x,y,z,nx,ny,nz",
        "frameLayout": "RGBA8: h_uint16=R*256+G; v_uint16=B*256+A; north-to-south rows",
        "frameBytes": frame_bytes, "heightScale": height_scale, "speedScale": speed_scale,
        "heightMin": float(sampled.min() - center[2]), "heightMax": float(sampled.max() - center[2]),
        "peakDepth": float(np.nanmax(peak_h)), "peakSpeed": float(np.nanmax(peak_v)),
        "impactArea": float((peak_h > AREA_THRESHOLD).sum() * cell * cell),
        "areaThreshold": AREA_THRESHOLD, "duration": entries[-1]["t"], "frames": entries,
        "overlays": overlays,
        # Display-only vertical stretch; the packaged geometry stays in raw metres.
        "verticalExaggeration": DISPLAY_EXAGGERATION.get(terrain, 1.0),
        "verticalExaggerationRange": [1.0, 3.0],
        "zone": zone_summary, "candidates": candidate_summaries,
        "snowpack": {"entrainableDepthM": snow_depth, "state": "static -> flowing -> deposited"},
        "mass": mass_summary,
        "flowBounds": flow_bounds,
        "particles": {"count": int(particles.shape[0]), "stride": PARTICLE_STRIDE,
                      "layout": PARTICLE_LAYOUT, "bytes": int(particles.nbytes),
                      "url": f"/data/{run_id}/snow.bin"},
        "albedo": albedo,
    }
    if lonlat is None and terrain == "jiangjunshan":
        from pyproj import Transformer

        from jiangjunshan import CRS

        lonlat = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True).transform(center[0], center[1])
    if lonlat:
        manifest["originLonLat"] = [float(lonlat[0]), float(lonlat[1])]
        manifest["crs"] = "EPSG:32645"
    if zones:
        manifest["zoneModel"] = zones.get("model")
        manifest["zoneWarning"] = zones.get("warning")
    json_save(public / "manifest.json", manifest)
    index_path = ROOT / "data" / "public" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    entry = {key: manifest[key] for key in ("id", "title", "scenario", "initialThickness",
                                            "duration", "peakDepth", "peakSpeed", "terrain")}
    entry["zone"] = zone_summary
    index = [item for item in index if item["id"] != run_id]
    # The highest-risk Jiangjunshan run is the entry point of the demo; synthetic
    # examples and lower-ranked zones stay in the list for comparison.
    entry["default"] = bool(zone_summary and zone_summary.get("rank") == 1)
    if entry["default"]:
        for item in index:
            item.pop("default", None)
    index.append(entry)
    index.sort(key=lambda item: (not item.get("default"), item.get("terrain") != "jiangjunshan"))
    json_save(index_path, index)
    return f"/data/{run_id}/manifest.json"


if __name__ == "__main__":
    import argparse
    import sys

    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--id", required=True)
    parser.add_argument("--scenario", choices=("null", "res", "ent"), default="null")
    parser.add_argument("--thickness", type=float, default=1.)
    parser.add_argument("--terrain", choices=("jiangjunshan", "parabola"), default="parabola")
    parser.add_argument("--zone", default="", help="release zone id used by the run")
    parser.add_argument("--snow-depth", type=float, default=0.35)
    args = parser.parse_args()
    zones = None
    zone = None
    if args.terrain == "jiangjunshan":
        from release_zones import by_id, ensure
        zones = ensure()
        zone = by_id(args.zone) if args.zone else next(iter(zones["candidates"]), None)
    print(pack_run(args.directory, args.id, args.scenario, args.thickness,
                   terrain=args.terrain, zone=zone, zones=zones, snow_depth=args.snow_depth))
