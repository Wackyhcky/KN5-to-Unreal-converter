"""Builds a synthetic AC car folder for testing (real KN5 conventions:
right-handed, +Y up, +X = car's LEFT, +Z = forward, CCW front faces)."""

import json
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_sample_track import dummy, material, s, tex_bytes, write_kn5  # noqa: E402


def box_mesh(name, lo, hi, mat_id):
    """Closed box, outward normals, CCW winding seen from outside."""
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    c = (lo + hi) / 2
    verts, idx = b"", []
    n = 0
    for axis in range(3):
        for sign in (-1, 1):
            normal = np.zeros(3)
            normal[axis] = sign
            u, v = [a for a in range(3) if a != axis]
            quad = []
            for du, dv in ((0, 0), (1, 0), (1, 1), (0, 1)):
                p = c.copy()
                p[axis] = hi[axis] if sign > 0 else lo[axis]
                p[u] = lo[u] if du == 0 else hi[u]
                p[v] = lo[v] if dv == 0 else hi[v]
                quad.append(p)
            if np.dot(np.cross(quad[1] - quad[0], quad[2] - quad[0]), normal) < 0:
                quad = [quad[0], quad[3], quad[2], quad[1]]
            for p, uv in zip(quad, ((0, 0), (1, 0), (1, 1), (0, 1))):
                verts += struct.pack("<3f3f2f3f", *p, *normal, *uv, 1, 0, 0)
            idx += [n, n + 1, n + 2, n, n + 2, n + 3]
            n += 4
    head = struct.pack("<i", 2) + s(name) + struct.pack("<i?", 0, True) + struct.pack("<???", True, True, False)
    body = struct.pack("<I", n) + verts + struct.pack("<I", len(idx)) + np.array(idx, "<u2").tobytes()
    body += struct.pack("<II", mat_id, 0) + struct.pack("<ff4f?", 0, 0, 0, 0, 0, 1, True)
    return head + body


def translate(x, y, z):
    m = np.identity(4, np.float32)
    m[3, :3] = (x, y, z)
    return m


def build(root_dir, with_data=True):
    car = os.path.join(root_dir, "test_gt")
    os.makedirs(os.path.join(car, "ui"), exist_ok=True)
    with open(os.path.join(car, "ui", "ui_car.json"), "w") as f:
        json.dump({"name": "Test GT"}, f)

    textures = [
        ("Skin_00.dds", tex_bytes((174, 174, 174))),
        ("metal_detail.dds", tex_bytes((126, 1, 0))),
        ("damage_nm.dds", tex_bytes((128, 128, 255))),
        ("rim.dds", tex_bytes((200, 200, 205))),
        ("tyre.dds", tex_bytes((30, 30, 30))),
        ("glass.png", tex_bytes((20, 20, 25), fmt="png", alpha=True)),
        ("INT_Decals.dds", tex_bytes((90, 90, 90))),
    ]
    mats = [
        material("carpaint", "ksPerPixelMultiMap_damage_dirt",
                 props={"ksSpecular": 0.5, "ksSpecularEXP": 50.0, "useDetail": 1.0, "ksDiffuse": 0.4},
                 maps={"txDiffuse": "Skin_00.dds", "txDetail": "metal_detail.dds",
                       "txNormal": "damage_nm.dds"}),
        material("rims", "ksPerPixelMultiMap", maps={"txDiffuse": "rim.dds"}),
        material("tyres", "ksTyres", maps={"txDiffuse": "tyre.dds"}),
        material("glass", "ksPerPixel", blend=1, maps={"txDiffuse": "glass.png"}),
        material("interior", "ksPerPixel", maps={"txDiffuse": "int_decals.dds"}),  # case differs
    ]

    def wheel(tag, x, z):
        return dummy(f"WHEEL_{tag}", [
            box_mesh(f"RIM_{tag}", (-0.11, -0.33, -0.33), (0.11, 0.33, 0.33), 1),
            box_mesh(f"TYRE_{tag}", (-0.1, -0.31, -0.31), (0.1, 0.31, 0.31), 2),
            box_mesh(f"RIM_BLUR_{tag}", (-0.11, -0.33, -0.33), (0.11, 0.33, 0.33), 1),
        ], matrix=translate(x, 0.33, z))

    root = dummy("test_gt", [
        box_mesh("BODY", (-0.9, 0.25, -2.2), (0.9, 1.2, 2.2), 0),
        box_mesh("GLASS", (-0.7, 1.2, -0.6), (0.7, 1.45, 0.8), 3),
        dummy("STEER_HR", [box_mesh("STEER_HR_mesh", (0.2, 0.9, 0.4), (0.5, 1.1, 0.45), 4)]),
        dummy("STEER_LR", [box_mesh("STEER_LR_mesh", (0.2, 0.9, 0.4), (0.5, 1.1, 0.45), 4)]),
        dummy("FRONT_BUMPER_DAMAGE", [box_mesh("BUMPER_DAMAGE", (-0.9, 0.2, 2.2), (0.9, 0.5, 2.4), 0)]),
        # +X is the car's left: LF has positive X, front has positive Z.
        wheel("LF", 0.8, 1.35), wheel("RF", -0.8, 1.35),
        wheel("LR", 0.8, -1.25), wheel("RR", -0.8, -1.25),
    ])
    write_kn5(os.path.join(car, "test_gt.kn5"), textures, mats, root)
    write_kn5(os.path.join(car, "collider.kn5"), [], [material("col", "ksPerPixel")],
              dummy("collider", [box_mesh("col0", (-0.85, 0.3, -2.15), (0.85, 1.3, 2.15), 0)]))

    for skin, color, fname in (("00_soul_red", (126, 1, 0), "metal_detail.dds"),
                               ("01_blue", (10, 30, 160), "METAL_DETAIL.DDS")):
        d = os.path.join(car, "skins", skin)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, fname), "wb") as f:
            f.write(tex_bytes(color))
        with open(os.path.join(d, "preview.jpg"), "wb") as f:
            f.write(b"not a texture")

    if with_data:
        d = os.path.join(car, "data")
        os.makedirs(d, exist_ok=True)
        files = {
            "car.ini": "[BASIC]\nTOTALMASS=1150\n[CONTROLS]\nSTEER_LOCK=450\nSTEER_RATIO=15\n",
            "drivetrain.ini": "[TRACTION]\nTYPE=FWD\n[GEARS]\nCOUNT=5\nGEAR_R=-3.3\nGEAR_1=3.6\n"
                              "GEAR_2=2.1\nGEAR_3=1.4\nGEAR_4=1.05\nGEAR_5=0.82\nFINAL=4.1\n",
            "engine.ini": "[HEADER]\nPOWER_CURVE=power.lut\n[ENGINE_DATA]\nMINIMUM=1000\nLIMITER=7500\n",
            "power.lut": "0|150\n2000|210\n4500|260 ; peak\n7500|200\n",
            "tyres.ini": "[FRONT]\nRADIUS=0.31\nWIDTH=0.205\n[REAR]\nRADIUS=0.31\nWIDTH=0.225\n",
            "suspensions.ini": "[FRONT]\nSPRING_RATE=60000\n[REAR]\nSPRING_RATE=55000\n",
        }
        for name, body in files.items():
            with open(os.path.join(d, name), "w") as f:
                f.write(body)
    else:
        with open(os.path.join(car, "data.acd"), "wb") as f:
            f.write(b"\x00" * 64)
    return car


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "sample_cars"))
