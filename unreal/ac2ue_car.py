"""Unreal side of the car import: called by ue_import_track.py for car manifests.

Builds, under <dest>/Cars/<car>/:
  Textures/, Materials/          (shared master materials, one instance per AC material)
  SK_<car> + skeleton + physics  (root + WHEEL_LF/RF/LR/RR bones)
  BP_<car>_WheelFront/Rear       (ChaosVehicleWheel blueprints from tyres.ini / mesh)
  ABP_<car>                      (wheel animation; copied from the Vehicle template when present)
  BP_<car>                       (drivable WheeledVehiclePawn; child of the Vehicle
                                  template's pawn when present, so input + camera work)
  BP_<car>_<skin>                (one child blueprint per extra livery)
"""

import json
import os
import struct

import unreal

EAL = unreal.EditorAssetLibrary
MEL = unreal.MaterialEditingLibrary
ASSET_TOOLS = unreal.AssetToolsHelpers.get_asset_tools()

WHEELS = ("LF", "RF", "LR", "RR")


# ------------------------------------------------------------ axis handling

def _matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _correction(matrix, scale):
    """Signed permutation C (glTF->glTF) so the importer maps car +X fwd, +Z right,
    +Y up onto Unreal +X, +Y, +Z."""
    s2 = scale * scale
    inv = [[matrix[j][i] / s2 for j in range(3)] for i in range(3)]  # M^-1 = M^T / s^2
    expected = [[scale, 0, 0], [0, 0, scale], [0, scale, 0]]
    c = _matmul(inv, expected)
    return [[int(round(v)) for v in row] for row in c]


def _det(m):
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def remap_glb(src, dst, c):
    """Apply a signed permutation to positions, normals, joint translations and
    inverse bind matrices of a glb written by the converter (pure Python)."""
    with open(src, "rb") as f:
        data = bytearray(f.read())
    jlen = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(bytes(data[20:20 + jlen]))
    bin_off = 20 + jlen + 8

    def apply(v):
        return [sum(c[i][j] * v[j] for j in range(3)) for i in range(3)]

    def remap_accessor(idx, stride_floats, offset_floats, update_minmax):
        acc = gltf["accessors"][idx]
        view = gltf["bufferViews"][acc["bufferView"]]
        base = bin_off + view.get("byteOffset", 0) + acc.get("byteOffset", 0)
        lo, hi = [float("inf")] * 3, [float("-inf")] * 3
        for k in range(acc["count"]):
            at = base + (k * stride_floats + offset_floats) * 4
            v = apply(struct.unpack_from("<3f", data, at))
            struct.pack_into("<3f", data, at, *v)
            lo = [min(a, b) for a, b in zip(lo, v)]
            hi = [max(a, b) for a, b in zip(hi, v)]
        if update_minmax:
            acc["min"], acc["max"] = lo, hi

    done = set()
    for mesh in gltf["meshes"]:
        for prim in mesh["primitives"]:
            for attr, mm in (("POSITION", True), ("NORMAL", False)):
                idx = prim["attributes"].get(attr)
                if idx is not None and idx not in done:
                    remap_accessor(idx, 3, 0, mm)
                    done.add(idx)
    for node in gltf["nodes"]:
        if "translation" in node:
            node["translation"] = apply(node["translation"])
    for skin in gltf.get("skins", []):
        if "inverseBindMatrices" in skin:
            remap_accessor(skin["inverseBindMatrices"], 16, 12, False)

    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * ((4 - len(js) % 4) % 4)
    binary = bytes(data[bin_off:])
    total = 12 + 8 + len(js) + 8 + len(binary)
    with open(dst, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A))
        f.write(js)
        f.write(struct.pack("<II", len(binary), 0x004E4942))
        f.write(binary)


def to_ue(v, scale):
    """Car frame (glTF X fwd, Y up, Z right, meters) -> Unreal (cm)."""
    return unreal.Vector(v[0] * scale, v[2] * scale, v[1] * scale)


# ------------------------------------------------------------ asset search

def _find_assets(class_package, class_name, root="/Game"):
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        flt = unreal.ARFilter(class_paths=[unreal.TopLevelAssetPath(class_package, class_name)],
                              package_paths=[root], recursive_paths=True)
    except Exception:  # noqa: BLE001  (UE 5.0)
        flt = unreal.ARFilter(class_names=[class_name], package_paths=[root], recursive_paths=True)
    return list(ar.get_assets(flt))


def _tag(asset, name):
    try:
        return str(asset.get_tag_value(name) or "")
    except Exception:  # noqa: BLE001
        return ""


def find_vehicle_base(exclude_root, override=None):
    """The Vehicle template's base pawn (has input + camera), if the project has it."""
    if override:
        bp = EAL.load_asset(override)
        return bp.generated_class() if bp else None
    candidates = []
    for a in _find_assets("/Script/Engine", "Blueprint"):
        path = str(a.package_name)
        if path.startswith(exclude_root):
            continue
        if "WheeledVehiclePawn" not in _tag(a, "NativeParentClass"):
            continue
        direct = "WheeledVehiclePawn" in _tag(a, "ParentClass")
        name = str(a.asset_name).lower()
        score = (2 if direct else 0) + (2 if "base" in name else 0) + (1 if "vehicletemplate" in path.lower() else 0)
        candidates.append((score, path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    bp = EAL.load_asset(candidates[0][1])
    unreal.log("[AC2UE] using vehicle base %s (input, camera)" % candidates[0][1])
    return bp.generated_class()


def find_template_abp(exclude_root):
    for a in _find_assets("/Script/Engine", "AnimBlueprint"):
        path = str(a.package_name)
        if path.startswith(exclude_root):
            continue
        if "VehicleAnimationInstance" in _tag(a, "ParentClass") + _tag(a, "NativeParentClass"):
            return path
    return None


# ------------------------------------------------------------------ helpers

def _compile(bp):
    try:
        unreal.BlueprintEditorLibrary.compile_blueprint(bp)
    except Exception:  # noqa: BLE001
        try:
            unreal.KismetEditorUtilities.compile_blueprint(bp)
        except Exception:  # noqa: BLE001
            pass


def _new_blueprint(name, folder, parent_class):
    path = folder + "/" + name
    if EAL.does_asset_exist(path):
        bp = EAL.load_asset(path)
        if bp is not None:
            # A re-run may have a better parent now (e.g. the Vehicle template
            # was added since the first import).
            try:
                if bp.get_editor_property("parent_class") != parent_class:
                    unreal.BlueprintEditorLibrary.reparent_blueprint(bp, parent_class)
            except Exception:  # noqa: BLE001
                pass
            return bp
    factory = unreal.BlueprintFactory()
    factory.set_editor_property("parent_class", parent_class)
    return ASSET_TOOLS.create_asset(name, folder, unreal.Blueprint, factory)


def _cdo(bp):
    return unreal.get_default_object(bp.generated_class())


def _set(obj, prop, value, H):
    try:
        obj.set_editor_property(prop, value)
        return True
    except Exception as e:  # noqa: BLE001
        H.warn("could not set %s: %s" % (prop, e))
        return False


def assign_mesh_materials(skm, manifest, mats, H):
    slots = list(skm.get_editor_property("materials"))
    sections = manifest["mesh"]["sections"]
    keys = manifest["mesh"]["section_material_keys"]
    by_name = {s: k for s, k in zip(sections, keys)}
    new = []
    for i, slot in enumerate(slots):
        name = str(slot.get_editor_property("material_slot_name"))
        key = by_name.get(name) or (keys[i] if i < len(keys) else None)
        mi = mats.get(key)
        if mi is not None:
            slot.set_editor_property("material_interface", mi)
        new.append(slot)
    skm.set_editor_property("materials", new)
    return [str(s.get_editor_property("material_slot_name")) for s in new]


def setup_physics_asset(skm, manifest, scale, H):
    pa = None
    try:
        pa = skm.get_editor_property("physics_asset")
    except Exception:  # noqa: BLE001
        pass
    if pa is None:
        H.warn("no physics asset was generated; right-click the skeletal mesh > Create > Physics Asset, "
               "then delete the wheel bodies")
        return None
    box = manifest["body_box"]
    for body in pa.get_editor_property("skeletal_body_setups"):
        bone = str(body.get_editor_property("bone_name"))
        if bone.upper().startswith("WHEEL_"):
            # Chaos raycasts the wheels; their bodies must not collide or simulate.
            _set(body, "physics_type", unreal.PhysicsType.PHYS_TYPE_KINEMATIC, H)
            _set(body, "collision_response", unreal.BodyCollisionResponse.BODY_COLLISION_DISABLED, H)
        elif bone == "root":
            try:
                geom = body.get_editor_property("agg_geom")
                elem = unreal.KBoxElem()
                elem.set_editor_property("center", to_ue(box["center"], scale))
                elem.set_editor_property("x", box["size"][0] * scale)
                elem.set_editor_property("y", box["size"][2] * scale)
                elem.set_editor_property("z", box["size"][1] * scale)
                geom.set_editor_property("box_elems", [elem])
                for other in ("sphyl_elems", "sphere_elems", "convex_elems"):
                    geom.set_editor_property(other, [])
                body.set_editor_property("agg_geom", geom)
            except Exception as e:  # noqa: BLE001
                H.warn("kept the auto-generated body shape (%s)" % e)
    EAL.save_loaded_asset(pa)
    return pa


def make_wheel(name, folder, axle, phys, manifest, scale, H):
    bp = _new_blueprint(name, folder, unreal.ChaosVehicleWheel)
    w = _cdo(bp)
    keys = ("LF", "RF") if axle == "front" else ("LR", "RR")
    radius = phys[axle]["radius"] or manifest["wheels"][keys[0]]["mesh_radius"] or 0.33
    width = phys[axle]["width"] or manifest["wheels"][keys[0]]["mesh_width"] or 0.22
    drive = phys["drive"]
    _set(w, "wheel_radius", float(radius) * scale, H)
    _set(w, "wheel_width", float(width) * scale, H)
    _set(w, "axle_type", unreal.AxleType.FRONT if axle == "front" else unreal.AxleType.REAR, H)
    _set(w, "affected_by_steering", axle == "front", H)
    _set(w, "max_steer_angle", float(phys["max_steer_deg"]) if axle == "front" else 0.0, H)
    _set(w, "affected_by_handbrake", axle == "rear", H)
    _set(w, "affected_by_engine", drive == "AWD" or (drive == "FWD") == (axle == "front"), H)
    _compile(bp)
    EAL.save_loaded_asset(bp)
    return bp


def setup_movement(cdo, manifest, wheel_bps, scale, H):
    phys = manifest["physics"]
    mv = cdo.get_editor_property("vehicle_movement_component")
    setups = []
    for k in WHEELS:
        s = unreal.ChaosWheelSetup()
        s.set_editor_property("wheel_class", wheel_bps["front" if k.endswith("F") else "rear"].generated_class())
        s.set_editor_property("bone_name", "WHEEL_" + k)
        setups.append(s)
    _set(mv, "wheel_setups", setups, H)
    _set(mv, "mass", float(phys["mass"]), H)
    size = manifest["body_box"]["size"]
    _set(mv, "chassis_width", size[2] * scale, H)
    _set(mv, "chassis_height", size[1] * scale, H)

    try:
        eng = mv.get_editor_property("engine_setup")
        eng.set_editor_property("max_torque", float(phys["engine"]["max_torque"]))
        eng.set_editor_property("max_rpm", float(phys["engine"]["max_rpm"]))
        eng.set_editor_property("engine_idle_rpm", float(phys["engine"]["idle_rpm"]))
        curve = phys["engine"].get("curve") or []
        if curve:
            try:
                rfc = eng.get_editor_property("torque_curve")
                rich = rfc.get_editor_property("editor_curve_data")
                peak = max(t for _, t in curve) or 1.0
                keys = [unreal.RichCurveKey(time=float(r), value=float(t) / peak) for r, t in curve]
                rich.set_editor_property("keys", keys)
                rfc.set_editor_property("editor_curve_data", rich)
                eng.set_editor_property("torque_curve", rfc)
            except Exception as e:  # noqa: BLE001
                H.warn("kept the default torque curve shape (%s); max torque is set" % e)
        mv.set_editor_property("engine_setup", eng)
    except Exception as e:  # noqa: BLE001
        H.warn("engine setup not applied: %s" % e)

    try:
        tr = mv.get_editor_property("transmission_setup")
        tr.set_editor_property("forward_gear_ratios", [float(g) for g in phys["gears"]["forward"]])
        tr.set_editor_property("reverse_gear_ratios", [float(phys["gears"]["reverse"])])
        tr.set_editor_property("final_ratio", float(phys["gears"]["final"]))
        mv.set_editor_property("transmission_setup", tr)
    except Exception as e:  # noqa: BLE001
        H.warn("transmission setup not applied: %s" % e)

    try:
        diff = mv.get_editor_property("differential_setup")
        kind = {"FWD": "FRONT_WHEEL_DRIVE", "RWD": "REAR_WHEEL_DRIVE", "AWD": "ALL_WHEEL_DRIVE"}[phys["drive"]]
        diff.set_editor_property("differential_type", getattr(unreal.VehicleDifferential, kind))
        mv.set_editor_property("differential_setup", diff)
    except Exception as e:  # noqa: BLE001
        H.warn("differential setup not applied: %s" % e)


def set_mesh(cdo, skm, abp_class, H):
    comp = cdo.get_editor_property("mesh")
    try:
        comp.set_skeletal_mesh_asset(skm)
    except Exception:  # noqa: BLE001
        if not _set(comp, "skeletal_mesh_asset", skm, H):
            _set(comp, "skeletal_mesh", skm, H)
    if abp_class is not None:
        _set(comp, "animation_mode", unreal.AnimationMode.ANIMATION_BLUEPRINT, H)
        _set(comp, "anim_class", abp_class, H)
    return comp


# ---------------------------------------------------------------------- run

def run(manifest, manifest_dir, args, H):
    if not hasattr(unreal, "ChaosVehicleWheel"):
        raise RuntimeError("The ChaosVehicles plugin is not enabled. Enable 'Chaos Vehicles' in "
                           "Edit > Plugins, restart the editor and run this again.")
    dest = args.dest.rstrip("/")
    shared = dest + "/_Shared"
    root = "%s/Cars/%s" % (dest, manifest["car_asset_name"])
    car = manifest["car_asset_name"]
    H.log("importing car '%s' into %s" % (manifest["car"], root))

    matrix, scale = H.calibrate(manifest_dir, manifest, dest)
    c = _correction(matrix, scale)
    glb = os.path.join(manifest_dir, manifest["mesh"]["file"])
    if c != [[1, 0, 0], [0, 1, 0], [0, 0, 1]]:
        if _det(c) < 0:
            H.warn("this engine's glTF axes would mirror the car; check its orientation after import")
        fixed = os.path.join(manifest_dir, "_ue_" + manifest["mesh"]["file"])
        remap_glb(glb, fixed, c)
        H.log("re-oriented the car for this engine's glTF axes")
        glb = fixed

    defaults = H.import_defaults(manifest_dir, manifest, shared + "/DefaultTextures", True)
    masters = {k: H.build_master(k, shared + "/Masters", defaults, args.rebuild_masters)
               for k in H.MASTER_PARAMS}
    textures = H.import_textures(manifest_dir, manifest, root + "/Textures", args.reuse_existing)
    mats = H.build_material_instances(manifest, textures, masters, root + "/Materials")

    # ---- skeletal mesh
    asset = manifest["mesh"]["asset"]
    paths = H.import_files([(glb, root + "/Mesh", asset)], "Importing car mesh", args.reuse_existing)[0]
    skm = H.pick(paths, unreal.SkeletalMesh)
    if skm is None:
        raise RuntimeError("the car mesh did not import as a skeletal mesh (check the Output Log)")
    H.cleanup_extras(paths, skm, root + "/Mesh")
    slot_names = assign_mesh_materials(skm, manifest, mats, H)
    setup_physics_asset(skm, manifest, scale, H)
    EAL.save_loaded_asset(skm)
    skeleton = skm.get_editor_property("skeleton")

    if not manifest["drivable"]:
        H.warn("no wheel nodes in this car: imported as a skeletal mesh only")
        EAL.save_directory(root, only_if_is_dirty=True, recursive=True)
        return

    # ---- wheels, animation, vehicle
    bp_folder = root + "/Blueprints"
    wheel_bps = {axle: make_wheel("BP_%s_Wheel%s" % (car, axle.title()), bp_folder, axle,
                                  manifest["physics"], manifest, scale, H)
                 for axle in ("front", "rear")}

    abp_class = None
    abp_path = bp_folder + "/ABP_" + car
    template_abp = find_template_abp(root)
    if template_abp and EAL.does_asset_exist(abp_path):
        old = EAL.load_asset(abp_path)
        if old is not None and EAL.get_metadata_tag(old, "AC2UE_ABP") == "empty":
            EAL.delete_asset(abp_path)  # replace the empty one with the template's
    if not EAL.does_asset_exist(abp_path):
        if template_abp:
            EAL.duplicate_asset(template_abp, abp_path)
        else:
            try:
                f = unreal.AnimBlueprintFactory()
                f.set_editor_property("target_skeleton", skeleton)
                f.set_editor_property("parent_class", unreal.VehicleAnimationInstance)
                made_abp = ASSET_TOOLS.create_asset("ABP_" + car, bp_folder, unreal.AnimBlueprint, f)
                EAL.set_metadata_tag(made_abp, "AC2UE_ABP", "empty")
            except Exception as e:  # noqa: BLE001
                H.warn("could not create the animation blueprint: %s" % e)
    abp = EAL.load_asset(abp_path) if EAL.does_asset_exist(abp_path) else None
    if abp is not None:
        _set(abp, "target_skeleton", skeleton, H)
        _compile(abp)
        EAL.save_loaded_asset(abp)
        abp_class = abp.generated_class()

    base = find_vehicle_base(root, getattr(args, "vehicle_base", None))
    vehicle = _new_blueprint("BP_" + car, bp_folder, base or unreal.WheeledVehiclePawn)
    cdo = _cdo(vehicle)
    set_mesh(cdo, skm, abp_class, H)
    setup_movement(cdo, manifest, wheel_bps, scale, H)
    _compile(vehicle)
    EAL.save_loaded_asset(vehicle)

    # ---- liveries
    made = []
    for skin, entry in manifest["skins"].items():
        if not entry["materials"]:
            continue
        skin_tex = H.import_textures(manifest_dir, {"textures": entry["textures"]},
                                     "%s/Textures/Skins/%s" % (root, H.safe_name(skin)), args.reuse_existing)
        overrides = {}
        for key, var in entry["materials"].items():
            base_mi = mats.get(key)
            if base_mi is None:
                continue
            mi, _ = H.create_or_load(var["asset"], root + "/Materials/Skins", unreal.MaterialInstanceConstant,
                                     unreal.MaterialInstanceConstantFactoryNew())
            MEL.set_material_instance_parent(mi, base_mi)
            for param, tex_key in var["textures"].items():
                if tex_key in skin_tex:
                    MEL.set_material_instance_texture_parameter_value(mi, param, skin_tex[tex_key])
            MEL.update_material_instance(mi)
            overrides[key] = mi
        child = _new_blueprint("BP_%s_%s" % (car, H.safe_name(skin)), bp_folder, vehicle.generated_class())
        comp = _cdo(child).get_editor_property("mesh")
        keys = manifest["mesh"]["section_material_keys"]
        names = manifest["mesh"]["sections"]
        order = [keys[names.index(n)] if n in names else (keys[i] if i < len(keys) else None)
                 for i, n in enumerate(slot_names)]
        _set(comp, "override_materials", [overrides.get(k) or mats.get(k) for k in order], H)
        _compile(child)
        EAL.save_loaded_asset(child)
        made.append(skin)

    EAL.save_directory(root, only_if_is_dirty=True, recursive=True)
    H.log("car ready: %s/BP_%s%s" % (bp_folder, car, " (+%d livery variants)" % len(made) if made else ""))
    if base is None:
        H.warn("no Vehicle template pawn found, so BP_%s has no input or camera yet. Add it with "
               "Content Browser > Add > Add Feature or Content Pack > Vehicle, then run this again "
               "with --reuse-existing" % car)
    if template_abp is None:
        H.warn("ABP_%s was created empty: open it and connect a 'Wheel Controller' node between "
               "'Mesh Space Ref Pose' and the Output Pose so the wheels turn" % car)
    for w in manifest.get("warnings", []):
        H.warn("converter: " + w)
