"""End-to-end test: synthetic track -> converter -> import script (against fake_unreal)."""

import importlib
import json
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "unreal"))

import fake_unreal  # noqa: E402
sys.modules["unreal"] = fake_unreal

from ac2ue.convert import convert  # noqa: E402
import make_sample_track  # noqa: E402


def main():
    work = tempfile.mkdtemp(prefix="ac2ue_test_")
    try:
        track = make_sample_track.build(os.path.join(work, "src"))
        manifest_path = convert(track, os.path.join(work, "out"), stream=open(os.devnull, "w"))
        manifest = json.load(open(manifest_path))

        ue = importlib.import_module("ue_import_track")
        ue.main([manifest_path])

        F = fake_unreal
        levels = {k.rsplit("/", 1)[-1]: v for k, v in F.LEVELS.items()}
        assert set(levels) == {"L_test_ring_gp", "L_test_ring_national"}, levels.keys()

        # 1. Placement: actor location + imported local verts must equal the
        #    simulated importer's mapping of the converter's world-space glTF data.
        checked = 0
        for layout in manifest["layouts"]:
            actors = [a for a in levels[layout["level"]] if isinstance(a.obj, F.StaticMesh)]
            by_label = {}
            for a in actors:
                by_label.setdefault(a.label, []).append(a)
            for model in layout["models"]:
                kn5 = manifest["kn5"][model["kn5"]]
                for i, m in enumerate(kn5["meshes"]):
                    a = by_label[m["name"]].pop(0)
                    loc = np.array([a.loc.x, a.loc.y, a.loc.z])
                    world_ue = a.obj.verts + loc
                    local = F._read_glb_positions(os.path.join(os.path.dirname(manifest_path), m["file"]))
                    expected = (local + np.array(m["center"]) + np.array(model["position"])) @ F.IMPORT_M.T
                    assert np.allclose(world_ue, expected, atol=1e-3), (m["name"], world_ue[:2], expected[:2])
                    comp = a.get_editor_property("static_mesh_component")
                    key = model["surfaces"].get(str(i))
                    if key:
                        assert comp.collision == "BlockAll", m["name"]
                        smi = comp.mat[0]
                        pm = smi._props["phys_material"]
                        assert pm._props["friction"] == float(layout["surfaces"][key]["FRICTION"]), (m["name"], key)
                        assert smi._props["parent"] is a.obj.materials[0]
                    else:
                        assert comp.collision == "NoCollision", m["name"]
                    if not (m["visible"] and m["renderable"]):
                        assert a.hidden, m["name"]
                        assert comp._props.get("visible") is False, m["name"]  # hidden in editor too
                    checked += 1
        print(f"placement/collision/surface checks passed for {checked} actors")

        # 2. Importer side-assets were cleaned up, meshes have the right material
        leftovers = [k for k, v in F.ASSETS.items() if "/Meshes/" in k and not isinstance(v, F.StaticMesh)]
        assert not leftovers, leftovers
        mesh = F.ASSETS["/Game/ACTracks/test_ring/Meshes/test_ring/SM_1GRASS_infield"]
        mi = mesh.materials[0]
        assert mi._props["parent"].get_name() == "M_AC_Multilayer"
        assert mi._props["scalar:MultB"] == 30.0
        assert "tex:DetailR" in mi._props
        asphalt = F.ASSETS["/Game/ACTracks/test_ring/Materials/test_ring/MI_asphalt"]
        nm = asphalt._props["tex:Normal"]
        assert nm._props["compression_settings"].endswith("TC_NORMALMAP") and nm._props["srgb"] is False
        assert mi._props["vector:MeanR"].rgba[1] > mi._props["vector:MeanR"].rgba[2]  # green-ish layer
        tree = F.ASSETS["/Game/ACTracks/test_ring/Materials/test_ring/MI_tree"]
        assert tree._props["parent"].get_name() == "M_AC_Masked"
        master = F.ASSETS["/Game/ACTracks/_Shared/Masters/M_AC_Opaque"]
        assert master._props["meta:AC2UE_MASTER_VERSION"] == ue.MASTER_VERSION
        g = F.ASSETS["/Game/ACTracks/test_ring/Materials/test_ring/MI_bbgrass"]
        assert g._props["scalar:Dither"] == 1.0 and abs(g._props["vector:Tint"].rgba[0] - 0.475 ** 2.2) < 1e-3
        actors = {a.label: a for a in levels["L_test_ring_gp"] if isinstance(a.obj, F.StaticMesh)}
        marker = actors["AC_START_0"]
        assert marker.get_editor_property("static_mesh_component")._props.get("visible") is False
        assert "AC_HELPER" in marker.get_editor_property("tags")
        assert actors["AC_POBJECT_cone"].get_editor_property("static_mesh_component")._props.get("visible") is not False
        print("materials/textures checks passed")

        # 3. Surface keys per layout: SANDTRAP only defined for gp; national falls back to SAND
        pms = sorted(k.rsplit("/", 1)[-1] for k, v in F.ASSETS.items() if isinstance(v, F.PhysicalMaterial))
        print("physical materials:", pms)
        assert "PM_AC_SANDTRAP" in pms and "PM_AC_SAND" in pms

        # 4. Re-running is idempotent: same actor counts, nothing duplicated
        # An outdated master is rebuilt in place (same asset), not duplicated
        master._props["meta:AC2UE_MASTER_VERSION"] = "1"
        counts = {k: len(v) for k, v in F.LEVELS.items()}
        ue.main([manifest_path, "--reuse-existing"])
        counts2 = {k: len(v) for k, v in F.LEVELS.items()}
        assert counts == counts2, (counts, counts2)
        assert F.ASSETS["/Game/ACTracks/_Shared/Masters/M_AC_Opaque"] is master
        assert ("clear_graph", "M_AC_Opaque") in F.CALLS
        print("re-run idempotency check passed:", counts2)
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
