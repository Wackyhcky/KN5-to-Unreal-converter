"""Car pipeline test: synthetic car -> converter -> Unreal car import (fake editor)."""

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

import fake_unreal as F  # noqa: E402
sys.modules["unreal"] = F

from ac2ue.car import convert_car, is_car_folder  # noqa: E402
import make_sample_car  # noqa: E402
import make_sample_track  # noqa: E402

UE_LIKE = np.array([[100.0, 0, 0], [0, 0, 100.0], [0, 100.0, 0]])     # (x, z, y) * 100
ROTATED = np.array([[0, 0, -100.0], [100.0, 0, 0], [0, 100.0, 0]])     # a different convention


def reset():
    F.ASSETS.clear(); F.ACTORS.clear(); F.LEVELS.clear(); F.CALLS.clear(); F.REGISTRY.clear()
    F._SUBS.clear()


def run_once(car_dir, out, mapping, with_template):
    reset()
    F.IMPORT_M = mapping
    if with_template:
        F.REGISTRY.append(F._AssetData("/Game/VehicleTemplate/Blueprints/BP_VehicleAdvPawnBase",
                                       {"NativeParentClass": "Class'/Script/ChaosVehicles.WheeledVehiclePawn'",
                                        "ParentClass": "Class'/Script/ChaosVehicles.WheeledVehiclePawn'"}))
        F.ASSETS["/Game/VehicleTemplate/Blueprints/BP_VehicleAdvPawnBase"] = F._BlueprintAsset("BP_VehicleAdvPawnBase", F.WheeledVehiclePawn)
        F.REGISTRY.append(F._AssetData("/Game/VehicleTemplate/Blueprints/ABP_SportsCar",
                                       {"ParentClass": "Class'/Script/ChaosVehicles.VehicleAnimationInstance'"}))
    manifest = os.path.join(out, "manifest.json")
    # Mimic Unreal's `py script.py args`: run the file in its own namespace,
    # which is NOT sys.modules["__main__"].
    path = os.path.join(ROOT, "unreal", "ue_import_track.py")
    ns = {"__name__": "__main__", "__file__": path}
    old_argv = sys.argv
    sys.argv = [path, manifest]
    try:
        exec(compile(open(path).read(), path, "exec"), ns)
    finally:
        sys.argv = old_argv
    return json.load(open(manifest))


def check(m, with_template):
    root = "/Game/ACTracks/Cars/test_gt"
    skm = F.ASSETS[root + "/Mesh/SK_test_gt"]
    # orientation: front-left wheel ends up forward (+X) and on the left (-Y) in Unreal
    lf, rr = skm.bones["WHEEL_LF"], skm.bones["WHEEL_RR"]
    assert lf[0] > 0 and lf[1] < 0 and rr[0] < 0 and rr[1] > 0, (lf, rr)
    # importer side materials removed, our instances assigned
    assert not [k for k in F.ASSETS if k.endswith("_imported")]
    slots = {str(s._props["material_slot_name"]): s._props["material_interface"].get_name()
             for s in skm._props["materials"]}
    assert slots["MI_carpaint"] == "MI_carpaint", slots
    # physics asset: wheels kinematic + no collision, root box from collider
    bodies = {b._props["bone_name"]: b for b in skm._props["physics_asset"]._props["skeletal_body_setups"]}
    assert bodies["WHEEL_LF"]._props["physics_type"].endswith("KINEMATIC")
    box = bodies["root"]._props["agg_geom"]._props["box_elems"][0]
    assert abs(box._props["x"] - 430) < 1 and abs(box._props["y"] - 170) < 1, box._props
    # wheels and drivetrain
    wf = F.ASSETS[root + "/Blueprints/BP_test_gt_WheelFront"].cdo._props
    wr = F.ASSETS[root + "/Blueprints/BP_test_gt_WheelRear"].cdo._props
    assert abs(wf["wheel_radius"] - 31) < 1e-6 and abs(wr["wheel_width"] - 22.5) < 1e-6
    assert wf["max_steer_angle"] == 30.0 and wf["affected_by_engine"] and not wr["affected_by_engine"]
    veh = F.ASSETS[root + "/Blueprints/BP_test_gt"]
    mv = veh.cdo._props["vehicle_movement_component"]._props
    assert [s._props["bone_name"] for s in mv["wheel_setups"]] == ["WHEEL_LF", "WHEEL_RF", "WHEEL_LR", "WHEEL_RR"]
    assert mv["mass"] == 1150 and mv["differential_setup"]._props["differential_type"].endswith("FRONT_WHEEL_DRIVE")
    assert mv["transmission_setup"]._props["forward_gear_ratios"] == [3.6, 2.1, 1.4, 1.05, 0.82]
    keys = mv["engine_setup"]._props["torque_curve"]._props["editor_curve_data"]._props["keys"]
    assert abs(keys[2]._props["value"] - 1.0) < 1e-6  # normalised at the 260 Nm peak
    assert veh.cdo._props["mesh"]._props.get("anim_class") is not None
    if with_template:
        assert veh.parent.bp.get_name() == "BP_VehicleAdvPawnBase"
    else:
        assert veh.parent is F.WheeledVehiclePawn
    # livery child blueprint overrides only the paint
    blue = F.ASSETS[root + "/Blueprints/BP_test_gt_01_blue"]
    assert blue.parent.bp is veh
    over = [mi.get_name() for mi in blue.cdo._props["mesh"]._props["override_materials"]]
    assert "MI_carpaint__01_blue" in over and "MI_rims" in over, over
    assert "/Game/ACTracks/Cars/test_gt/Blueprints/BP_test_gt_00_soul_red" not in F.ASSETS  # default skin = base


def main():
    work = tempfile.mkdtemp(prefix="ac2ue_car_")
    try:
        cars = os.path.join(work, "cars")
        car = make_sample_car.build(cars)
        track = make_sample_track.build(os.path.join(work, "tracks"))
        assert is_car_folder(car) and not is_car_folder(track)
        out = os.path.join(work, "out")
        convert_car(car, out, stream=open(os.devnull, "w"))

        for mapping, template in ((UE_LIKE, True), (ROTATED, False)):
            m = run_once(car, out, mapping, template)
            check(m, template)
            remapped = any(c[0] == "import" and "_ue_SK_test_gt.glb" in c[1] for c in F.CALLS)
            print(f"car import ok (template={template}, re-oriented={remapped})")
            assert remapped == (mapping is ROTATED)

        # Template added after a first import: re-run reparents and swaps the empty ABP
        F.REGISTRY.append(F._AssetData("/Game/VehicleTemplate/Blueprints/BP_VehicleAdvPawnBase",
                                       {"NativeParentClass": "WheeledVehiclePawn", "ParentClass": "WheeledVehiclePawn"}))
        F.ASSETS["/Game/VehicleTemplate/Blueprints/BP_VehicleAdvPawnBase"] = F._BlueprintAsset("BP_VehicleAdvPawnBase", F.WheeledVehiclePawn)
        F.REGISTRY.append(F._AssetData("/Game/VehicleTemplate/ABP_SportsCar", {"ParentClass": "VehicleAnimationInstance"}))
        importlib.import_module("ue_import_track").main([os.path.join(out, "manifest.json"), "--reuse-existing"])
        veh = F.ASSETS["/Game/ACTracks/Cars/test_gt/Blueprints/BP_test_gt"]
        assert veh.parent.bp.get_name() == "BP_VehicleAdvPawnBase"
        assert ("delete", "/Game/ACTracks/Cars/test_gt/Blueprints/ABP_test_gt") in F.CALLS
        print("re-run after adding the Vehicle template ok")

        # no data/ folder: physics estimated, still drivable
        shutil.rmtree(car)
        car2 = make_sample_car.build(cars, with_data=False)
        out2 = os.path.join(work, "out2")
        convert_car(car2, out2, stream=open(os.devnull, "w"))
        m2 = json.load(open(os.path.join(out2, "manifest.json")))
        assert m2["physics"]["source"] == "estimated" and m2["drivable"]
        assert abs(m2["physics"]["front"]["radius"] - 0.467) < 0.01  # from the wheel mesh
        print("estimated-physics car ok")
        print("ALL CAR TESTS PASSED")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
