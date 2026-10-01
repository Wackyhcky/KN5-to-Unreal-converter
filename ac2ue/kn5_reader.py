"""Reader for Assetto Corsa .kn5 model files (unencrypted, standard format).

Layout (little-endian), matching the open-source AcTools reader:

    "sc6969"                      magic (6 bytes)
    int32 version                 (5 or 6; version > 5 adds one extra int32)
    int32 textureCount
      int32 active, string name, uint32 size, bytes[size]
    int32 materialCount
      string name, string shader, byte blendMode, bool alphaTested, int32 depthMode,
      int32 propCount { string name, float A, vec2 B, vec3 C, vec4 D }
      int32 mappingCount { string name, int32 slot, string texture }
    node tree (recursive, depth-first)
      int32 class (1 = dummy, 2 = mesh, 3 = skinned mesh), string name,
      int32 childCount, bool active, then class-specific data.

Strings are int32 length + UTF-8 bytes. Matrices are 16 floats, row-major,
DirectX row-vector convention: world = local @ parent.

Encrypted or obfuscated KN5 files are not supported. They are detected and
rejected with a clear error rather than decoded. This includes files protected
with Custom Shaders Patch encryption: those still parse, but their plain
section holds decoy textures and placeholder meshes, so converting them would
silently produce a broken track.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


class Kn5Error(Exception):
    pass


class Kn5UnsupportedError(Kn5Error):
    """Raised when a file is not a standard KN5 (e.g. encrypted/protected)."""


MESH_VERTEX_DTYPE = np.dtype([
    ("pos", "<f4", 3),
    ("normal", "<f4", 3),
    ("uv", "<f4", 2),
    ("tangent", "<f4", 3),
])  # 44 bytes

SKINNED_VERTEX_DTYPE = np.dtype([
    ("pos", "<f4", 3),
    ("normal", "<f4", 3),
    ("uv", "<f4", 2),
    ("tangent", "<f4", 3),
    ("weights", "<f4", 4),
    ("bone_indices", "<f4", 4),
])  # 76 bytes

# Markers that identify protected KN5 files (refused, never decrypted).
_PROTECTION_MARKERS = (b"__AC_SHADERS_PATCH_KN5ENC",)

# Sanity limits used to detect garbage (typically encrypted files).
_MAX_STRING = 4096
_MAX_COUNT = 50_000_000


@dataclass
class Kn5Texture:
    name: str
    active: bool
    data: Optional[bytes]  # None when textures were skipped


@dataclass
class Kn5ShaderProperty:
    name: str
    a: float
    b: tuple
    c: tuple
    d: tuple


@dataclass
class Kn5Material:
    name: str
    shader: str
    blend_mode: int  # 0 opaque, 1 alpha blend, 2 alpha-to-coverage
    alpha_tested: bool
    depth_mode: int
    properties: dict = field(default_factory=dict)  # name -> Kn5ShaderProperty
    mappings: dict = field(default_factory=dict)  # slot name -> texture name


@dataclass
class Kn5Node:
    node_class: int
    name: str
    active: bool
    children: list = field(default_factory=list)
    matrix: Optional[np.ndarray] = None  # 4x4, dummies only
    # mesh data
    cast_shadows: bool = True
    is_visible: bool = True
    is_transparent: bool = False
    is_renderable: bool = True
    vertices: Optional[np.ndarray] = None  # structured array
    indices: Optional[np.ndarray] = None  # uint16
    material_id: int = 0
    layer: int = 0
    lod_in: float = 0.0
    lod_out: float = 0.0

    @property
    def is_mesh(self) -> bool:
        return self.node_class in (2, 3)


@dataclass
class Kn5File:
    path: str
    version: int
    textures: list
    materials: list
    root: Kn5Node

    def iter_nodes(self):
        stack = [(self.root, np.identity(4, dtype=np.float64), (), True)]
        while stack:
            node, parent_world, path, parent_active = stack.pop()
            active = parent_active and node.active
            world = parent_world
            if node.node_class == 1 and node.matrix is not None:
                world = node.matrix.astype(np.float64) @ parent_world
            node_path = path + (node.name,)
            yield node, world, node_path, active
            for child in reversed(node.children):
                stack.append((child, world, node_path, active))


class _Reader:
    def __init__(self, data: bytes, path: str):
        self.buf = memoryview(data)
        self.pos = 0
        self.path = path

    def _need(self, n):
        if n < 0 or self.pos + n > len(self.buf):
            raise Kn5UnsupportedError(
                f"{self.path}: unexpected end of data at byte {self.pos} "
                f"(wanted {n} bytes). The file is truncated, encrypted or not a standard KN5."
            )

    def bytes(self, n) -> bytes:
        self._need(n)
        out = bytes(self.buf[self.pos:self.pos + n])
        self.pos += n
        return out

    def skip(self, n):
        self._need(n)
        self.pos += n

    def i32(self) -> int:
        self._need(4)
        v = struct.unpack_from("<i", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def u32(self) -> int:
        self._need(4)
        v = struct.unpack_from("<I", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def u8(self) -> int:
        self._need(1)
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def f32s(self, n) -> tuple:
        self._need(4 * n)
        v = struct.unpack_from(f"<{n}f", self.buf, self.pos)
        self.pos += 4 * n
        return v

    def count(self, what: str) -> int:
        n = self.i32()
        if n < 0 or n > _MAX_COUNT:
            raise Kn5UnsupportedError(
                f"{self.path}: implausible {what} count ({n}) at byte {self.pos - 4}. "
                "The file is probably encrypted or not a standard KN5."
            )
        return n

    def string(self) -> str:
        n = self.i32()
        if n < 0 or n > _MAX_STRING:
            raise Kn5UnsupportedError(
                f"{self.path}: implausible string length ({n}) at byte {self.pos - 4}. "
                "The file is probably encrypted or not a standard KN5."
            )
        raw = self.bytes(n)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")

    def array(self, dtype: np.dtype, n: int) -> np.ndarray:
        size = dtype.itemsize * n
        self._need(size)
        arr = np.frombuffer(self.buf, dtype=dtype, count=n, offset=self.pos).copy()
        self.pos += size
        return arr


def read_kn5(path: str, load_textures: bool = True) -> Kn5File:
    with open(path, "rb") as f:
        data = f.read()

    if data[:6] != b"sc6969":
        raise Kn5UnsupportedError(
            f"{path}: missing the 'sc6969' KN5 signature. This is either not a KN5 file "
            "or an encrypted/protected one, which this tool does not support."
        )

    if any(marker in data for marker in _PROTECTION_MARKERS):
        raise Kn5UnsupportedError(
            f"{path}: this KN5 is protected with Custom Shaders Patch encryption. Its readable "
            "section only contains placeholder textures and meshes, so it can't be converted. "
            "Ask the track's author for an unprotected version or permission to use it."
        )

    r = _Reader(data, path)
    r.skip(6)
    version = r.i32()
    if version < 1 or version > 64:
        raise Kn5UnsupportedError(f"{path}: unknown KN5 version {version}.")
    if version > 5:
        r.i32()  # extra header int

    textures = []
    for _ in range(r.count("texture")):
        active = r.i32() == 1
        name = r.string()
        size = r.u32()
        if load_textures:
            textures.append(Kn5Texture(name, active, r.bytes(size)))
        else:
            r.skip(size)
            textures.append(Kn5Texture(name, active, None))

    materials = []
    for _ in range(r.count("material")):
        m = Kn5Material(
            name=r.string(),
            shader=r.string(),
            blend_mode=r.u8(),
            alpha_tested=r.u8() != 0,
            depth_mode=r.i32(),
        )
        for _ in range(r.count("shader property")):
            pname = r.string()
            vals = r.f32s(10)
            m.properties[pname] = Kn5ShaderProperty(
                pname, vals[0], tuple(vals[1:3]), tuple(vals[3:6]), tuple(vals[6:10]))
        for _ in range(r.count("texture mapping")):
            slot_name = r.string()
            r.i32()  # slot index
            m.mappings[slot_name] = r.string()
        materials.append(m)

    root = _read_node_tree(r)

    if r.pos != len(data):
        # Trailing data is unusual but harmless; record it for diagnostics.
        pass

    return Kn5File(path=path, version=version, textures=textures, materials=materials, root=root)


def _read_node(r: _Reader) -> tuple:
    node_class = r.i32()
    if node_class not in (1, 2, 3):
        raise Kn5UnsupportedError(
            f"{r.path}: unknown node class {node_class} at byte {r.pos - 4}. "
            "The file is probably encrypted or not a standard KN5."
        )
    node = Kn5Node(node_class=node_class, name=r.string(), active=False)
    n_children = r.count("child node")
    node.active = r.u8() != 0

    if node_class == 1:
        node.matrix = np.array(r.f32s(16), dtype=np.float32).reshape(4, 4)
    elif node_class == 2:
        node.cast_shadows = r.u8() != 0
        node.is_visible = r.u8() != 0
        node.is_transparent = r.u8() != 0
        node.vertices = r.array(MESH_VERTEX_DTYPE, r.count("vertex"))
        node.indices = r.array(np.dtype("<u2"), r.count("index"))
        node.material_id = r.u32()
        node.layer = r.u32()
        node.lod_in, node.lod_out = r.f32s(2)
        r.f32s(4)  # bounding sphere center + radius
        node.is_renderable = r.u8() != 0
    else:  # skinned mesh
        node.cast_shadows = r.u8() != 0
        node.is_visible = r.u8() != 0
        node.is_transparent = r.u8() != 0
        for _ in range(r.count("bone")):
            r.string()
            r.f32s(16)
        node.vertices = r.array(SKINNED_VERTEX_DTYPE, r.count("vertex"))
        node.indices = r.array(np.dtype("<u2"), r.count("index"))
        node.material_id = r.u32()
        node.layer = r.u32()
        node.lod_in, node.lod_out = r.f32s(2)
        node.is_renderable = True

    return node, n_children


def _read_node_tree(r: _Reader) -> Kn5Node:
    # Iterative to avoid recursion limits on deep hierarchies.
    root, n = _read_node(r)
    stack = [(root, n)]
    while stack:
        parent, remaining = stack[-1]
        if remaining == 0:
            stack.pop()
            continue
        stack[-1] = (parent, remaining - 1)
        child, n_child = _read_node(r)
        parent.children.append(child)
        stack.append((child, n_child))
    return root
