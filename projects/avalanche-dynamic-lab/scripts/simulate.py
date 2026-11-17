"""Run real AvaFrame computations in an isolated workspace, then package time frames.

Two terrains are supported:

`jiangjunshan`
    The real Jiangjunshan crop in `data/source/jiangjunshan`. The raw GLO-30
    elevations are resampled onto a 30 m UTM 45N grid and the release area is
    taken from the terrain-susceptibility screen in `release_zones.py`. The
    highest ranked candidate is chosen unless `--zone` names another one.

`parabola`
    The synthetic AvaFrame `avaParabola` example previously shipped. Kept as a
    reference scenario with the identical physics settings.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
for path in (CACHE / "tmp", CACHE / "matplotlib"):
    path.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = str(CACHE / "tmp")
os.environ["TMP"] = str(CACHE / "tmp")
os.environ["MPLCONFIGDIR"] = str(CACHE / "matplotlib")
os.environ["MPLBACKEND"] = "Agg"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
# This machine's PATH also exposes a PostgreSQL PROJ database that is older than
# the PROJ bundled with the AvaFrame environment. Dropping the overrides lets
# rasterio/pyproj resolve the env's own proj.db instead of failing on EPSG codes.
for _variable in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA"):
    os.environ.pop(_variable, None)

sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_CELL = 30.0


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def progress(run_id, phase, message, **extra):
    write_json(ROOT / "data" / "jobs" / f"{run_id}.json", {
        "id": run_id, "phase": phase, "message": message, "updated": time.time(), **extra,
    })
    print(f"[{phase}] {message}", flush=True)


def write_polygons(path, features):
    """Write one or more snow polygons in the DEM metric CRS."""
    import shapefile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = shapefile.Writer(str(path), shapeType=shapefile.POLYGON)
    writer.field("ID", "C", 40)
    writer.field("Name", "C", 40)
    writer.field("thickness", "N", 11, 3)
    for index, (name, points, thickness) in enumerate(features, start=1):
        ring = [(float(x), float(y)) for x, y in points]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        writer.poly([ring])
        writer.record(str(index), name, round(float(thickness), 3))
    writer.close()
    path.with_suffix(".prj").write_text(
        'PROJCS["WGS 84 / UTM zone 45N",GEOGCS["WGS 84",DATUM["WGS_1984",'
        'SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],'
        'UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
        'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",87],'
        'PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],'
        'PARAMETER["false_northing",0],UNIT["metre",1]]', encoding="ascii")
    return path


def prepare_jiangjunshan(ava_dir, zone_id, thickness, cell=DEFAULT_CELL,
                         snow_depth=0.35, secondary_depth=0.65):
    """Build an entrainment run with terrain-derived secondary release areas."""
    from jiangjunshan import build_metric, load_source, lonlat_to_xy, write_ascii
    from release_zones import by_id, ensure

    elevation, _meta, bounds = load_source()
    metric = build_metric(elevation, bounds, cell=cell)
    inputs = Path(ava_dir) / "Inputs"
    dem_path = inputs / "DEM_JJS_Topo.asc"
    write_ascii(dem_path, metric["grid"], metric["header"])

    zone = by_id(zone_id)
    ring = [(point[0], point[1]) for point in zone["ring"]]
    xs, ys = lonlat_to_xy(metric, [p[0] for p in ring], [p[1] for p in ring])
    release_path = write_polygons(inputs / "REL" / f"{zone_id}.shp",
                                  [(zone_id, list(zip(xs, ys)), thickness)])

    # A finite entrainable layer covers the whole operational terrain. AvaFrame
    # rejects polygons on the raster envelope, so keep it one solver cell in.
    header = metric["header"]
    x0 = header["xllcenter"]
    y0 = header["yllcenter"]
    xmax = x0 + (int(header["ncols"]) - 1) * metric["cellSize"]
    ymax = y0 + (int(header["nRows"]) - 1) * metric["cellSize"]
    inset = metric["cellSize"]
    snow_ring = [(x0 + inset, y0 + inset), (x0 + inset, ymax - inset),
                 (xmax - inset, ymax - inset), (xmax - inset, y0 + inset),
                 (x0 + inset, y0 + inset)]
    entrainment_path = write_polygons(inputs / "ENT" / "whole-mountain-snowpack.shp",
                                      [("whole-mountain", snow_ring, snow_depth)])

    secondary = []
    for candidate in ensure()["candidates"]:
        if candidate["id"] == zone_id:
            continue
        candidate_ring = [(point[0], point[1]) for point in candidate["ring"]]
        sx, sy = lonlat_to_xy(metric, [p[0] for p in candidate_ring],
                              [p[1] for p in candidate_ring])
        secondary.append((candidate["id"], list(zip(sx, sy)), secondary_depth))
    secondary_path = write_polygons(inputs / "SECREL" / "terrain-risk-secondary.shp", secondary)
    return {
        "zone": zone,
        "dem": dem_path,
        "release": release_path,
        "entrainment": entrainment_path,
        "secondaryRelease": secondary_path,
        "snowDepth": snow_depth,
        "secondaryDepth": secondary_depth,
        "cell": metric["cellSize"],
        "crs": metric["crs"],
        "lonLat": metric["centreLonLat"],
        "zones": ensure(),
    }


def prepare_parabola(ava_dir, source):
    import avaframe

    source = Path(source) if source else Path(avaframe.__file__).parent / "data" / "avaParabola" / "Inputs"
    shutil.copytree(source, Path(ava_dir) / "Inputs", dirs_exist_ok=True)
    return None


def run(run_id, scenario, thickness, source="", terrain="jiangjunshan", zone=None,
        cell=DEFAULT_CELL, snow_depth=0.35, secondary_depth=0.65):
    from avaframe.com1DFA import com1DFA
    from avaframe.in3Utils import cfgUtils, logUtils

    started = time.time()
    ava_dir = ROOT / "data" / "runs" / run_id
    if (ava_dir / "Outputs").exists():
        raise FileExistsError(f"Run already exists: {run_id}; choose a new run id")

    prepared = None
    progress(run_id, "preparing", "正在准备独立计算目录", scenario=scenario, thickness=thickness,
             terrain=terrain, zone=zone)
    if terrain == "jiangjunshan":
        prepared = prepare_jiangjunshan(ava_dir, zone, thickness, cell=cell,
                                        snow_depth=snow_depth, secondary_depth=secondary_depth)
        scenario = "ent"
    elif terrain == "parabola":
        prepare_parabola(ava_dir, source)
    else:
        raise ValueError(f"Unknown terrain: {terrain}")

    cfg_main = cfgUtils.getGeneralConfig()
    cfg_main["MAIN"]["avalancheDir"] = str(ava_dir)
    cfg_main["MAIN"]["nCPU"] = "1"
    cfg_main["FLAGS"]["showPlot"] = "False"
    cfg_main["FLAGS"]["savePlot"] = "False"
    cfg_main["FLAGS"]["createReport"] = "False"
    cfg_main["FLAGS"]["showOnlineBackground"] = "False"
    cfg = cfgUtils.getModuleConfig(com1DFA, onlyDefault=True, toPrint=False)
    cfg["GENERAL"]["simTypeList"] = scenario
    cfg["GENERAL"]["resType"] = "pft|pfv|ppr|FT|FV"
    cfg["GENERAL"]["tSteps"] = "0:2"
    cfg["GENERAL"]["relThFromFile"] = "True" if prepared else "False"
    if not prepared:
        cfg["GENERAL"]["relTh"] = str(thickness)
    cfg["GENERAL"]["secRelArea"] = "True" if prepared else "False"
    if prepared:
        # Solve on the DEM's own grid. AvaFrame otherwise remeshes to its default
        # 5 m cell, which both explodes the grid and desyncs the published terrain
        # from the computed rasters.
        cfg["GENERAL"]["meshCellSize"] = str(prepared["cell"])
        cfg["GENERAL"]["entThFromFile"] = "True"
        cfg["GENERAL"]["secondaryRelThFromFile"] = "True"
    cfg["VISUALISATION"]["writePartToCSV"] = "False"
    logUtils.initiateLogger(str(ava_dir), "simulation")
    progress(run_id, "computing", "AvaFrame 正在计算雪崩时序", scenario=scenario, thickness=thickness,
             terrain=terrain, zone=zone)
    com1DFA.com1DFAMain(cfg_main, cfgInfo=cfg)
    progress(run_id, "packing", "正在转换地形和逐帧流深、流速", scenario=scenario, thickness=thickness,
             terrain=terrain, zone=zone)
    from pack import pack_run

    manifest = pack_run(ava_dir, run_id, scenario, thickness, terrain=terrain,
                        zone=prepared["zone"] if prepared else None,
                        zones=prepared["zones"] if prepared else None,
                        snow_depth=snow_depth)
    progress(run_id, "complete", "三维时序数据已就绪", scenario=scenario, thickness=thickness,
             terrain=terrain, zone=zone, manifest=manifest, elapsed=round(time.time() - started, 1))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--scenario", choices=("null", "res", "ent"), default="ent")
    parser.add_argument("--thickness", type=float, default=1.0)
    parser.add_argument("--terrain", choices=("jiangjunshan", "parabola"), default="jiangjunshan")
    parser.add_argument("--zone", default="", help="release zone id such as JJS-R01; default is the highest ranked")
    parser.add_argument("--cell", type=float, default=DEFAULT_CELL, help="metric DEM cell size in metres")
    parser.add_argument("--snow-depth", type=float, default=0.35,
                        help="whole-mountain entrainable snow depth in metres")
    parser.add_argument("--secondary-depth", type=float, default=0.65,
                        help="secondary high-risk zone release depth in metres")
    parser.add_argument("--source", default="", help="parabola input directory override")
    args = parser.parse_args()
    if not all(c.isalnum() or c in "-_" for c in args.id):
        parser.error("Run id must contain only letters, digits, hyphens and underscores")
    if not 0.2 <= args.thickness <= 3:
        parser.error("Thickness must be between 0.2 and 3 meters")
    zone = args.zone
    if args.terrain == "jiangjunshan" and not zone:
        from release_zones import ensure
        summary = ensure()
        if not summary["candidates"]:
            parser.error("No release zone was detected on this terrain")
        zone = summary["candidates"][0]["id"]
    try:
        run(args.id, args.scenario, args.thickness, args.source, terrain=args.terrain,
            zone=zone or None, cell=args.cell, snow_depth=args.snow_depth,
            secondary_depth=args.secondary_depth)
    except Exception as exc:
        progress(args.id, "failed", str(exc))
        traceback.print_exc()
        sys.exit(1)
