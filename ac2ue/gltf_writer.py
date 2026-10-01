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
