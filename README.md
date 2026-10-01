# KN5 to Unreal V2

Converts Assetto Corsa tracks and cars into Unreal Engine 5. Tracks become static meshes, materials, physics surfaces with collision, and one level per layout. Cars become drivable Chaos vehicles with every livery (see [Cars](#cars)).

It works in two steps:

1. **Converter** (runs on your PC): reads the KN5 files and writes meshes (`.glb`), textures (`.png`) and a `manifest.json`.
2. **Unreal import script** (runs inside the editor): builds materials, imports everything, sets up collision and physical materials, and creates the levels.

## Requirements

- Python 3.9+ with `numpy` and `Pillow` → `pip install -r requirements.txt` (or run `install.bat` on Windows)
- Unreal Engine 5.1 or newer (5.3+ recommended), with these plugins enabled (Edit → Plugins):
  - **Python Editor Script Plugin**
  - **Interchange / glTF importer** (on by default in recent 5.x versions)

## Step 1: Convert

**Desktop app:** double-click `kn5conv_gui.bat` (or run `python kn5conv_gui.py`). Pick the track folder and an output folder, then click **Convert**.

**Command line:**

```
python kn5conv.py "C:/Program Files (x86)/Steam/steamapps/common/assettocorsa/content/tracks/mytrack" "C:/ac2ue/mytrack"
```

Options:

| Option | What it does |
|---|---|
| `--ac-root <AC folder>` | Reads AC's own `system/data/surfaces.ini` for surface keys the track doesn't define (recommended) |
| `--layouts gp,national` | Convert only some layouts |
| `--include-inactive` | Also export meshes under inactive nodes (AC normally hides them) |
| `-v` | Verbose log |

## Step 2: Import into Unreal

1. Open your project and go to **Window → Output Log**.
2. Make sure the box at the bottom is set to **Cmd**.
3. Run the following command. The desktop app's **Copy Unreal command** button copies it for you.

```
py "C:/path/to/kn5_to_unreal/unreal/ue_import_track.py" "C:/ac2ue/mytrack/manifest.json"
```

Optional flags, added after the manifest path:

| Flag | What it does |
|---|---|
| `--dest /Game/ACTracks` | Content folder to import into |
| `--layouts gp` | Only build some layouts |
| `--nanite` | Enable Nanite on opaque and masked meshes |
| `--no-lighting` | Don't add sun, sky and fog to new levels |
| `--reuse-existing` | Skip re-importing meshes and textures that already exist. This makes rebuilding levels fast |
| `--rebuild-masters` | Recreate the shared master materials |

Re-running the script is safe: existing levels are updated and actors are replaced, not duplicated.

## What you get

```
/Game/ACTracks/_Shared/Masters        M_AC_Opaque, M_AC_Masked, M_AC_Translucent, M_AC_Multilayer
/Game/ACTracks/<track>/Textures/...   sRGB, normal-map and mask compression set per texture use
/Game/ACTracks/<track>/Materials/...  one material instance per KN5 material, every value editable
/Game/ACTracks/<track>/Meshes/...     one static mesh per KN5 mesh
/Game/ACTracks/<track>/Physics/...    PM_AC_<KEY> physical materials (friction from surfaces.ini)
/Game/ACTracks/<track>/Levels/...     L_<track>_<layout>, one per layout
```

In each level, actors are placed at their original positions. Each has its mesh name as its label and is organized in the Outliner under `AC/<kn5>/Visual` or `AC/<kn5>/Surfaces/<KEY>`.

### Collision and surfaces

Assetto Corsa uses meshes named `<number><KEY>`, such as `1ROAD`, `2GRASS` or `1KERB_03`, as physics surfaces. The `KEY` refers to the layout's `data/surfaces.ini`. The importer does the following for these meshes:

- It gives them per-polygon collision (complex-as-simple, `BlockAll`). All other meshes get `NoCollision`, just like in AC.
- It assigns a material-instance variant whose **Physical Material** is `PM_AC_<KEY>`, with friction from `surfaces.ini`. Chaos Vehicles read this friction directly.
- It stores every `surfaces.ini` value (such as `IS_VALID_TRACK`, `DAMPING` and `DIRT_ADDITIVE`) as metadata on the physical material.
- It tags actors with `AC_SURFACE=<KEY>` and `AC_VALID_TRACK=0/1`, so gameplay code (track limits, for example) can query them.
- It keeps physics-only meshes (AC flags them non-renderable; many tracks put them in their own KN5, like Spa's `3.kn5`). They're hidden in the editor and in game, and grouped under `AC/<kn5>/Hidden` in the Outliner, but their collision stays on.

When two layouts define the same key differently, each gets its own physical material, for example `PM_AC_GRASS_gp`.

### Materials

AC shaders are mapped onto four master materials. The mapping is an approximation, because AC uses a Blinn-Phong style renderer and Unreal is physically based:

| AC shader family | Master | Notes |
|---|---|---|
| ksPerPixel, ksPerPixelNM, ksPerPixelMultiMap… | Opaque | txDiffuse, txNormal, txMaps (R spec, G gloss), txDetail × 2 (neutral at mid-grey) masked by diffuse alpha |
| alpha-tested, ksTree, ksGrass, *AT*, alpha-blend with ksAlphaRef | Masked | two-sided, clip at ksAlphaRef when usable, otherwise 0.5. ksGrass shell layers use dithered alpha so they stay soft |
| alpha-blend materials | Translucent | opacity from diffuse alpha |
| ksMultilayer* | Multilayer | txDiffuse × (txDetailR/G/B/A weighted by txMask, normalised). Details are projected from world position, tiling `multX` times per metre, as in AC. Object-space variants (`_objsp`) and layers with zero tiling use the layer's average colour |

Roughness comes from `ksSpecularEXP` with `ksSpecular` folded in (0 means fully matte, as in AC). Road shaders use `tarmacSpecularMultiplier` where `fresnelMaxLevel` asks for a sheen. Emissive comes from `ksEmissive`. Each material's `ksDiffuse` becomes its Tint (relative to 0.4), so relative brightness matches AC. AC's spawn and timing markers (`AC_START_*`, `AC_PIT_*`, `AC_TIME_*`…) are hidden; `AC_POBJECT*` props stay visible. Master materials are versioned and rebuilt in place automatically when the script updates them. Every value can be edited on the material instances (Roughness, Specular, Tint, EmissiveColor, DetailTiling and so on). The original AC values are kept in `manifest.json` under `ac_properties`.

## Cars

Point the converter (or the desktop app) at a car folder such as `content/cars/ks_mazda_mx5_nd`. Car folders are detected automatically, and `--car` forces it. The Unreal command is the same `py ".../ue_import_track.py" ".../manifest.json"`, and the script recognises a car manifest.

**Before importing a car**, enable the **Chaos Vehicles** plugin. For a car you can drive straight away, also add the Vehicle template content to your project: Content Browser > **Add** > **Add Feature or Content Pack** > **Vehicle**. The script derives your car from that template's pawn, which gives it keyboard and gamepad input, a chase camera, and the wheel animation Blueprint. Python can't author those parts itself. Without the template the car is still built completely, and the Output Log tells you what's missing. Adding the template later and re-running with `--reuse-existing` upgrades the car in place.

What gets built under `/Game/ACTracks/Cars/<car>/`:

| Asset | What it is |
|---|---|
| `SK_<car>` | One skeletal mesh with a `root` bone plus `WHEEL_LF/RF/LR/RR`. Rims and tyres are skinned to their wheel, everything else to the root. It faces +X, as Chaos expects |
| Physics asset | Root body is a box from the car's `collider.kn5` (or its bounds); wheel bodies are kinematic with collision off, since Chaos raycasts the wheels |
| `BP_<car>_WheelFront/Rear` | Chaos wheel classes: radius and width from `tyres.ini` (or measured from the mesh), steering lock from `car.ini`, driven wheels from the drivetrain |
| `ABP_<car>` | Wheel animation (copied from the Vehicle template) |
| `BP_<car>` | The drivable vehicle: mass, torque curve (`power.lut`), rev limit, gears, final drive and FWD/RWD/AWD from `data/` |
| `BP_<car>_<skin>` | One child Blueprint per extra livery, overriding only the materials that livery changes |

Details:
- **Liveries:** the first `skins/` folder is the default, as in AC. Skin textures replace KN5 textures by file name, case-insensitively. On many cars the paint is a flat `txDetail` colour, and that's handled.
- **Runtime variants:** motion-blur rims (`*_BLUR*`), crash damage (`*DAMAGE*`) and low-res twins (`COCKPIT_LR` when `COCKPIT_HR` exists) are skipped.
- **Physics:** values come from the car's readable `data/` folder. Kunos cars pack theirs in `data.acd`, which this tool doesn't unpack. For those, the physics are estimates (1300 kg, 400 Nm, six gears, RWD), and the wheel size is measured from the mesh. Tune them on `BP_<car>`. Suspension uses Chaos's defaults in every case, because AC's suspension geometry doesn't map onto Chaos's model.
- **Doesn't move:** the steering wheel, doors and other animated parts stay fixed to the body. Only the wheels spin and steer.

## Limitations

- **Encrypted and protected KN5 files are not supported.** This includes Custom Shaders Patch–protected tracks. They are detected and refused, never decrypted. Make sure you have the right to use any track you import: most mods are their authors' work, and Kunos content is covered by the game's license.
- Not imported: AI lines (`fast_lane.ai`), spawn and timing points, CSP extension configs (`ext_config.ini` lights, grass FX, rain), VAO patches, sounds.
- A non-zero `ROTATION=` in `models_*.ini` is reported but not applied. `POSITION=` is applied. This is rare in practice.
- Skinned meshes (such as animated flags) are imported as static meshes in their rest pose.
- Unreal generates lightmap UVs on import according to your project settings. On very large tracks you can turn this off to speed up the import if you use Lumen.

## How it handles coordinates

KN5 vertex data is right-handed with +Y up, in meters, the same convention as glTF. Node transforms are baked into the vertices, and each mesh is re-centred on its own bounds so it gets a sensible pivot. Before placing anything, the Unreal script imports a small calibration shape and measures how your engine version's glTF importer maps axes and units. This means placement doesn't depend on assumptions about importer conventions.

## Tests

```
python tests/test_end_to_end.py
```

This builds a synthetic two-layout track. The track has nested and mirrored transforms, inactive nodes, multilayer, alpha-tested and translucent materials, physics surfaces and a broken texture. The test converts it, then runs the Unreal script against a simulated `unreal` module. It checks placement, collision, physical materials, material parameters and re-run behavior.
