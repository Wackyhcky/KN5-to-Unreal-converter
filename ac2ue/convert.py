"""KN5 / AC track -> glTF meshes + PNG textures + manifest.json for Unreal."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import __version__
from .gltf_writer import write_glb, write_calibration_glb
from .kn5_reader import read_kn5, Kn5Error
from .materials import convert_material, texture_roles, ROLE_PRIORITY
from .track import load_track, read_surfaces, match_surface

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


def sanitize(name: str, max_len: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", os.path.splitext(name)[0] if name.lower().endswith(
        (".dds", ".png", ".jpg", ".jpeg", ".tga", ".bmp")) else name)
    s = re.sub(r"_+", "_", s).strip("_") or "unnamed"
    return s[:max_len]


class _Unique:
    def __init__(self):
        self.used = set()

    def __call__(self, base: str) -> str:
        name, i = base, 1
        while name.lower() in self.used:
            i += 1
            name = f"{base}_{i}"
        self.used.add(name.lower())
        return name


class Log:
    def __init__(self, stream=None, verbose=False):
        self.stream = stream or sys.stdout
        self.verbose = verbose
        self.warnings = []

    def info(self, msg):
        print(msg, file=self.stream, flush=True)

    def debug(self, msg):
        if self.verbose:
            self.info("  " + msg)

    def warn(self, msg):
        self.warnings.append(msg)
        self.info("  WARNING: " + msg)


# ----------------------------------------------------------------- textures

def _mean_linear(img) -> list:
    """Average colour of an image in linear space (what the shader would see)."""
    small = img.convert("RGB").resize((64, 64))
    a = np.asarray(small, dtype=np.float64) / 255.0
    lin = np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)
    return [round(float(c), 5) for c in lin.reshape(-1, 3).mean(axis=0)]


def _write_texture(data: bytes, base_path: str) -> tuple:
    """Convert texture bytes to PNG. Returns (written_path, converted_ok, mean_linear)."""
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
                if img.mode == "RGBA":
                    alpha = img.getchannel("A")
                    if alpha.getextrema() == (255, 255):
                        img = img.convert("RGB")
                path = base_path + ".png"
                img.save(path, "PNG", compress_level=1)
                return path, True, _mean_linear(img)
        except Exception:
            pass
    # Fall back to the raw file; Unreal can import many DDS variants itself.
    if data[:4] == b"DDS ":
        ext = ".dds"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif data[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    else:
        ext = ".bin"
    path = base_path + ext
    with open(path, "wb") as f:
        f.write(data)
    return path, ext in (".png", ".jpg"), None


def write_default_textures(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    defaults = {
        "White": ((255, 255, 255, 255), "color"),
        "WhiteLinear": ((255, 255, 255, 255), "mask"),
        "FlatNormal": ((128, 128, 255, 255), "normal"),
    }
    result = {}
    for name, (rgba, role) in defaults.items():
        path = os.path.join(out_dir, f"T_AC_{name}.png")
        if Image is not None:
            Image.new("RGB", (4, 4), rgba[:3]).save(path)
        result[name] = {"file": path, "asset": f"T_AC_{name}", "role": role}
    return result


# ------------------------------------------------------------------- meshes

def _bake_mesh(node, world: np.ndarray):
    """Transform a KN5 mesh to world space (glTF space).

    KN5 vertex data is right-handed, +Y up, meters, counter-clockwise front
    faces - the same conventions as glTF (this matches AcTools' Collada export,
    which writes positions unchanged, and the signed-volume measurements made
    by the independent assetto-corsa-gltf project). So no axis change is needed;
    mirroring here would produce a mirror-image track.
    """
    v = node.vertices
    idx = node.indices.astype(np.uint32)
    n = len(v)
    tri = (len(idx) // 3) * 3
    idx = idx[:tri]
    if n == 0 or tri == 0:
        return None
    if idx.max() >= n:
        tris = idx.reshape(-1, 3)
        tris = tris[(tris < n).all(axis=1)]
        idx = tris.reshape(-1)
        if idx.size == 0:
            return None

    pos = v["pos"].astype(np.float64)
    nrm = v["normal"].astype(np.float64)
    if not np.allclose(world, np.identity(4)):
        pos = pos @ world[:3, :3] + world[3, :3]
        lin = world[:3, :3]
        try:
            nrm = nrm @ np.linalg.inv(lin).T
        except np.linalg.LinAlgError:
            nrm = nrm @ lin
        det = np.linalg.det(lin)
    else:
        det = 1.0

    lengths = np.linalg.norm(nrm, axis=1, keepdims=True)
    bad = (lengths[:, 0] < 1e-8) | ~np.isfinite(lengths[:, 0])
    lengths[bad] = 1.0
    nrm = nrm / lengths
    nrm[bad] = (0.0, 1.0, 0.0)

    tris = idx.reshape(-1, 3).copy()
    if det < 0:  # mirrored node transform reverses winding
        tris = tris[:, [0, 2, 1]]

    uv = v["uv"].astype(np.float32)
    if not (np.isfinite(pos).all() and np.isfinite(uv).all()):
        pos = np.nan_to_num(pos)
        uv = np.nan_to_num(uv)
    return pos, nrm, uv, tris


def _winding_vote(pos, nrm, tris) -> float:
    """Area-weighted agreement between CCW face normals and stored vertex normals."""
    a, b, c = pos[tris[:, 0]], pos[tris[:, 1]], pos[tris[:, 2]]
    face = np.cross(b - a, c - a)  # CCW (glTF) face normal, length = 2*area
    vn = nrm[tris[:, 0]] + nrm[tris[:, 1]] + nrm[tris[:, 2]]
    return float(np.einsum("ij,ij->i", face, vn).sum())


# ---------------------------------------------------------------- per KN5

def convert_kn5(kn5_path: str, kn5_id: str, out_root: str, log: Log,
                include_inactive: bool = False, jobs: int = 4) -> dict:
    t0 = time.time()
    log.info(f"Reading {os.path.basename(kn5_path)} ...")
    kn5 = read_kn5(kn5_path)
    log.info(f"  KN5 v{kn5.version}: {len(kn5.textures)} textures, {len(kn5.materials)} materials")

    tex_dir = os.path.join(out_root, "textures", kn5_id)
    mesh_dir = os.path.join(out_root, "meshes", kn5_id)
    os.makedirs(tex_dir, exist_ok=True)
    os.makedirs(mesh_dir, exist_ok=True)

    # --- textures
    roles = texture_roles(kn5.materials)
    tex_unique = _Unique()
    textures = {}  # key "name|role" -> entry
    tex_data = {t.name: t.data for t in kn5.textures}

    jobs_list = []
    for tex in kn5.textures:
        base = tex_unique("T_" + sanitize(tex.name))
        jobs_list.append((tex.name, tex.data, os.path.join(tex_dir, base), base))

    def do_tex(job):
        name, data, base_path, asset = job
        path, ok, mean = _write_texture(data, base_path)
        return name, path, ok, asset, mean

    files = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for name, path, ok, asset, mean in pool.map(do_tex, jobs_list):
            files[name] = (path, asset, mean)
            if not ok:
                log.warn(f"texture '{name}' could not be decoded; kept as {os.path.basename(path)}")

    for name, (path, asset, mean) in files.items():
        tex_roles = sorted(roles.get(name, {"color"}), key=lambda r: ROLE_PRIORITY[r])
        for i, role in enumerate(tex_roles):
            role_asset, role_path = asset, path
            if i > 0:
                # Same image used with different colour-space settings: give the
                # extra role its own file so importers that name assets after the
                # file never overwrite one with the other.
                role_asset = tex_unique(f"{asset}_{role}")
                role_path = os.path.join(os.path.dirname(path), role_asset + os.path.splitext(path)[1])
                shutil.copyfile(path, role_path)
            textures[f"{name}|{role}"] = {
                "source_name": name,
                "file": os.path.relpath(role_path, out_root).replace("\\", "/"),
                "asset": role_asset,
                "role": role,
                "mean_linear": mean,
            }

    def lookup(tex_name, role):
        key = f"{tex_name}|{role}"
        if key in textures:
            return key
        if tex_name not in tex_data:
            log.debug(f"texture '{tex_name}' referenced but not embedded in this KN5")
        return None

    # --- materials
    mat_unique = _Unique()
    materials = {}
    mat_names = []
    for i, mat in enumerate(kn5.materials):
        entry = convert_material(mat, lookup)
        entry["name"] = mat.name
        entry["asset"] = mat_unique("MI_" + sanitize(mat.name))
        key = f"{i}:{mat.name}"
        materials[key] = entry
        mat_names.append(key)

    # --- meshes
    mesh_unique = _Unique()
    meshes = []
    skipped_inactive = 0
    baked = []
    for node, world, path, active in kn5.iter_nodes():
        if not node.is_mesh:
            continue
        if not active and not include_inactive:
            skipped_inactive += 1
            continue
        result = _bake_mesh(node, world)
        if result is None:
            log.debug(f"skipping empty mesh '{node.name}'")
            continue
        baked.append((node, path, active, result))

    vote = sum(_winding_vote(r[0], r[1], r[3]) for _, _, _, r in baked)
    flip_all = vote < 0
    log.debug(f"winding vote {vote:.3g} -> {'flip' if flip_all else 'keep'} triangle order")

    for node, path, active, (pos, nrm, uv, tris) in baked:
        if flip_all:
            tris = tris[:, [0, 2, 1]]
        lo, hi = pos.min(axis=0), pos.max(axis=0)
        center = (lo + hi) * 0.5
        local = (pos - center).astype(np.float32)
        asset = mesh_unique("SM_" + sanitize(node.name))
        glb_path = os.path.join(mesh_dir, asset + ".glb")
        write_glb(glb_path, asset, local, nrm.astype(np.float32), uv, tris.reshape(-1))
        mat_key = mat_names[node.material_id] if node.material_id < len(mat_names) else None
        if mat_key is None:
            log.warn(f"mesh '{node.name}' references missing material #{node.material_id}")
        # AC_START_0, AC_PIT_3, AC_TIME_0_L... are spawn/timing markers that AC
        # reads for positions and never draws. AC_POBJECT* are real movable props.
        helper = node.name.upper().startswith("AC_") and not node.name.upper().startswith("AC_POBJECT")
        meshes.append({
            "helper": helper,
            "name": node.name,
            "node_path": "/".join(path),
            "asset": asset,
            "file": os.path.relpath(glb_path, out_root).replace("\\", "/"),
            "material": mat_key,
            "center": [float(c) for c in center],
            "extent": [float(c) for c in (hi - lo)],
            "visible": bool(node.is_visible and active and not helper),
            "renderable": bool(node.is_renderable),
            "cast_shadows": bool(node.cast_shadows),
            "transparent": bool(node.is_transparent),
            "lod_in": float(node.lod_in),
            "lod_out": float(node.lod_out),
            "skinned": node.node_class == 3,
            "vertices": int(len(pos)),
            "triangles": int(len(tris)),
        })

    if skipped_inactive:
        log.info(f"  skipped {skipped_inactive} inactive meshes (use --include-inactive to keep them)")
    if any(m["skinned"] for m in meshes):
        log.warn("skinned meshes were imported as static meshes in their bind pose")

    tri_total = sum(m["triangles"] for m in meshes)
    log.info(f"  wrote {len(meshes)} meshes ({tri_total:,} triangles), "
             f"{len(files)} textures in {time.time() - t0:.1f}s")

    return {
        "source": os.path.abspath(kn5_path),
        "kn5_version": kn5.version,
        "winding_flipped": flip_all,
        "textures": textures,
        "materials": materials,
        "meshes": meshes,
    }


# ------------------------------------------------------------------ track

def _ac_to_gltf(v):
    return [float(v[0]), float(v[1]), float(v[2])]  # same axes, see _bake_mesh


def convert(input_path: str, out_dir: str, ac_root: str = None, layouts=None,
            include_inactive: bool = False, jobs: int = 4, verbose: bool = False,
            stream=None) -> str:
    log = Log(stream, verbose)
    if Image is None:
        log.warn("Pillow is not installed; textures will be copied raw (pip install Pillow)")

    system_surfaces = {}
    if ac_root:
        sys_ini = os.path.join(ac_root, "system", "data", "surfaces.ini")
        system_surfaces = read_surfaces(sys_ini)
        if system_surfaces:
            log.info(f"Loaded {len(system_surfaces)} system surfaces from {sys_ini}")
        else:
            log.warn(f"no surfaces found at {sys_ini}")

    track = load_track(input_path, system_surfaces, layouts)
    log.info(f"Track '{track.name}': {len(track.layouts)} layout(s): "
             + ", ".join(l.name for l in track.layouts))

    os.makedirs(out_dir, exist_ok=True)
    calib = os.path.join(out_dir, "calibration.glb")
    write_calibration_glb(calib)
    defaults = write_default_textures(os.path.join(out_dir, "textures", "_defaults"))
    for d in defaults.values():
        d["file"] = os.path.relpath(d["file"], out_dir).replace("\\", "/")

    kn5_ids = {}
    id_unique = _Unique()
    kn5_entries = {}
    failed = {}
    for layout in track.layouts:
        for model in layout.models:
            key = os.path.normcase(os.path.abspath(model.kn5_path))
            if key in kn5_ids or key in failed:
                continue
            if not os.path.isfile(model.kn5_path):
                failed[key] = "file not found"
                log.warn(f"layout '{layout.name}' references missing {model.kn5_path}")
                continue
            kid = id_unique(sanitize(os.path.splitext(os.path.basename(model.kn5_path))[0]))
            try:
                kn5_entries[kid] = convert_kn5(model.kn5_path, kid, out_dir, log,
                                               include_inactive, jobs)
                kn5_ids[key] = kid
            except Kn5Error as e:
                failed[key] = str(e)
                log.warn(str(e))

    if not kn5_entries:
        raise Kn5Error("No KN5 files could be converted. " + "; ".join(failed.values()))

    layout_entries = []
    level_unique = _Unique()
    for layout in track.layouts:
        keys = [k for k in layout.surfaces]
        models = []
        surface_counts = {}
        for model in layout.models:
            kid = kn5_ids.get(os.path.normcase(os.path.abspath(model.kn5_path)))
            if kid is None:
                continue
            surf_map = {}
            for i, mesh in enumerate(kn5_entries[kid]["meshes"]):
                s = match_surface(mesh["name"], keys)
                if s:
                    surf_map[str(i)] = s
                    surface_counts[s] = surface_counts.get(s, 0) + 1
            if any(abs(r) > 1e-6 for r in model.rotation):
                log.warn(f"layout '{layout.name}': ROTATION={model.rotation} on "
                         f"{os.path.basename(model.kn5_path)} is not applied (position is)")
            models.append({
                "kn5": kid,
                "position": _ac_to_gltf(model.position),
                "rotation_ac": model.rotation,
                "surfaces": surf_map,
            })
        used = {k: v for k, v in layout.surfaces.items() if k in surface_counts}
        unknown = 0
        for m in models:
            for i, mesh in enumerate(kn5_entries[m["kn5"]]["meshes"]):
                if str(i) not in m["surfaces"] and re.match(r"^\d+[A-Z]", mesh["name"]):
                    unknown += 1
        if unknown:
            log.warn(f"layout '{layout.name}': {unknown} meshes look like physics surfaces but "
                     "match no surface key (pass --ac-root to load AC's system surfaces)")
        log.info(f"Layout '{layout.name}': {len(models)} model(s), surfaces "
                 + (", ".join(f"{k}x{v}" for k, v in sorted(surface_counts.items())) or "none"))
        layout_entries.append({
            "name": layout.name,
            "level": level_unique(f"L_{sanitize(track.name)}_{sanitize(layout.name)}"),
            "surfaces_ini": layout.surfaces_path if layout.surfaces_path and os.path.isfile(layout.surfaces_path) else None,
            "surfaces": used,
            "models": models,
        })

    manifest = {
        "format": "ac2ue-manifest",
        "version": 1,
        "converter_version": __version__,
        "track": track.name,
        "track_asset_name": sanitize(track.name),
        "source": track.root,
        "space": "glTF (right-handed, +Y up, meters); mesh vertices are relative to 'center'",
        "calibration_glb": "calibration.glb",
        "default_textures": defaults,
        "kn5": kn5_entries,
        "layouts": layout_entries,
        "warnings": log.warnings,
        "failed_kn5": failed,
    }
    path = os.path.join(out_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    log.info(f"Done. Manifest: {path}")
    if log.warnings:
        log.info(f"{len(log.warnings)} warning(s); see 'warnings' in the manifest.")
    return path
