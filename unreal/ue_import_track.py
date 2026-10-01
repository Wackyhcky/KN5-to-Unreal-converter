"""Import a converted Assetto Corsa track into Unreal Engine 5.

Run inside the Unreal Editor (Python Editor Script Plugin enabled). In the
Output Log, make sure the input box is set to "Cmd" and run:

    py "C:/path/to/kn5_to_unreal/unreal/ue_import_track.py" "C:/path/to/out/mytrack/manifest.json"

Options (append after the manifest path):
    --dest /Game/ACTracks     content folder to import into
    --layouts gp,national     only build these layouts
    --nanite                  enable Nanite on opaque/masked meshes
    --no-lighting             don't add sun/sky/fog actors to new levels
    --reuse-existing          skip re-importing meshes/textures that already exist
    --rebuild-masters         recreate the shared master materials

What it builds:
    <dest>/_Shared/Masters        M_AC_Opaque / Masked / Translucent / Multilayer
    <dest>/<track>/Textures/...   imported textures with correct sRGB/compression
    <dest>/<track>/Materials/...  one material instance per KN5 material
    <dest>/<track>/Meshes/...     one static mesh per KN5 mesh, complex-as-simple collision
    <dest>/<track>/Physics/...    PM_AC_<KEY> physical materials from surfaces.ini
    <dest>/<track>/Levels/...     one level per layout with every mesh placed
"""

import argparse
import json
import os
import sys

import unreal

ASSET_TOOLS = unreal.AssetToolsHelpers.get_asset_tools()
EAL = unreal.EditorAssetLibrary
MEL = unreal.MaterialEditingLibrary

TAG = "AC2UE"
IMPORT_BATCH = 40


# --------------------------------------------------------------------- utils

def log(msg):
    unreal.log("[AC2UE] " + msg)


def warn(msg):
    unreal.log_warning("[AC2UE] " + msg)


def subsystem(cls_name):
    cls = getattr(unreal, cls_name, None)
    return unreal.get_editor_subsystem(cls) if cls else None


def obj_path(package_path):
    """'/Game/A/B' -> '/Game/A/B.B'"""
    if "." in package_path.rsplit("/", 1)[-1]:
        return package_path
    name = package_path.rsplit("/", 1)[-1]
    return package_path + "." + name


def load(package_path):
    if EAL.does_asset_exist(package_path):
        return EAL.load_asset(package_path)
    return None


def set_props(obj, **props):
    for k, v in props.items():
        try:
            obj.set_editor_property(k, v)
        except Exception as e:  # noqa: BLE001
            warn("could not set %s.%s: %s" % (obj.get_name(), k, e))


def create_or_load(name, folder, cls, factory):
    existing = load(folder + "/" + name)
    if existing is not None:
        return existing, False
    return ASSET_TOOLS.create_asset(name, folder, cls, factory), True


def import_files(items, label, reuse_existing=False):
    """items: list of (filename, dest_folder, dest_name). Returns list of object path lists."""
    results = [None] * len(items)
    todo = []
    for i, (filename, dest, name) in enumerate(items):
        if reuse_existing and EAL.does_asset_exist(dest + "/" + name):
            results[i] = [obj_path(dest + "/" + name)]
        elif not os.path.isfile(filename):
            warn("missing file " + filename)
            results[i] = []
        else:
            todo.append(i)

    with unreal.ScopedSlowTask(max(1, len(todo)), label) as task:
        task.make_dialog(True)
        for start in range(0, len(todo), IMPORT_BATCH):
            if task.should_cancel():
                raise RuntimeError("Cancelled by user")
            chunk = todo[start:start + IMPORT_BATCH]
            tasks = []
            for i in chunk:
                filename, dest, name = items[i]
                t = unreal.AssetImportTask()
                t.set_editor_property("filename", filename)
                t.set_editor_property("destination_path", dest)
                t.set_editor_property("destination_name", name)
                t.set_editor_property("automated", True)
                t.set_editor_property("replace_existing", True)
                t.set_editor_property("save", False)
                tasks.append(t)
            ASSET_TOOLS.import_asset_tasks(tasks)
            for i, t in zip(chunk, tasks):
                paths = []
                try:
                    paths = [str(p) for p in t.get_editor_property("imported_object_paths")]
                except Exception:  # noqa: BLE001
                    pass
                if not paths:
                    paths = _find_imported(items[i][1], items[i][2])
                results[i] = paths
            task.enter_progress_frame(len(chunk), "%s (%d/%d)" % (label, min(start + len(chunk), len(todo)), len(todo)))
    return results


def _find_imported(dest, name):
    """Fallback when an importer doesn't report its outputs: match by name in dest."""
    if EAL.does_asset_exist(dest + "/" + name):
        return [obj_path(dest + "/" + name)]
    found = []
    for p in EAL.list_assets(dest, recursive=True, include_folder=False):
        base = str(p).rsplit("/", 1)[-1].split(".")[0]
        if base.lower() == name.lower() or base.lower().endswith(name.lower()):
            found.append(str(p))
    return found


def pick(paths, cls):
    for p in paths:
        a = EAL.load_asset(p.split(".")[0]) if EAL.does_asset_exist(p.split(".")[0]) else None
        if a is not None and isinstance(a, cls):
            return a
    return None


def cleanup_extras(paths, keep, folder):
    """Delete side assets (materials/textures) an importer created next to a mesh."""
    for p in paths:
        pkg = p.split(".")[0]
        if not pkg.startswith(folder + "/") or not EAL.does_asset_exist(pkg):
            continue
        a = EAL.load_asset(pkg)
        if a is None or a == keep:
            continue
        if isinstance(a, (unreal.MaterialInterface, unreal.Texture)):
            EAL.delete_asset(pkg)


# ------------------------------------------------------------- calibration

def calibrate(manifest_dir, manifest, dest):
    """Learn the importer's glTF->Unreal axis mapping from a known tetrahedron."""
    folder = dest + "/_Shared/_Calibration"
    paths = import_files([(os.path.join(manifest_dir, manifest["calibration_glb"]), folder,
                           "AC2UE_Calibration")], "Calibrating glTF axes")[0]
    mesh = pick(paths, unreal.StaticMesh)
    if mesh is None:
        raise RuntimeError("Could not import calibration.glb. Is the glTF importer "
                           "(Interchange) enabled in this project?")
    box = mesh.get_bounding_box()
    lo = [box.min.x, box.min.y, box.min.z]
    hi = [box.max.x, box.max.y, box.max.z]
    vals = [hi[i] if abs(hi[i]) >= abs(lo[i]) else lo[i] for i in range(3)]
    scale = max(abs(v) for v in vals) / 3.0
    matrix = [[0.0, 0.0, 0.0] for _ in range(3)]
    used = set()
    for ue_axis, v in enumerate(vals):
        k = int(round(abs(v) / scale)) if scale > 0 else 0
        if k not in (1, 2, 3) or k in used:
            raise RuntimeError("Unexpected calibration bounds %s / %s" % (lo, hi))
        used.add(k)
        matrix[ue_axis][k - 1] = scale if v > 0 else -scale
    for p in paths:
        pkg = p.split(".")[0]
        if pkg.startswith(folder + "/") and EAL.does_asset_exist(pkg):
            EAL.delete_asset(pkg)
    log("glTF->UE mapping: X=%s Y=%s Z=%s (scale %.3g)" % (matrix[0], matrix[1], matrix[2], scale))
    return matrix, scale


def to_ue(matrix, v):
    return unreal.Vector(*[sum(matrix[r][c] * v[c] for c in range(3)) for r in range(3)])


# ---------------------------------------------------------- master materials

MASTER_PARAMS = {
    "Opaque": {"tex": ["Diffuse", "Detail", "Maps", "Normal"],
               "scalar": ["DetailTiling", "UseDetail", "Roughness", "Specular"],
               "vector": ["EmissiveColor", "Tint"]},
    "Masked": {"tex": ["Diffuse", "Detail", "Maps", "Normal"],
               "scalar": ["DetailTiling", "UseDetail", "Roughness", "Specular", "Dither"],
               "vector": ["EmissiveColor", "Tint"]},
    "Translucent": {"tex": ["Diffuse", "Detail", "Maps", "Normal"],
                    "scalar": ["DetailTiling", "UseDetail", "Roughness", "Specular", "Opacity"],
                    "vector": ["EmissiveColor", "Tint"]},
    "Multilayer": {"tex": ["Diffuse", "Mask", "DetailR", "DetailG", "DetailB", "DetailA", "Maps", "Normal"],
                   "scalar": ["MultR", "MultG", "MultB", "MultA", "UseMeanR", "UseMeanG", "UseMeanB",
                              "UseMeanA", "Roughness", "Specular"],
                   "vector": ["EmissiveColor", "Tint", "MeanR", "MeanG", "MeanB", "MeanA"]},
}

# Bump when the master graphs change; existing masters are then rebuilt in
# place (same asset, so material instances keep pointing at them).
MASTER_VERSION = "3"
DITHER_FUNCTION_PATHS = (
    "/Engine/Functions/Engine_MaterialFunctions02/Utility/DitherTemporalAA",
    "/Engine/Functions/Engine_MaterialFunctions02/DitherTemporalAA",
)
MEAN_SLOTS = {"DetailR": "MeanR", "DetailG": "MeanG", "DetailB": "MeanB", "DetailA": "MeanA"}


class _Graph:
    def __init__(self, mat):
        self.mat = mat
        self.y = 0

    def node(self, cls, x=-600, **props):
        self.y += 40
        e = MEL.create_material_expression(self.mat, cls, x, self.y)
        for k, v in props.items():
            e.set_editor_property(k, v)
        return e

    def link(self, a, a_out, b, b_in):
        if not MEL.connect_material_expressions(a, a_out, b, b_in):
            warn("%s: failed to connect %s.%s -> %s.%s" % (self.mat.get_name(), a.get_name(), a_out,
                                                            b.get_name(), b_in))

    def out(self, a, a_out, prop):
        if not MEL.connect_material_property(a, a_out, prop):
            warn("%s: failed to connect %s -> %s" % (self.mat.get_name(), a_out, prop))

    def tex(self, name, texture, sampler, uv=None):
        e = self.node(unreal.MaterialExpressionTextureSampleParameter2D, -900,
                      parameter_name=name, texture=texture, sampler_type=sampler)
        if uv is not None:
            self.link(uv, "", e, "UVs")
        return e

    def scalar(self, name, value):
        return self.node(unreal.MaterialExpressionScalarParameter, -1200,
                         parameter_name=name, default_value=value)

    def vector_rgb(self, name, rgba):
        v = self.node(unreal.MaterialExpressionVectorParameter, -1200,
                      parameter_name=name, default_value=unreal.LinearColor(*rgba))
        m = self.node(unreal.MaterialExpressionComponentMask, -1000, r=True, g=True, b=True, a=False)
        self.link(v, "", m, "")
        return m

    def mul(self, a, a_out, b, b_out):
        e = self.node(unreal.MaterialExpressionMultiply, -400)
        self.link(a, a_out, e, "A")
        self.link(b, b_out, e, "B")
        return e

    def add(self, a, b, a_out="", b_out=""):
        e = self.node(unreal.MaterialExpressionAdd, -300)
        self.link(a, a_out, e, "A")
        self.link(b, b_out, e, "B")
        return e

    def div(self, a, b):
        e = self.node(unreal.MaterialExpressionDivide, -300)
        self.link(a, "", e, "A")
        self.link(b, "", e, "B")
        return e

    def max_const(self, a, const):
        e = self.node(unreal.MaterialExpressionMax, -300, const_b=const)
        self.link(a, "", e, "A")
        return e

    def dither(self, src, src_out):
        """Engine 'DitherTemporalAA' material function applied to src.

        It is a material function, not an expression class, so it goes in
        through a function-call node. Returns None if this engine version
        doesn't have it; the caller then uses a plain alpha cut-out.
        """
        func = None
        for path in DITHER_FUNCTION_PATHS:
            if EAL.does_asset_exist(path):
                func = EAL.load_asset(path)
                break
        if func is None:
            warn("DitherTemporalAA material function not found; grass shells use a plain cut-out")
            return None
        call = self.node(unreal.MaterialExpressionMaterialFunctionCall, -400)
        try:
            call.set_material_function(func)
        except Exception:  # noqa: BLE001
            call.set_editor_property("material_function", func)
        self.link(src, src_out, call, "")
        return call

    def saturate(self, a):
        e = self.node(unreal.MaterialExpressionSaturate, -300)
        self.link(a, "", e, "")
        return e

    def lerp(self, a, a_out, b, b_out, alpha, alpha_out, const_a=None, const_b=None):
        e = self.node(unreal.MaterialExpressionLinearInterpolate, -300)
        if a is not None:
            self.link(a, a_out, e, "A")
        else:
            e.set_editor_property("const_a", const_a)
        if b is not None:
            self.link(b, b_out, e, "B")
        else:
            e.set_editor_property("const_b", const_b)
        self.link(alpha, alpha_out, e, "Alpha")
        return e


def build_master(kind, folder, defaults, rebuild):
    name = "M_AC_" + kind
    path = folder + "/" + name
    if EAL.does_asset_exist(path):
        mat = EAL.load_asset(path)
        if not rebuild and EAL.get_metadata_tag(mat, "AC2UE_MASTER_VERSION") == MASTER_VERSION:
            return mat
        MEL.delete_all_material_expressions(mat)
        log("rebuilding master material %s (v%s)" % (name, MASTER_VERSION))
    else:
        mat = ASSET_TOOLS.create_asset(name, folder, unreal.Material, unreal.MaterialFactoryNew())
    g = _Graph(mat)
    ST = unreal.MaterialSamplerType
    MP = unreal.MaterialProperty
    white, white_lin, flat = defaults["White"], defaults["WhiteLinear"], defaults["FlatNormal"]

    uv = g.node(unreal.MaterialExpressionTextureCoordinate, -1400, coordinate_index=0)
    diffuse = g.tex("Diffuse", white, ST.SAMPLERTYPE_COLOR)

    if kind == "Multilayer":
        # detail = sum(layer_i * mask_i) / sum(mask_i), faded to neutral where
        # the mask is empty; a layer can use its average colour instead of the
        # texture (object-space shaders, zero tiling).
        # AC multilayer details are projected from world position in metres
        # (multR 0.8 on asphalt = a ~1.25 m tile); the mesh UVs only carry the
        # large-scale diffuse and mask.
        wp = g.node(unreal.MaterialExpressionWorldPosition, -1600)
        wxy = g.node(unreal.MaterialExpressionComponentMask, -1500, r=True, g=True, b=False, a=False)
        g.link(wp, "", wxy, "")
        metres = g.node(unreal.MaterialExpressionMultiply, -1450, const_b=0.01)
        g.link(wxy, "", metres, "A")
        mask = g.tex("Mask", white_lin, ST.SAMPLERTYPE_MASKS)
        acc = None
        for ch in "RGBA":
            uvc = g.mul(metres, "", g.scalar("Mult" + ch, 1.0), "")
            d = g.tex("Detail" + ch, white, ST.SAMPLERTYPE_COLOR, uvc)
            layer = g.lerp(d, "RGB", g.vector_rgb("Mean" + ch, (0.5, 0.5, 0.5, 1)), "",
                           g.scalar("UseMean" + ch, 0.0), "")
            w = g.mul(layer, "", mask, ch)
            acc = w if acc is None else g.add(acc, w)
            if ch == "R":
                wsum_node, wsum_out = mask, "R"
            else:
                wsum_node, wsum_out = g.add(wsum_node, mask, wsum_out, ch), ""
        norm = g.div(acc, g.max_const(wsum_node, 0.001))
        detail = g.lerp(None, None, norm, "", g.saturate(wsum_node), "", const_a=1.0)
        base = g.mul(diffuse, "RGB", detail, "")
    else:
        duv = g.mul(uv, "", g.scalar("DetailTiling", 1.0), "")
        detail = g.tex("Detail", white, ST.SAMPLERTYPE_COLOR, duv)
        # AC detail maps are neutral at mid-grey: they enter as detail * 2.
        detail2 = g.node(unreal.MaterialExpressionMultiply, -400, const_b=2.0)
        g.link(detail, "RGB", detail2, "A")
        if kind == "Opaque":
            # AC multimap: detail shows where the diffuse alpha is dark.
            dmask = g.lerp(detail2, "", None, None, diffuse, "A", const_b=1.0)
            dmask_out = ""
        else:
            dmask, dmask_out = detail2, ""
        factor = g.lerp(None, None, dmask, dmask_out, g.scalar("UseDetail", 0.0), "", const_a=1.0)
        base = g.mul(diffuse, "RGB", factor, "")

    base = g.mul(base, "", g.vector_rgb("Tint", (1, 1, 1, 1)), "")
    g.out(base, "", MP.MP_BASE_COLOR)

    maps = g.tex("Maps", white_lin, ST.SAMPLERTYPE_MASKS)
    rough = g.lerp(None, None, g.scalar("Roughness", 0.6), "", maps, "G", const_a=1.0)
    g.out(rough, "", MP.MP_ROUGHNESS)
    spec = g.mul(g.scalar("Specular", 0.5), "", maps, "R")
    g.out(spec, "", MP.MP_SPECULAR)

    normal = g.tex("Normal", flat, ST.SAMPLERTYPE_NORMAL)
    g.out(normal, "RGB", MP.MP_NORMAL)

    emissive = g.mul(diffuse, "RGB", g.vector_rgb("EmissiveColor", (0, 0, 0, 1)), "")
    g.out(emissive, "", MP.MP_EMISSIVE_COLOR)

    if kind == "Masked":
        dither = g.dither(diffuse, "A")
        if dither is not None:
            opacity = g.lerp(diffuse, "A", dither, "", g.scalar("Dither", 0.0), "")
            g.out(opacity, "", MP.MP_OPACITY_MASK)
        else:
            g.scalar("Dither", 0.0)  # keep the parameter so instances still accept it
            g.out(diffuse, "A", MP.MP_OPACITY_MASK)
        set_props(mat, blend_mode=unreal.BlendMode.BLEND_MASKED, two_sided=True,
                  opacity_mask_clip_value=0.5)
    elif kind == "Translucent":
        op = g.mul(diffuse, "A", g.scalar("Opacity", 1.0), "")
        g.out(op, "", MP.MP_OPACITY)
        set_props(mat, blend_mode=unreal.BlendMode.BLEND_TRANSLUCENT)

    MEL.layout_material_expressions(mat)
    MEL.recompile_material(mat)
    EAL.set_metadata_tag(mat, "AC2UE_MASTER_VERSION", MASTER_VERSION)
    EAL.save_loaded_asset(mat)
    log("built master material " + name)
    return mat


# -------------------------------------------------------------------- import

ROLE_SETTINGS = {
    "color": (True, "TC_DEFAULT"),
    "mask": (False, "TC_MASKS"),
    "normal": (False, "TC_NORMALMAP"),
}


def configure_texture(tex, role):
    srgb, comp = ROLE_SETTINGS[role]
    set_props(tex, srgb=srgb,
              compression_settings=getattr(unreal.TextureCompressionSettings, comp))
    if role == "normal":
        set_props(tex, flip_green_channel=False)


def import_defaults(manifest_dir, manifest, folder, reuse):
    items, roles = [], []
    for key, d in manifest["default_textures"].items():
        items.append((os.path.join(manifest_dir, d["file"]), folder, d["asset"]))
        roles.append((key, d["role"]))
    out = {}
    for (key, role), paths in zip(roles, import_files(items, "Importing default textures", reuse)):
        tex = pick(paths, unreal.Texture2D)
        if tex is None:
            raise RuntimeError("could not import default texture " + key)
        configure_texture(tex, role)
        out[key] = tex
    return out


def import_textures(manifest_dir, kn5, folder, reuse):
    keys = list(kn5["textures"].keys())
    items = [(os.path.join(manifest_dir, kn5["textures"][k]["file"]), folder, kn5["textures"][k]["asset"])
             for k in keys]
    result = {}
    for key, paths in zip(keys, import_files(items, "Importing textures", reuse)):
        tex = pick(paths, unreal.Texture2D)
        if tex is None:
            warn("texture %s failed to import; using a default" % kn5["textures"][key]["source_name"])
            continue
        configure_texture(tex, kn5["textures"][key]["role"])
        result[key] = tex
    return result


def build_material_instances(kn5, textures, masters, folder):
    result = {}
    with unreal.ScopedSlowTask(max(1, len(kn5["materials"])), "Creating material instances") as task:
        task.make_dialog(False)
        for key, m in kn5["materials"].items():
            task.enter_progress_frame(1, m["name"])
            parent = masters[m["master"]]
            mi, _ = create_or_load(m["asset"], folder, unreal.MaterialInstanceConstant,
                                   unreal.MaterialInstanceConstantFactoryNew())
            MEL.set_material_instance_parent(mi, parent)
            allowed = MASTER_PARAMS[m["master"]]
            for param, tex_key in m["textures"].items():
                if param in allowed["tex"] and tex_key in textures:
                    MEL.set_material_instance_texture_parameter_value(mi, param, textures[tex_key])
            for param, value in m["scalars"].items():
                if param in allowed["scalar"]:
                    MEL.set_material_instance_scalar_parameter_value(mi, param, float(value))
            for param, rgba in m["vectors"].items():
                if param in allowed["vector"]:
                    MEL.set_material_instance_vector_parameter_value(mi, param, unreal.LinearColor(*rgba))
            # Average colour of each detail layer, used where a layer can't be
            # tiled faithfully (see UseMean* in the converter).
            for slot, mean_param in MEAN_SLOTS.items():
                tex_key = m["textures"].get(slot)
                mean = kn5["textures"].get(tex_key, {}).get("mean_linear") if tex_key else None
                if mean_param in allowed["vector"] and mean:
                    MEL.set_material_instance_vector_parameter_value(
                        mi, mean_param, unreal.LinearColor(mean[0], mean[1], mean[2], 1.0))
            set_clip_override(mi, m.get("opacity_clip"))
            MEL.update_material_instance(mi)
            result[key] = mi
    return result


def set_clip_override(mi, clip):
    """Per-instance alpha-test threshold (AC's ksAlphaRef), when it is usable."""
    if clip is None:
        return
    try:
        ov = mi.get_editor_property("base_property_overrides")
        ov.set_editor_property("override_opacity_mask_clip_value", True)
        ov.set_editor_property("opacity_mask_clip_value", float(clip))
        mi.set_editor_property("base_property_overrides", ov)
    except Exception as e:  # noqa: BLE001
        warn("could not set clip value on %s: %s" % (mi.get_name(), e))


def setup_collision(mesh):
    smes = subsystem("StaticMeshEditorSubsystem")
    try:
        if smes is not None:
            smes.remove_collisions(mesh)
        else:
            unreal.EditorStaticMeshLibrary.remove_collisions(mesh)
    except Exception:  # noqa: BLE001
        pass
    try:
        body = mesh.get_editor_property("body_setup")
        if body is not None:
            body.set_editor_property("collision_trace_flag",
                                     unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
    except Exception as e:  # noqa: BLE001
        warn("could not set complex collision on %s: %s" % (mesh.get_name(), e))


def enable_nanite(mesh):
    try:
        ns = mesh.get_editor_property("nanite_settings")
        ns.set_editor_property("enabled", True)
        mesh.set_editor_property("nanite_settings", ns)
    except Exception as e:  # noqa: BLE001
        warn("could not enable Nanite on %s: %s" % (mesh.get_name(), e))


def import_meshes(manifest_dir, kn5, mats, masters_by_key, folder, reuse, nanite):
    items = [(os.path.join(manifest_dir, m["file"]), folder, m["asset"]) for m in kn5["meshes"]]
    results = import_files(items, "Importing meshes", reuse)
    meshes = []
    with unreal.ScopedSlowTask(max(1, len(results)), "Configuring meshes") as task:
        task.make_dialog(False)
        for m, paths in zip(kn5["meshes"], results):
            task.enter_progress_frame(1, m["asset"])
            mesh = pick(paths, unreal.StaticMesh)
            if mesh is None:
                warn("mesh %s failed to import" % m["name"])
                meshes.append(None)
                continue
            cleanup_extras(paths, mesh, folder)
            mi = mats.get(m["material"])
            if mi is not None:
                mesh.set_material(0, mi)
            setup_collision(mesh)
            if nanite and kn5["materials"].get(m["material"], {}).get("master") != "Translucent":
                enable_nanite(mesh)
            meshes.append(mesh)
    return meshes


def build_physical_materials(manifest, folder):
    """One PM per distinct (surface key, properties); returns {(layout, KEY): PhysicalMaterial}."""
    variants = {}
    for layout in manifest["layouts"]:
        for key, props in layout["surfaces"].items():
            sig = json.dumps({k: v for k, v in props.items() if not k.startswith("_")}, sort_keys=True)
            variants.setdefault(key, {}).setdefault(sig, []).append(layout["name"])

    result = {}
    for key, sigs in variants.items():
        for i, (sig, layouts) in enumerate(sorted(sigs.items())):
            name = "PM_AC_" + key if len(sigs) == 1 else "PM_AC_%s_%s" % (key, layouts[0])
            props = json.loads(sig)
            pm, _ = create_or_load(name, folder, unreal.PhysicalMaterial, unreal.PhysicalMaterialFactoryNew())
            friction = float(props.get("FRICTION", 0.7))
            set_props(pm, friction=friction)
            try:
                pm.set_editor_property("static_friction", friction)
            except Exception:  # noqa: BLE001
                pass
            for k, v in props.items():
                EAL.set_metadata_tag(pm, "AC_" + k, str(v))
            EAL.save_loaded_asset(pm)
            for l in layouts:
                result[(l, key)] = pm
    return result


def surface_mi(base_mi, key, pm, folder, cache):
    ck = (base_mi.get_name(), pm.get_name())
    if ck in cache:
        return cache[ck]
    name = "%s__%s" % (base_mi.get_name(), pm.get_name().replace("PM_AC_", ""))
    mi, _ = create_or_load(name, folder, unreal.MaterialInstanceConstant,
                           unreal.MaterialInstanceConstantFactoryNew())
    MEL.set_material_instance_parent(mi, base_mi)
    set_props(mi, phys_material=pm)
    MEL.update_material_instance(mi)
    cache[ck] = mi
    return mi


# --------------------------------------------------------------------- levels

def open_or_create_level(path):
    les = subsystem("LevelEditorSubsystem")
    if EAL.does_asset_exist(path):
        les.load_level(path)
        eas = subsystem("EditorActorSubsystem")
        for a in eas.get_all_level_actors():
            if TAG in [str(t) for t in a.get_editor_property("tags")]:
                eas.destroy_actor(a)
        return False
    if not les.new_level(path):
        raise RuntimeError("could not create level " + path)
    return True


def add_lighting():
    eas = subsystem("EditorActorSubsystem")
    sun = eas.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 5000),
                                     unreal.Rotator(0, -40, 30))
    try:
        sun.get_editor_property("directional_light_component").set_editor_property("atmosphere_sun_light", True)
    except Exception:  # noqa: BLE001
        pass
    sky = eas.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(0, 0, 5000), unreal.Rotator(0, 0, 0))
    try:
        comp = sky.get_editor_property("light_component")
        comp.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
        comp.set_editor_property("real_time_capture", True)
    except Exception:  # noqa: BLE001
        pass
    eas.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
    eas.spawn_actor_from_class(unreal.ExponentialHeightFog, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
    for a in (sun, sky):
        a.set_folder_path("AC/Lighting")


def build_level(layout, manifest, kn5_assets, phys, matrix, scale, level_path, surf_folder, lighting):
    created = open_or_create_level(level_path)
    if created and lighting:
        add_lighting()
    eas = subsystem("EditorActorSubsystem")
    smi_cache = {}
    total = sum(len(manifest["kn5"][m["kn5"]]["meshes"]) for m in layout["models"])
    spawned = 0
    with unreal.ScopedSlowTask(max(1, total), "Placing layout '%s'" % layout["name"]) as task:
        task.make_dialog(True)
        for model in layout["models"]:
            kid = model["kn5"]
            kn5 = manifest["kn5"][kid]
            meshes, mats = kn5_assets[kid]
            offset = model["position"]
            for i, m in enumerate(kn5["meshes"]):
                task.enter_progress_frame(1)
                if task.should_cancel():
                    raise RuntimeError("Cancelled by user")
                mesh = meshes[i]
                if mesh is None:
                    continue
                c = [m["center"][j] + offset[j] for j in range(3)]
                actor = eas.spawn_actor_from_object(mesh, to_ue(matrix, c), unreal.Rotator(0, 0, 0))
                if actor is None:
                    continue
                spawned += 1
                actor.set_actor_label(m["name"])
                comp = actor.get_editor_property("static_mesh_component")
                key = model["surfaces"].get(str(i))
                tags = [TAG, "AC_KN5=" + kid]
                if key:
                    pm = phys.get((layout["name"], key))
                    props = layout["surfaces"].get(key, {})
                    tags += ["AC_SURFACE", "AC_SURFACE=" + key,
                             "AC_VALID_TRACK=%d" % int(float(props.get("IS_VALID_TRACK", 0)))]
                    comp.set_collision_profile_name("BlockAll")
                    base_mi = mats.get(m["material"])
                    if pm is not None and base_mi is not None:
                        comp.set_material(0, surface_mi(base_mi, key, pm, surf_folder, smi_cache))
                    actor.set_folder_path("AC/%s/Surfaces/%s" % (kid, key))
                else:
                    comp.set_collision_profile_name("NoCollision")
                    actor.set_folder_path("AC/%s/Visual" % kid)
                if m.get("helper"):
                    tags.append("AC_HELPER")
                if not (m["visible"] and m["renderable"]):
                    # AC never draws these (typically the physics surfaces and
                    # walls); hide them in the editor too. Collision stays on.
                    actor.set_actor_hidden_in_game(True)
                    try:
                        comp.set_visibility(False, False)
                    except Exception:  # noqa: BLE001
                        comp.set_editor_property("visible", False)
                    comp.set_cast_shadow(False)
                    tags.append("AC_HIDDEN")
                    actor.set_folder_path("AC/%s/Hidden%s" % (kid, "/" + key if key else ""))
                if not m["cast_shadows"]:
                    comp.set_cast_shadow(False)
                if m["lod_out"] > 0:
                    comp.set_editor_property("ld_max_draw_distance", float(m["lod_out"]) * scale)
                actor.set_editor_property("tags", [unreal.Name(t) for t in tags])
    subsystem("LevelEditorSubsystem").save_current_level()
    log("level %s: placed %d actors" % (level_path, spawned))


# ---------------------------------------------------------------------- main

def main(argv):
    p = argparse.ArgumentParser(prog="ue_import_track.py")
    p.add_argument("manifest")
    p.add_argument("--dest", default="/Game/ACTracks")
    p.add_argument("--layouts")
    p.add_argument("--nanite", action="store_true")
    p.add_argument("--no-lighting", action="store_true")
    p.add_argument("--reuse-existing", action="store_true")
    p.add_argument("--rebuild-masters", action="store_true")
    args = p.parse_args(argv)

    manifest_path = os.path.abspath(args.manifest)
    manifest_dir = os.path.dirname(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("format") != "ac2ue-manifest":
        raise RuntimeError("not an ac2ue manifest: " + manifest_path)

    dest = args.dest.rstrip("/")
    shared = dest + "/_Shared"
    root = "%s/%s" % (dest, manifest["track_asset_name"])
    log("importing track '%s' into %s" % (manifest["track"], root))

    matrix, scale = calibrate(manifest_dir, manifest, dest)
    defaults = import_defaults(manifest_dir, manifest, shared + "/DefaultTextures", True)
    masters = {k: build_master(k, shared + "/Masters", defaults, args.rebuild_masters)
               for k in MASTER_PARAMS}

    layouts = manifest["layouts"]
    if args.layouts:
        wanted = {l.strip().lower() for l in args.layouts.split(",")}
        layouts = [l for l in layouts if l["name"].lower() in wanted]
    needed = []
    for l in layouts:
        for m in l["models"]:
            if m["kn5"] not in needed:
                needed.append(m["kn5"])

    kn5_assets = {}
    for kid in needed:
        kn5 = manifest["kn5"][kid]
        log("KN5 %s: %d textures, %d materials, %d meshes" % (
            kid, len(kn5["textures"]), len(kn5["materials"]), len(kn5["meshes"])))
        textures = import_textures(manifest_dir, kn5, "%s/Textures/%s" % (root, kid), args.reuse_existing)
        mats = build_material_instances(kn5, textures, masters, "%s/Materials/%s" % (root, kid))
        meshes = import_meshes(manifest_dir, kn5, mats, masters, "%s/Meshes/%s" % (root, kid),
                               args.reuse_existing, args.nanite)
        kn5_assets[kid] = (meshes, mats)
        EAL.save_directory(root, only_if_is_dirty=True, recursive=True)

    phys = build_physical_materials({"layouts": layouts}, root + "/Physics")

    for layout in layouts:
        build_level(layout, manifest, kn5_assets, phys, matrix, scale,
                    "%s/Levels/%s" % (root, layout["level"]), root + "/Materials/_Surfaces",
                    not args.no_lighting)

    EAL.save_directory(dest, only_if_is_dirty=True, recursive=True)
    log("done: %d layout level(s) in %s/Levels" % (len(layouts), root))
    for w in manifest.get("warnings", []):
        warn("converter: " + w)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SystemExit:
        pass
    except Exception as exc:  # noqa: BLE001
        unreal.log_error("[AC2UE] " + str(exc))
        raise
