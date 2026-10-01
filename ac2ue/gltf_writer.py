"""Minimal binary glTF 2.0 (.glb) writer for single static meshes."""

from __future__ import annotations

import json
import struct

import numpy as np

_FLOAT = 5126
_USHORT = 5123
_UINT = 5125
_ARRAY_BUFFER = 34962
_ELEMENT_ARRAY_BUFFER = 34963


def _pad4(b: bytes, pad: bytes = b"\x00") -> bytes:
    return b + pad * ((4 - len(b) % 4) % 4)


def write_glb(path: str, name: str, positions: np.ndarray, normals: np.ndarray,
              uvs: np.ndarray, indices: np.ndarray) -> None:
    """Write one mesh as a .glb.

    positions/normals: (N,3) float32 in glTF space (right-handed, +Y up, meters).
    uvs: (N,2) float32, top-left origin. indices: (M,) triangles, CCW front faces.
    The mesh carries no material; the Unreal script assigns materials itself.
    """
    positions = np.ascontiguousarray(positions, dtype=np.float32)
    normals = np.ascontiguousarray(normals, dtype=np.float32)
    uvs = np.ascontiguousarray(uvs, dtype=np.float32)
    n = len(positions)
    if n <= 65535:
        idx = np.ascontiguousarray(indices, dtype=np.uint16)
        idx_type = _USHORT
    else:
        idx = np.ascontiguousarray(indices, dtype=np.uint32)
        idx_type = _UINT

    chunks = []
    views = []
    offset = 0
    for arr, target in ((positions, _ARRAY_BUFFER), (normals, _ARRAY_BUFFER),
                        (uvs, _ARRAY_BUFFER), (idx, _ELEMENT_ARRAY_BUFFER)):
        data = _pad4(arr.tobytes())
        views.append({"buffer": 0, "byteOffset": offset,
                      "byteLength": arr.nbytes, "target": target})
        chunks.append(data)
        offset += len(data)
    bin_data = b"".join(chunks)

    gltf = {
        "asset": {"version": "2.0", "generator": "ac2ue kn5 converter"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": name, "mesh": 0}],
        "meshes": [{
            "name": name,
            "primitives": [{
                "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                "indices": 3,
                "mode": 4,
            }],
        }],
        "accessors": [
            {"bufferView": 0, "componentType": _FLOAT, "count": n, "type": "VEC3",
             "min": positions.min(axis=0).tolist(), "max": positions.max(axis=0).tolist()},
            {"bufferView": 1, "componentType": _FLOAT, "count": n, "type": "VEC3"},
            {"bufferView": 2, "componentType": _FLOAT, "count": n, "type": "VEC2"},
            {"bufferView": 3, "componentType": idx_type, "count": int(idx.size), "type": "SCALAR"},
        ],
        "bufferViews": views,
        "buffers": [{"byteLength": len(bin_data)}],
    }

    json_bytes = _pad4(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    total = 12 + 8 + len(json_bytes) + 8 + len(bin_data)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(json_bytes), 0x4E4F534A))
        f.write(json_bytes)
        f.write(struct.pack("<II", len(bin_data), 0x004E4942))
        f.write(bin_data)


def write_calibration_glb(path: str) -> None:
    """A tetrahedron spanning +1 m on glTF X, +2 m on Y, +3 m on Z.

    The Unreal script imports this and reads the resulting bounding box to learn
    exactly how the engine's glTF importer maps glTF axes to Unreal axes, so
    actor placement never depends on assumptions about importer conventions.
    """
    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 2, 0], [0, 0, 3]], dtype=np.float32)
    nrm = np.array([[0, 1, 0]] * 4, dtype=np.float32)
    uv = np.zeros((4, 2), dtype=np.float32)
    idx = np.array([0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3], dtype=np.uint16)
    write_glb(path, "AC2UE_Calibration", pos, nrm, uv, idx)


def write_skinned_glb(path: str, name: str, primitives: list, joints: list) -> None:
    """Write one skinned mesh (several material sections) with a simple skeleton.

    primitives: list of dicts {material, positions (N,3), normals (N,3),
                uvs (N,2), joints (N,) int bone index, indices (M,)}
    joints: list of (bone_name, parent_index or None, translation xyz). Bones
            carry translation only, so inverse bind matrices are pure
            translations of the bone's world position.
    The frame is glTF (right-handed, +Y up, meters).
    """
    chunks, views, accessors = [], [], []
    offset = 0

    def add(arr, target=None):
        nonlocal offset
        data = _pad4(arr.tobytes())
        view = {"buffer": 0, "byteOffset": offset, "byteLength": arr.nbytes}
        if target:
            view["target"] = target
        views.append(view)
        chunks.append(data)
        offset += len(data)
        return len(views) - 1

    def accessor(arr, ctype, typ, target=None, minmax=False):
        idx = add(arr, target)
        acc = {"bufferView": idx, "componentType": ctype, "count": int(len(arr)), "type": typ}
        if minmax:
            acc["min"] = arr.min(axis=0).tolist()
            acc["max"] = arr.max(axis=0).tolist()
        accessors.append(acc)
        return len(accessors) - 1

    # Bone world positions (translation-only chain).
    world = []
    for bname, parent, t in joints:
        base = world[parent] if parent is not None else np.zeros(3)
        world.append(base + np.asarray(t, dtype=np.float64))

    materials, gl_prims = [], []
    for prim in primitives:
        pos = np.ascontiguousarray(prim["positions"], dtype=np.float32)
        n = len(pos)
        nrm = np.ascontiguousarray(prim["normals"], dtype=np.float32)
        uv = np.ascontiguousarray(prim["uvs"], dtype=np.float32)
        j = np.zeros((n, 4), dtype=np.uint8)
        j[:, 0] = np.asarray(prim["joints"], dtype=np.uint8)
        w = np.zeros((n, 4), dtype=np.float32)
        w[:, 0] = 1.0
        if n <= 65535:
            idx = np.ascontiguousarray(prim["indices"], dtype=np.uint16)
            ictype = _USHORT
        else:
            idx = np.ascontiguousarray(prim["indices"], dtype=np.uint32)
            ictype = _UINT
        materials.append({"name": prim["material"],
                          "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.6}})
        gl_prims.append({
            "attributes": {
                "POSITION": accessor(pos, _FLOAT, "VEC3", _ARRAY_BUFFER, True),
                "NORMAL": accessor(nrm, _FLOAT, "VEC3", _ARRAY_BUFFER),
                "TEXCOORD_0": accessor(uv, _FLOAT, "VEC2", _ARRAY_BUFFER),
                "JOINTS_0": accessor(j, 5121, "VEC4", _ARRAY_BUFFER),
                "WEIGHTS_0": accessor(w, _FLOAT, "VEC4", _ARRAY_BUFFER),
            },
            "indices": accessor(idx, ictype, "SCALAR", _ELEMENT_ARRAY_BUFFER),
            "material": len(materials) - 1,
            "mode": 4,
        })

    ibm = np.zeros((len(joints), 16), dtype=np.float32)
    for i, wp in enumerate(world):
        m = np.identity(4, dtype=np.float32)
        m[3, :3] = -wp  # column-major storage: translation is elements 12..14
        ibm[i] = m.reshape(-1)
    ibm_acc = accessor(ibm, _FLOAT, "MAT4")

    # nodes: 0 = mesh node, 1.. = joints (joint i -> node i+1)
    nodes = [{"name": name, "mesh": 0, "skin": 0}]
    for i, (bname, parent, t) in enumerate(joints):
        nodes.append({"name": bname, "translation": [float(c) for c in t]})
    for i, (bname, parent, t) in enumerate(joints):
        if parent is not None:
            nodes[parent + 1].setdefault("children", []).append(i + 1)
    roots = [i + 1 for i, (_, parent, _) in enumerate(joints) if parent is None]

    bin_data = b"".join(chunks)
    gltf = {
        "asset": {"version": "2.0", "generator": "ac2ue kn5 converter"},
        "scene": 0,
        "scenes": [{"nodes": [0] + roots}],
        "nodes": nodes,
        "meshes": [{"name": name, "primitives": gl_prims}],
        "materials": materials,
        "skins": [{"name": name + "_Skeleton", "joints": list(range(1, len(joints) + 1)),
                   "skeleton": roots[0], "inverseBindMatrices": ibm_acc}],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(bin_data)}],
    }
    json_bytes = _pad4(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    total = 12 + 8 + len(json_bytes) + 8 + len(bin_data)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(json_bytes), 0x4E4F534A))
        f.write(json_bytes)
        f.write(struct.pack("<II", len(bin_data), 0x004E4942))
        f.write(bin_data)
