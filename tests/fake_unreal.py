"""A stand-in 'unreal' module for dry-running ue_import_track.py outside the editor.

It simulates the asset registry, imports (including a glTF importer with a
non-trivial axis mapping), material editing and actor spawning, and records
everything so tests can check the script's behaviour.
"""

import os
import struct
import json
import types

import numpy as np

CALLS = []
ASSETS = {}
ACTORS = []
LEVELS = {}
STATE = {"level": None}
# Simulated importer: UE = M @ glTF (meters -> cm, with an axis permutation)
IMPORT_M = np.array([[0, 0, 100.0], [100.0, 0, 0], [0, 100.0, 0]])


class _Enum:
    def __init__(self, name):
        self._n = name

    def __getattr__(self, item):
        return f"{self._n}.{item}"


class Name(str):
    pass


class Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __repr__(self):
        return f"V({self.x:.1f},{self.y:.1f},{self.z:.1f})"


class Rotator:
    def __init__(self, roll=0.0, pitch=0.0, yaw=0.0):
        self.roll, self.pitch, self.yaw = roll, pitch, yaw


class LinearColor:
    def __init__(self, r=0, g=0, b=0, a=1):
        self.rgba = (r, g, b, a)


class Box:
    def __init__(self, lo, hi):
        self.min, self.max = Vector(*lo), Vector(*hi)


class Object:
    def __init__(self, name="obj", path=None):
        self._name = name
        self._path = path
        self._props = {}

    def get_name(self):
        return self._name

    def set_editor_property(self, k, v):
        CALLS.append(("set", self._name, k))
        self._props[k] = v

    def get_editor_property(self, k):
        if k not in self._props:
            self._props[k] = Object(f"{self._name}.{k}")
        return self._props[k]


class MaterialInterface(Object): pass
class Material(MaterialInterface): pass
class MaterialInstanceConstant(MaterialInterface): pass
class Texture(Object): pass
class Texture2D(Texture): pass
class PhysicalMaterial(Object): pass
class World(Object): pass


class StaticMesh(Object):
    def __init__(self, name, path, verts=None):
        super().__init__(name, path)
        self.verts = verts
        self.materials = {}

    def get_bounding_box(self):
        return Box(self.verts.min(axis=0), self.verts.max(axis=0))

    def set_material(self, i, m):
        self.materials[i] = m


class StaticMeshComponent(Object):
    def __init__(self, name):
        super().__init__(name)
        self.collision = None
        self.mat = {}

    def set_collision_profile_name(self, n):
        self.collision = n

    def set_material(self, i, m):
        self.mat[i] = m

    def set_cast_shadow(self, b):
        self._props["cast_shadow"] = b

    def set_visibility(self, v, propagate=False):
        self._props["visible"] = v


class Actor(Object):
    def __init__(self, obj, loc):
        super().__init__(getattr(obj, "_name", "actor"))
        self.obj, self.loc = obj, loc
        self.label = None
        self.folder = None
        self.hidden = False
        self._props["static_mesh_component"] = StaticMeshComponent(self._name + ".smc")
        self._props["tags"] = []

    def set_actor_label(self, l):
        self.label = l

    def set_folder_path(self, f):
        self.folder = f

    def set_actor_hidden_in_game(self, h):
        self.hidden = h


for _n in ["MaterialExpressionTextureSampleParameter2D", "MaterialExpressionScalarParameter",
           "MaterialExpressionVectorParameter", "MaterialExpressionComponentMask",
           "MaterialExpressionMultiply", "MaterialExpressionAdd",
           "MaterialExpressionLinearInterpolate", "MaterialExpressionTextureCoordinate",
           "MaterialExpressionDivide", "MaterialExpressionMax", "MaterialExpressionSaturate",
           "MaterialExpressionWorldPosition", "MaterialExpressionMaterialFunctionCall",
           "DirectionalLight", "SkyLight", "SkyAtmosphere", "ExponentialHeightFog"]:
    globals()[_n] = type(_n, (Object,), {})

MaterialSamplerType = _Enum("MaterialSamplerType")
MaterialProperty = _Enum("MaterialProperty")
BlendMode = _Enum("BlendMode")
TextureCompressionSettings = _Enum("TextureCompressionSettings")
CollisionTraceFlag = _Enum("CollisionTraceFlag")
ComponentMobility = _Enum("ComponentMobility")


def _read_glb_positions(path):
    with open(path, "rb") as f:
        data = f.read()
    jlen = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20:20 + jlen])
    bin_off = 20 + jlen + 8
    acc = gltf["accessors"][0]
    view = gltf["bufferViews"][acc["bufferView"]]
    pos = np.frombuffer(data, "<f4", acc["count"] * 3, bin_off + view["byteOffset"]).reshape(-1, 3)
    return pos


def _register(pkg, obj):
    ASSETS[pkg] = obj
    return obj


class AssetImportTask(Object):
    pass


class _AssetTools:
    def create_asset(self, name, folder, cls, factory):
        pkg = f"{folder}/{name}"
        CALLS.append(("create_asset", pkg, cls.__name__))
        return _register(pkg, cls(name, pkg))

    def import_asset_tasks(self, tasks):
        for t in tasks:
            fn = t._props["filename"]
            dest, name = t._props["destination_path"], t._props["destination_name"]
            ext = os.path.splitext(fn)[1].lower()
            out = []
            if ext == ".glb" and _glb_json(fn).get("skins"):
                g = _glb_json(fn)
                skel = _register(f"{dest}/{name}_Skeleton", Skeleton(name + "_Skeleton"))
                bones = {n["name"]: np.array(n.get("translation", [0, 0, 0])) @ IMPORT_M.T
                         for n in g["nodes"] if "mesh" not in n}
                pa = _register(f"{dest}/{name}_PhysicsAsset", PhysicsAsset(name + "_PhysicsAsset", list(bones)))
                skm = _register(f"{dest}/{name}", SkeletalMesh(name, f"{dest}/{name}",
                                                               [m["name"] for m in g["materials"]], skel, pa, bones))
                out += [f"{dest}/{name}.{name}", f"{dest}/{name}_Skeleton.x", f"{dest}/{name}_PhysicsAsset.x"]
                for m in g["materials"]:
                    _register(f"{dest}/{m['name']}_imported", Material(m["name"]))
                    out.append(f"{dest}/{m['name']}_imported.x")
            elif ext == ".glb":
                verts = _read_glb_positions(fn) @ IMPORT_M.T
                mesh = _register(f"{dest}/{name}", StaticMesh(name, f"{dest}/{name}", verts))
                out.append(f"{dest}/{name}.{name}")
                # importers often create a placeholder material next to the mesh
                _register(f"{dest}/M_{name}", Material("M_" + name))
                out.append(f"{dest}/M_{name}.M_{name}")
            elif ext in (".png", ".jpg"):
                _register(f"{dest}/{name}", Texture2D(name, f"{dest}/{name}"))
                out.append(f"{dest}/{name}.{name}")
            # .dds/.bin: simulate a failed import (nothing created)
            t._props["imported_object_paths"] = out
            CALLS.append(("import", fn, len(out)))


class AssetToolsHelpers:
    @staticmethod
    def get_asset_tools():
        return _AssetTools()


class EditorAssetLibrary:
    @staticmethod
    def does_asset_exist(p):
        return p.split(".")[0] in ASSETS

    @staticmethod
    def load_asset(p):
        return ASSETS.get(p.split(".")[0])

    @staticmethod
    def delete_asset(p):
        CALLS.append(("delete", p))
        return ASSETS.pop(p.split(".")[0], None) is not None

    @staticmethod
    def list_assets(folder, recursive=True, include_folder=False):
        return [k for k in ASSETS if k.startswith(folder + "/")]

    @staticmethod
    def save_loaded_asset(a):
        return True

    @staticmethod
    def save_directory(*a, **k):
        CALLS.append(("save_dir", a, k))
        return True

    @staticmethod
    def set_metadata_tag(obj, tag, value):
        obj._props["meta:" + tag] = value

    @staticmethod
    def get_metadata_tag(obj, tag):
        return obj._props.get("meta:" + tag, "")


class MaterialEditingLibrary:
    @staticmethod
    def create_material_expression(mat, cls, x=0, y=0):
        return cls(cls.__name__)

    @staticmethod
    def connect_material_expressions(a, ao, b, bi):
        CALLS.append(("connect", ao, bi))
        return True

    @staticmethod
    def connect_material_property(a, ao, prop):
        CALLS.append(("connect_prop", ao, prop))
        return True

    @staticmethod
    def layout_material_expressions(m): pass

    @staticmethod
    def delete_all_material_expressions(m):
        CALLS.append(("clear_graph", m.get_name()))

    @staticmethod
    def recompile_material(m): pass

    @staticmethod
    def set_material_instance_parent(mi, parent):
        mi._props["parent"] = parent

    @staticmethod
    def set_material_instance_texture_parameter_value(mi, n, v):
        mi._props["tex:" + n] = v
        return True

    @staticmethod
    def set_material_instance_scalar_parameter_value(mi, n, v):
        mi._props["scalar:" + n] = v
        return True

    @staticmethod
    def set_material_instance_vector_parameter_value(mi, n, v):
        mi._props["vector:" + n] = v
        return True

    @staticmethod
    def update_material_instance(mi): pass


class MaterialFactoryNew: pass
class MaterialInstanceConstantFactoryNew: pass
class PhysicalMaterialFactoryNew: pass


class ScopedSlowTask:
    def __init__(self, *a): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def make_dialog(self, *a): pass
    def enter_progress_frame(self, *a): pass
    def should_cancel(self): return False


class StaticMeshEditorSubsystem:
    def remove_collisions(self, mesh):
        CALLS.append(("remove_collisions", mesh._name))


class LevelEditorSubsystem:
    def new_level(self, path):
        LEVELS[path] = []
        STATE["level"] = path
        ASSETS[path] = World(path)
        return True

    def load_level(self, path):
        STATE["level"] = path

    def save_current_level(self):
        CALLS.append(("save_level", STATE["level"]))


class EditorActorSubsystem:
    def spawn_actor_from_object(self, obj, loc, rot):
        a = Actor(obj, loc)
        LEVELS[STATE["level"]].append(a)
        ACTORS.append(a)
        return a

    def spawn_actor_from_class(self, cls, loc, rot):
        a = Actor(cls(cls.__name__), loc)
        LEVELS[STATE["level"]].append(a)
        return a

    def get_all_level_actors(self):
        return list(LEVELS.get(STATE["level"], []))

    def destroy_actor(self, a):
        LEVELS[STATE["level"]].remove(a)


_SUBS = {}


def get_editor_subsystem(cls):
    return _SUBS.setdefault(cls, cls())


def log(m): print("LOG", m)
def log_warning(m): print("WARN", m)
def log_error(m): print("ERROR", m)


class EditorStaticMeshLibrary:
    @staticmethod
    def remove_collisions(m): pass



# ---------------------------------------------------------------- car support

def _glb_json(path):
    with open(path, "rb") as f:
        d = f.read()
    jl = struct.unpack_from("<I", d, 12)[0]
    return json.loads(d[20:20 + jl])


class Skeleton(Object): pass
class AnimBlueprint(Object): pass
class Blueprint(Object): pass
class WheeledVehiclePawn(Object): pass
class ChaosVehicleWheel(Object): pass
class VehicleAnimationInstance(Object): pass
class BlueprintFactory(Object): pass
class AnimBlueprintFactory(Object): pass

for _e in ("PhysicsType", "BodyCollisionResponse", "AxleType", "VehicleDifferential", "AnimationMode"):
    globals()[_e] = _Enum(_e)


class Struct(Object):
    def __init__(self, **kw):
        super().__init__(type(self).__name__)
        self._props.update(kw)


class ChaosWheelSetup(Struct): pass
class KBoxElem(Struct): pass
class RichCurveKey(Struct): pass


class SkeletalMaterial(Struct): pass


class SkeletalMesh(Object):
    def __init__(self, name, path, slots, skel, pa, bones):
        super().__init__(name, path)
        self._props["materials"] = [SkeletalMaterial(material_slot_name=s) for s in slots]
        self._props["skeleton"] = skel
        self._props["physics_asset"] = pa
        self.bones = bones


class PhysicsAsset(Object):
    def __init__(self, name, bones):
        super().__init__(name)
        self._props["skeletal_body_setups"] = [Struct(bone_name=b) for b in bones]


class _GeneratedClass:
    def __init__(self, bp):
        self.bp = bp


class _BlueprintAsset(Blueprint):
    def __init__(self, name, parent):
        super().__init__(name)
        self.parent = parent
        self._props["parent_class"] = parent
        self.cdo = Object(name + "_CDO")
        self.cdo._props["mesh"] = Object(name + ".mesh")
        self.cdo._props["vehicle_movement_component"] = Object(name + ".movement")
        self.gc = _GeneratedClass(self)

    def generated_class(self):
        return self.gc


def get_default_object(gc):
    return gc.bp.cdo


_orig_create = _AssetTools.create_asset


def _create_asset(self, name, folder, cls, factory):
    pkg = f"{folder}/{name}"
    if cls in (Blueprint, AnimBlueprint):
        CALLS.append(("create_asset", pkg, cls.__name__))
        return _register(pkg, _BlueprintAsset(name, factory._props.get("parent_class")))
    return _orig_create(self, name, folder, cls, factory)


_AssetTools.create_asset = _create_asset


def _duplicate(src, dst):
    _register(dst, _BlueprintAsset(dst.rsplit("/", 1)[-1], VehicleAnimationInstance))
    return ASSETS[dst]


EditorAssetLibrary.duplicate_asset = staticmethod(_duplicate)


class BlueprintEditorLibrary:
    @staticmethod
    def compile_blueprint(bp):
        CALLS.append(("compile", bp.get_name()))

    @staticmethod
    def reparent_blueprint(bp, parent):
        bp.parent = parent
        bp._props["parent_class"] = parent


class TopLevelAssetPath:
    def __init__(self, *a): pass


class ARFilter:
    def __init__(self, **kw):
        self.kw = kw


REGISTRY = []  # list of fake AssetData added by tests


class _AssetData:
    def __init__(self, package_name, tags):
        self.package_name = package_name
        self.asset_name = package_name.rsplit("/", 1)[-1]
        self.tags = tags

    def get_tag_value(self, k):
        return self.tags.get(k)


class _Registry:
    def get_assets(self, flt):
        return list(REGISTRY)


class AssetRegistryHelpers:
    @staticmethod
    def get_asset_registry():
        return _Registry()
