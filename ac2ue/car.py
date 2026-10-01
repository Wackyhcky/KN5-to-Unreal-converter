"""Assetto Corsa car -> skinned glTF + textures + skins + physics manifest.

The car becomes ONE skeletal mesh with a root bone and four wheel bones
(WHEEL_LF/RF/LR/RR), which is what Unreal's Chaos vehicles drive. Every part
under an AC wheel node (rim, tyre, nuts) is skinned to that wheel's bone; the
rest of the car is skinned to the root.

The mesh is written in a vehicle frame: glTF +X = forward, +Y = up, +Z = right,
meters, origin on the ground midway between the axles. The Unreal script checks
the engine's glTF axis mapping with the calibration shape and corrects the file
if needed, so the car always faces Unreal +X as Chaos expects.
"""

from __future__ import annotations

import json
import os
import re
import time

import numpy as np

from . import __version__
from .convert import (Log, _Unique, _bake_mesh, _winding_vote, _write_texture,
                      sanitize, write_default_textures)
from .gltf_writer import write_calibration_glb, write_skinned_glb
from .kn5_reader import Kn5Error, read_kn5
from .materials import ROLE_PRIORITY, convert_material, texture_roles
from .track import parse_ini

WHEELS = ("LF", "RF", "LR", "RR")
# Runtime variants AC swaps in (motion-blurred rims, crash damage).
VARIANT_RE = re.compile(r"BLUR|DAMAGE", re.I)

# Used when the car's data/ folder isn't readable (Kunos cars pack theirs in
# data.acd, which this tool does not unpack).
DEFAULT_PHYSICS = {
    "mass": 1300.0,
    "drive": "RWD",
    "max_steer_deg": 35.0,
    "engine": {"max_torque": 400.0, "max_rpm": 7000.0, "idle_rpm": 900.0, "curve": []},
    "gears": {"forward": [3.5, 2.2, 1.5, 1.15, 0.92, 0.76], "reverse": 3.4, "final": 3.7},
    "front": {"radius": None, "width": None, "spring_rate": None},
    "rear": {"radius": None, "width": None, "spring_rate": None},
}


def is_car_folder(path: str) -> bool:
    if os.path.isfile(path):
        path = os.path.dirname(path)
    path = os.path.abspath(path)
    entries = os.listdir(path) if os.path.isdir(path) else []
    # Track markers win: tracks can have skins/ too (CSP track skins).
    if (os.path.isfile(os.path.join(path, "ui", "ui_track.json"))
            or any(e.lower().startswith("models") and e.lower().endswith(".ini") for e in entries)):
        return False
    if any(os.path.exists(os.path.join(path, p))
           for p in ("data.acd", os.path.join("ui", "ui_car.json"), "lods.ini", "collider.kn5")):
        return True
    parts = [p.lower() for p in os.path.normpath(path).split(os.sep)]
    return len(parts) >= 2 and parts[-2] == "cars"


def _pick_kn5(folder: str, log: Log) -> str:
    lods = os.path.join(folder, "lods.ini")
    if os.path.isfile(lods):
        sec = parse_ini(lods).get("LOD_0", {})
        f = sec.get("FILE")
        if f and os.path.isfile(os.path.join(folder, f)):
            return os.path.join(folder, f)
    kn5s = [f for f in os.listdir(folder) if f.lower().endswith(".kn5")]
    lod_a = [f for f in kn5s if re.search(r"_lod_a\.kn5$", f, re.I)]
    if lod_a:
        return os.path.join(folder, lod_a[0])
    main = [f for f in kn5s if not re.search(r"_lod_[b-z]\.kn5$|^collider\.kn5$|^driver", f, re.I)]
    if not main:
        raise Kn5Error(f"no car model (.kn5) found in {folder}")
    main.sort(key=lambda f: os.path.getsize(os.path.join(folder, f)), reverse=True)
    return os.path.join(folder, main[0])


def _lowres_twins(names) -> set:
    """COCKPIT_LR / STEER_LR are low-res twins only when an _HR partner exists
    (WHEEL_LR, SUSP_LR... mean left-rear)."""
    have = {n.lower() for n in names}
    return {n.lower() for n in names if n.lower().endswith("_lr") and n[:-3].lower() + "_hr" in have}


def _fold_texture_case(kn5) -> None:
    """AC matches texture names case-insensitively; keep the largest blob."""
    keep = {}
    for t in kn5.textures:
        k = t.name.lower()
        if k not in keep or len(t.data or b"") > len(keep[k].data or b""):
            keep[k] = t
    canon = {k: t.name for k, t in keep.items()}
    kn5.textures = list(keep.values())
    for m in kn5.materials:
        for slot, tex in list(m.mappings.items()):
            m.mappings[slot] = canon.get(tex.lower(), tex)


# ------------------------------------------------------------------ physics

def _f(sec, key, default=None):
    try:
        return float(str(sec.get(key, "")).split()[0])
    except (ValueError, IndexError):
        return default


def _read_lut(path):
    pts = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.split(";")[0].strip()
                if "|" in line:
                    a, b = line.split("|", 1)
                    try:
                        pts.append((float(a), float(b)))
                    except ValueError:
                        pass
    return pts


def read_physics(folder: str, log: Log) -> dict:
    phys = json.loads(json.dumps(DEFAULT_PHYSICS))
    data = os.path.join(folder, "data")
    if not os.path.isdir(data):
        src = "data.acd (packed, not read)" if os.path.isfile(os.path.join(folder, "data.acd")) else "none"
        log.warn(f"no readable data/ folder ({src}); vehicle physics are estimated - tune them in Unreal")
        phys["source"] = "estimated"
        return phys
    phys["source"] = "data/"

    def ini(name):
        p = os.path.join(data, name)
        return parse_ini(p) if os.path.isfile(p) else {}

    car = ini("car.ini")
    phys["mass"] = _f(car.get("BASIC", {}), "TOTALMASS", phys["mass"])
    ctl = car.get("CONTROLS", {})
    lock, ratio = _f(ctl, "STEER_LOCK"), _f(ctl, "STEER_RATIO")
    if lock and ratio:
        phys["max_steer_deg"] = round(lock / ratio, 2)

    dt = ini("drivetrain.ini")
    drive = str(dt.get("TRACTION", {}).get("TYPE", "RWD")).upper()
    phys["drive"] = "AWD" if drive.startswith("AWD") else drive if drive in ("RWD", "FWD") else "RWD"
    gears = dt.get("GEARS", {})
    count = int(_f(gears, "COUNT", 0) or 0)
    fwd = [_f(gears, f"GEAR_{i}") for i in range(1, count + 1)]
    if fwd and all(fwd):
        phys["gears"]["forward"] = fwd
    if _f(gears, "GEAR_R"):
        phys["gears"]["reverse"] = abs(_f(gears, "GEAR_R"))
    if _f(gears, "FINAL"):
        phys["gears"]["final"] = _f(gears, "FINAL")

    eng = ini("engine.ini")
    ed = eng.get("ENGINE_DATA", {})
    phys["engine"]["idle_rpm"] = _f(ed, "MINIMUM", phys["engine"]["idle_rpm"])
    phys["engine"]["max_rpm"] = _f(ed, "LIMITER", phys["engine"]["max_rpm"])
    lut_name = eng.get("HEADER", {}).get("POWER_CURVE", "power.lut")
    curve = _read_lut(os.path.join(data, lut_name))
    if curve:
        phys["engine"]["curve"] = curve
        phys["engine"]["max_torque"] = max(t for _, t in curve)

    tyres = ini("tyres.ini")
    susp = ini("suspensions.ini")
    for axle, sec in (("front", "FRONT"), ("rear", "REAR")):
        ty = tyres.get(sec, {})
        phys[axle]["radius"] = _f(ty, "RADIUS")
        phys[axle]["width"] = _f(ty, "WIDTH")
        phys[axle]["spring_rate"] = _f(susp.get(sec, {}), "SPRING_RATE")
    return phys


# ---------------------------------------------------------------- converter

def _wheel_of(path) -> str | None:
    for name in path:
        m = re.fullmatch(r"WHEEL_(LF|RF|LR|RR)", name, re.I)
        if m:
            return m.group(1).upper()
    return None


def convert_car(input_path: str, out_dir: str, skins: str = "all", jobs: int = 4,
                verbose: bool = False, stream=None) -> str:
    log = Log(stream, verbose)
    t0 = time.time()
    folder = input_path if os.path.isdir(input_path) else os.path.dirname(os.path.abspath(input_path))
    folder = os.path.abspath(folder)
    car_name = os.path.basename(os.path.normpath(folder))
    kn5_path = input_path if os.path.isfile(input_path) else _pick_kn5(folder, log)
    log.info(f"Car '{car_name}': reading {os.path.basename(kn5_path)} ...")
    kn5 = read_kn5(kn5_path)
    _fold_texture_case(kn5)
    log.info(f"  KN5 v{kn5.version}: {len(kn5.textures)} textures, {len(kn5.materials)} materials")

    os.makedirs(out_dir, exist_ok=True)
    tex_dir = os.path.join(out_dir, "textures", "car")
    os.makedirs(tex_dir, exist_ok=True)

    # ---- skins: AC applies the FIRST skin folder by default
    skins_root = os.path.join(folder, "skins")
    skin_names = sorted(d for d in os.listdir(skins_root)
                        if os.path.isdir(os.path.join(skins_root, d))) if os.path.isdir(skins_root) else []
    if skins == "default":
        skin_names = skin_names[:1]
    default_skin = skin_names[0] if skin_names else None
    kn5_names = {t.name.lower(): t.name for t in kn5.textures}

    def skin_files(skin):
        d = os.path.join(skins_root, skin)
        out = {}
        for f in os.listdir(d):
            if f.lower() in kn5_names:
                out[kn5_names[f.lower()]] = os.path.join(d, f)
        return out

    # ---- textures (base = KN5 textures with the default skin applied)
    roles = texture_roles(kn5.materials)
    uniq = _Unique()
    textures = {}
    base_blobs = {t.name: t.data for t in kn5.textures}
    if default_skin:
        for name, p in skin_files(default_skin).items():
            with open(p, "rb") as fh:
                base_blobs[name] = fh.read()

    def emit(name, blob, folder_out, asset_prefix, unique):
        base_asset = unique(asset_prefix + sanitize(name))
        path, ok, mean = _write_texture(blob, os.path.join(folder_out, base_asset))
        if not ok:
            log.warn(f"texture '{name}' could not be decoded; kept as {os.path.basename(path)}")
        entries = {}
        for i, role in enumerate(sorted(roles.get(name, {"color"}), key=lambda r: ROLE_PRIORITY[r])):
            asset, rpath = base_asset, path
            if i > 0:
                asset = unique(f"{base_asset}_{role}")
                rpath = os.path.join(folder_out, asset + os.path.splitext(path)[1])
                with open(path, "rb") as src, open(rpath, "wb") as dst:
                    dst.write(src.read())
            entries[f"{name}|{role}"] = {"source_name": name, "role": role, "asset": asset,
                                         "file": os.path.relpath(rpath, out_dir).replace("\\", "/"),
                                         "mean_linear": mean}
        return entries

    for name, blob in base_blobs.items():
        if blob:
            textures.update(emit(name, blob, tex_dir, "T_", uniq))

    def lookup(tex_name, role):
        key = f"{tex_name}|{role}"
        return key if key in textures else None

    # ---- materials
    mat_uniq = _Unique()
    materials, mat_keys = {}, []
    for i, mat in enumerate(kn5.materials):
        entry = convert_material(mat, lookup)
        # On damage shaders txNormal holds the DENT map, zero on an intact car.
        nm = mat.mappings.get("txNormal", "")
        if "damage" in nm.lower():
            entry["textures"].pop("Normal", None)
        entry["name"] = mat.name
        entry["asset"] = mat_uniq("MI_" + sanitize(mat.name))
        key = f"{i}:{mat.name}"
        materials[key] = entry
        mat_keys.append(key)

    # ---- other skins: only textures that differ from the base
    skin_entries = {}
    for skin in skin_names:
        files = skin_files(skin)
        entry = {"textures": {}, "materials": {}}
        if skin != default_skin and files:
            sdir = os.path.join(out_dir, "textures", "skins", sanitize(skin))
            os.makedirs(sdir, exist_ok=True)
            su = _Unique()
            for name, p in files.items():
                with open(p, "rb") as fh:
                    entry["textures"].update(emit(name, fh.read(), sdir, f"T_{sanitize(skin)}_", su))
            changed = {e["source_name"] for e in entry["textures"].values()}
            for key, m in materials.items():
                kn5_mat = kn5.materials[int(key.split(":")[0])]
                hits = {slot: tex for slot, tex in kn5_mat.mappings.items() if tex in changed}
                if not hits:
                    continue
                over = {}
                for param, tex_key in m["textures"].items():
                    name, role = tex_key.split("|")
                    if name in changed and tex_key in entry["textures"]:
                        over[param] = tex_key
                if over:
                    entry["materials"][key] = {"asset": f"{m['asset']}__{sanitize(skin)}", "textures": over}
        skin_entries[skin] = entry

    # ---- geometry
    names = [n.name for n, *_ in kn5.iter_nodes()]
    lowres = _lowres_twins(names)
    wheel_centers = {}
    parts = []
    skipped = 0
    for node, world, path, active in kn5.iter_nodes():
        if any(VARIANT_RE.search(p) or p.lower() in lowres for p in path):
            if node.is_mesh:
                skipped += 1
            continue
        w = re.fullmatch(r"WHEEL_(LF|RF|LR|RR)", node.name, re.I)
        if w and node.node_class == 1:
            wheel_centers[w.group(1).upper()] = world[3, :3].copy()
        if not node.is_mesh or not active:
            continue
        baked = _bake_mesh(node, world)
        if baked is None:
            continue
        parts.append((node, path, baked))
    if skipped:
        log.info(f"  skipped {skipped} runtime-variant meshes (blur/damage/low-res twins)")

    # vehicle frame: forward from the front axle's side, up = +Y, right = fwd x up
    up = np.array([0.0, 1.0, 0.0])
    drivable = all(k in wheel_centers for k in WHEELS)
    if drivable:
        front = (wheel_centers["LF"] + wheel_centers["RF"]) / 2
        rear = (wheel_centers["LR"] + wheel_centers["RR"]) / 2
        fwd = np.array([0.0, 0.0, 1.0 if front[2] >= rear[2] else -1.0])
    else:
        missing = [k for k in WHEELS if k not in wheel_centers]
        log.warn(f"wheel nodes missing ({', '.join('WHEEL_' + m for m in missing)}); "
                 "the car is imported as a non-drivable skeletal mesh")
        fwd = np.array([0.0, 0.0, 1.0])
    right = np.cross(fwd, up)
    R = np.stack([fwd, up, right])  # rows: new X, Y, Z

    all_pos = np.concatenate([b[0] for _, _, b in parts]) if parts else np.zeros((1, 3))
    if drivable:
        mid = np.mean([wheel_centers[k] for k in WHEELS], axis=0)
    else:
        mid = (all_pos.min(axis=0) + all_pos.max(axis=0)) / 2
    origin = np.array([mid[0], all_pos[:, 1].min(), mid[2]])

    def to_frame(p):
        return (p - origin) @ R.T

    vote = sum(_winding_vote(b[0], b[1], b[3]) for _, _, b in parts)
    flip = vote < 0

    groups = {}
    wheel_verts = {k: [] for k in WHEELS}
    for node, path, (pos, nrm, uv, tris) in parts:
        wheel = _wheel_of(path) if drivable else None
        bone = WHEELS.index(wheel) + 1 if wheel else 0
        p = to_frame(pos)
        n = nrm @ R.T
        if flip:
            tris = tris[:, [0, 2, 1]]
        if wheel:
            wheel_verts[wheel].append(p)
        key = mat_keys[node.material_id] if node.material_id < len(mat_keys) else mat_keys[0]
        g = groups.setdefault(key, {"pos": [], "nrm": [], "uv": [], "j": [], "idx": [], "count": 0})
        g["idx"].append(tris.reshape(-1) + g["count"])
        g["count"] += len(p)
        g["pos"].append(p)
        g["nrm"].append(n)
        g["uv"].append(uv)
        g["j"].append(np.full(len(p), bone))

    prims = []
    for key, g in groups.items():
        prims.append({"material": materials[key]["asset"], "material_key": key,
                      "positions": np.concatenate(g["pos"]), "normals": np.concatenate(g["nrm"]),
                      "uvs": np.concatenate(g["uv"]), "joints": np.concatenate(g["j"]),
                      "indices": np.concatenate(g["idx"])})

    wheels = {}
    joints = [("root", None, (0.0, 0.0, 0.0))]
    if drivable:
        for k in WHEELS:
            c = to_frame(wheel_centers[k])
            joints.append((f"WHEEL_{k}", 0, tuple(float(x) for x in c)))
            v = np.concatenate(wheel_verts[k]) - c if wheel_verts[k] else None
            radius = float(np.sqrt(v[:, 0] ** 2 + v[:, 1] ** 2).max()) if v is not None else None
            width = float(np.ptp(v[:, 2])) if v is not None else None
            wheels[k] = {"bone": f"WHEEL_{k}", "center": [float(x) for x in c],
                         "mesh_radius": radius, "mesh_width": width}

    asset = "SK_" + sanitize(car_name)
    glb = os.path.join(out_dir, asset + ".glb")
    write_skinned_glb(glb, asset, prims, joints)
    write_calibration_glb(os.path.join(out_dir, "calibration.glb"))
    defaults = write_default_textures(os.path.join(out_dir, "textures", "_defaults"))
    for d in defaults.values():
        d["file"] = os.path.relpath(d["file"], out_dir).replace("\\", "/")

    # Collider (eight boxes on Kunos cars) -> a body box for the physics asset
    body_box = None
    col_path = os.path.join(folder, "collider.kn5")
    try:
        if os.path.isfile(col_path):
            col = read_kn5(col_path, load_textures=False)
            pts = [ _bake_mesh(n, w)[0] for n, w, _, _ in col.iter_nodes()
                    if n.is_mesh and _bake_mesh(n, w) is not None]
            if pts:
                cp = to_frame(np.concatenate(pts))
                lo, hi = cp.min(axis=0), cp.max(axis=0)
                body_box = {"center": ((lo + hi) / 2).tolist(), "size": (hi - lo).tolist(), "source": "collider.kn5"}
    except Kn5Error as e:
        log.warn(f"collider.kn5 not usable: {e}")
    if body_box is None:
        body_pos = np.concatenate([np.concatenate(gr["pos"]) for gr in groups.values()])
        lo, hi = body_pos.min(axis=0), body_pos.max(axis=0)
        lo[1] = max(lo[1], 0.12)  # keep the body box off the ground
        body_box = {"center": ((lo + hi) / 2).tolist(), "size": (hi - lo).tolist(), "source": "mesh bounds"}

    physics = read_physics(folder, log)
    for axle, keys in (("front", ("LF", "RF")), ("rear", ("LR", "RR"))):
        if drivable:
            if not physics[axle]["radius"]:
                physics[axle]["radius"] = wheels[keys[0]]["mesh_radius"]
            if not physics[axle]["width"]:
                physics[axle]["width"] = wheels[keys[0]]["mesh_width"]

    manifest = {
        "format": "ac2ue-car-manifest",
        "version": 1,
        "converter_version": __version__,
        "car": car_name,
        "car_asset_name": sanitize(car_name),
        "source": kn5_path,
        "frame": "glTF +X forward, +Y up, +Z right, meters, origin on the ground between the axles",
        "calibration_glb": "calibration.glb",
        "default_textures": defaults,
        "mesh": {"file": os.path.basename(glb), "asset": asset,
                 "sections": [p["material"] for p in prims],
                 "section_material_keys": [p["material_key"] for p in prims]},
        "drivable": drivable,
        "wheels": wheels,
        "body_box": body_box,
        "physics": physics,
        "textures": textures,
        "materials": materials,
        "default_skin": default_skin,
        "skins": skin_entries,
        "warnings": log.warnings,
    }
    path = os.path.join(out_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    tris = sum(len(p["indices"]) // 3 for p in prims)
    log.info(f"  wrote {asset}.glb: {len(prims)} material sections, {tris:,} triangles, "
             f"{'4 wheel bones' if drivable else 'no wheel bones'}; {len(skin_names)} skin(s); "
             f"physics from {physics['source']} ({time.time() - t0:.1f}s)")
    log.info(f"Done. Manifest: {path}")
    return path
