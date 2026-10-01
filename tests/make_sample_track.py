"""Builds a synthetic multi-layout AC track for testing the converter.

Geometry follows real KN5 conventions: right-handed, +Y up, meters,
counter-clockwise front faces (numeric cross(b-a, c-a) points along the normal).
"""

import io
import os
import struct
import sys

import numpy as np
from PIL import Image


def s(text):
    b = text.encode("utf-8")
    return struct.pack("<i", len(b)) + b


def tex_bytes(color, size=32, fmt="dds", alpha=None, pixel_format="DXT1"):
    img = Image.new("RGBA", (size, size), tuple(color) + (255,))
    if alpha is not None:
        a = Image.linear_gradient("L").resize((size, size))
        img.putalpha(a)
    buf = io.BytesIO()
    if fmt == "dds":
        img.save(buf, "DDS", pixel_format=pixel_format)
    else:
        img.save(buf, "PNG")
    return buf.getvalue()


def material(name, shader, blend=0, alpha_tested=False, props=None, maps=None):
    out = s(name) + s(shader) + struct.pack("<B?i", blend, alpha_tested, 0)
    props = props or {}
    out += struct.pack("<i", len(props))
    for k, v in props.items():
        a, c = (v, (0, 0, 0)) if not isinstance(v, tuple) else (0.0, v)
        out += s(k) + struct.pack("<f2f3f4f", a, 0, 0, *c, 0, 0, 0, 0)
    maps = maps or {}
    out += struct.pack("<i", len(maps))
    for i, (slot, tex) in enumerate(maps.items()):
        out += s(slot) + struct.pack("<i", i) + s(tex)
    return out


def dummy(name, children, matrix=None, active=True):
    m = np.identity(4, dtype=np.float32) if matrix is None else np.asarray(matrix, np.float32)
    return struct.pack("<i", 1) + s(name) + struct.pack("<i?", len(children), active) + \
        m.astype("<f4").tobytes() + b"".join(children)


def quad_mesh(name, corners, normal, mat_id, uv_scale=1.0, visible=True, renderable=True,
              skinned=False):
    """corners: 4 points; two triangles (0,1,2),(0,2,3) in AC winding."""
    corners = np.asarray(corners, np.float32)
    uvs = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32) * uv_scale
    verts = b""
    for p, uv in zip(corners, uvs):
        verts += struct.pack("<3f3f2f3f", *p, *normal, *uv, 1, 0, 0)
        if skinned:
            verts += struct.pack("<4f4f", 1, 0, 0, 0, 0, 0, 0, 0)
    idx = np.array([0, 1, 2, 0, 2, 3], "<u2").tobytes()
    head = struct.pack("<i", 3 if skinned else 2) + s(name) + struct.pack("<i?", 0, True)
    head += struct.pack("<???", True, visible, False)
    if skinned:
        head += struct.pack("<i", 1) + s("bone0") + np.identity(4, "<f4").tobytes()
    body = struct.pack("<I", 4) + verts + struct.pack("<I", 6) + idx + struct.pack("<II", mat_id, 0)
    body += struct.pack("<ff", 0.0, 0.0 if name != "far_billboard" else 500.0)
    if not skinned:
        body += struct.pack("<4f", 0, 0, 0, 10) + struct.pack("<?", renderable)
    return head + body


def ground(x0, z0, x1, z1, y=0.0):
    # numeric cross of (b-a, c-a) = +Y -> CCW, up-facing
    return [(x0, y, z0), (x0, y, z1), (x1, y, z1), (x1, y, z0)]


def wall(x0, z, x1, h):
    # faces -Z; CCW (numeric cross = -Z)
    return [(x0, 0, z), (x0, h, z), (x1, h, z), (x1, 0, z)]


def write_kn5(path, textures, materials, root, version=6):
    out = b"sc6969" + struct.pack("<i", version)
    if version > 5:
        out += struct.pack("<i", 0)
    out += struct.pack("<i", len(textures))
    for name, data in textures:
        out += struct.pack("<i", 1) + s(name) + struct.pack("<I", len(data)) + data
    out += struct.pack("<i", len(materials)) + b"".join(materials)
    out += root
    with open(path, "wb") as f:
        f.write(out)


def build(root_dir):
    track = os.path.join(root_dir, "test_ring")
    os.makedirs(track, exist_ok=True)
    up = (0, 1, 0)

    textures = [
        ("asphalt.dds", tex_bytes((60, 60, 65))),
        ("asphalt_nm.dds", tex_bytes((128, 128, 255))),
        ("asphalt_maps.dds", tex_bytes((255, 200, 0))),
        ("grass_base.png", tex_bytes((90, 140, 60), fmt="png")),
        ("grass_mask.dds", tex_bytes((255, 0, 0))),
        ("detail_r.dds", tex_bytes((100, 160, 70))),
        ("detail_g.dds", tex_bytes((140, 120, 80))),
        ("detail_b.dds", tex_bytes((200, 190, 150))),
        ("detail_a.dds", tex_bytes((80, 80, 80))),
        ("tree.dds", tex_bytes((40, 100, 40), alpha=True, pixel_format="DXT5")),
        ("kerb.dds", tex_bytes((220, 30, 30))),
        ("glass.png", tex_bytes((150, 200, 255), fmt="png", alpha=True)),
        ("broken.dds", b"DDS " + b"\x00" * 40),  # undecodable
    ]
    mats = [
        material("asphalt", "ksPerPixelNM",
                 props={"ksSpecular": 0.2, "ksSpecularEXP": 30.0, "ksDiffuse": 0.4},
                 maps={"txDiffuse": "asphalt.dds", "txNormal": "asphalt_nm.dds",
                       "txMaps": "asphalt_maps.dds"}),
        material("grass", "ksMultilayer_fresnel_nm",
                 props={"multR": 20.0, "multG": 15.0, "multB": 30.0, "multA": 10.0},
                 maps={"txDiffuse": "grass_base.png", "txMask": "grass_mask.dds",
                       "txDetailR": "detail_r.dds", "txDetailG": "detail_g.dds",
                       "txDetailB": "detail_b.dds", "txDetailA": "detail_a.dds"}),
        material("tree", "ksTree", alpha_tested=True, maps={"txDiffuse": "tree.dds"}),
        material("kerb", "ksPerPixelMultiMap",
                 props={"useDetail": 1.0, "detailUVMultiplier": 8.0},
                 maps={"txDiffuse": "kerb.dds", "txDetail": "asphalt.dds",
                       "txMaps": "asphalt_maps.dds"}),
        material("glass", "ksPerPixel", blend=1, maps={"txDiffuse": "glass.png"}),
        material("lamp", "ksPerPixel", props={"ksEmissive": (5.0, 4.0, 2.0)},
                 maps={"txDiffuse": "broken.dds", "txVariation": "missing.dds"}),
        material("physics", "ksPerPixel"),
        material("bbgrass", "ksGrass", props={"ksDiffuse": 0.19, "ksSpecular": 0.0},
                 maps={"txDiffuse": "tree.dds"}),
    ]
    T = np.identity(4, np.float32)
    T[3, :3] = (100, 0, 50)  # row-vector translation
    mirror = np.diag([-1, 1, 1, 1]).astype(np.float32)
    mirror[3, :3] = (20, 0, 0)

    root = dummy("test_ring", [
        dummy("road_group", [
            quad_mesh("1ROAD_main", ground(0, 0, 200, 12), up, 0, uv_scale=20),
            quad_mesh("1KERB_t1", ground(0, 12, 200, 13.5, 0.02), up, 3, uv_scale=10),
        ], matrix=T),
        quad_mesh("1GRASS_infield", ground(-50, -50, 400, 150, -0.05), up, 1),
        quad_mesh("2SANDTRAP_t1", ground(210, 0, 250, 40, -0.03), up, 1),
        quad_mesh("1WALL_pit", wall(0, -5, 200, 1.2), (0, 0, -1), 6, visible=False, renderable=False),
        dummy("mirrored", [quad_mesh("tree_card", wall(0, 30, 6, 8), (0, 0, -1), 2)], matrix=mirror),
        dummy("disabled", [quad_mesh("old_stand", ground(0, 0, 5, 5, 3), up, 4)], active=False),
        quad_mesh("pit_glass", wall(10, -8, 30, 3), (0, 0, -1), 4),
        quad_mesh("lamp_post", wall(0, -10, 1, 6), (0, 0, -1), 5),
        quad_mesh("far_billboard", wall(0, 60, 30, 10), (0, 0, -1), 0),
        quad_mesh("flag", wall(0, -20, 2, 4), (0, 0, -1), 4, skinned=True),
        quad_mesh("AC_START_0", ground(10, 5, 11, 6, 0.5), up, 0),
        quad_mesh("AC_POBJECT_cone", ground(20, 5, 21, 6, 0.5), up, 0),
        quad_mesh("bbgr_HI_1_KSLAYER5", ground(-40, -40, 0, 0, 0.1), up, 7),
    ])
    write_kn5(os.path.join(track, "test_ring.kn5"), textures, mats, root)

    extras_root = dummy("extras", [
        quad_mesh("grandstand", wall(0, -30, 60, 15), (0, 0, -1), 0),
    ])
    write_kn5(os.path.join(track, "test_ring_gp.kn5"),
              [("asphalt.dds", tex_bytes((60, 60, 65)))],
              [material("concrete", "ksPerPixel", maps={"txDiffuse": "asphalt.dds"})],
              extras_root, version=5)

    with open(os.path.join(track, "models_gp.ini"), "w") as f:
        f.write("[MODEL_0]\nFILE=test_ring.kn5\nPOSITION=0,0,0\nROTATION=0,0,0\n\n"
                "[MODEL_1]\nFILE=test_ring_gp.kn5\nPOSITION=5,0,-2 ; offset\nROTATION=0,0,0\n")
    with open(os.path.join(track, "models_national.ini"), "w") as f:
        f.write("[MODEL_0]\nFILE=test_ring.kn5\n")

    for layout, extra in (("gp", True), ("national", False)):
        d = os.path.join(track, layout, "data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "surfaces.ini"), "w") as f:
            f.write("[SURFACE_0]\nKEY=ROAD\nFRICTION=0.97\nDAMPING=0\nWAV=\nIS_VALID_TRACK=1\n\n"
                    "[SURFACE_1]\nKEY=KERB\nFRICTION=0.92\nIS_VALID_TRACK=1\n\n"
                    "[SURFACE_2]\nKEY=GRASS\nFRICTION=0.6\nIS_VALID_TRACK=0\nDIRT_ADDITIVE=1\n")
            if extra:
                f.write("\n[SURFACE_3]\nKEY=SANDTRAP\nFRICTION=0.5\nDAMPING=0.2\nIS_VALID_TRACK=0\n")

    # A file that is not a standard KN5 (stands in for an encrypted/protected one)
    with open(os.path.join(root_dir, "protected.kn5"), "wb") as f:
        f.write(os.urandom(4096))
    # A structurally valid KN5 carrying the CSP protection trailer
    with open(os.path.join(track, "test_ring.kn5"), "rb") as src, \
            open(os.path.join(root_dir, "csp_protected.kn5"), "wb") as f:
        f.write(src.read() + b"__AC_SHADERS_PATCH_KN5ENC_v1__")
    with open(os.path.join(root_dir, "garbled.kn5"), "wb") as f:
        f.write(b"sc6969" + struct.pack("<i", 6) + os.urandom(4096))
    return track


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "sample_tracks"))
