"""Assetto Corsa track folder handling: layouts (models*.ini) and surfaces.ini."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

# Approximate stand-ins for AC's system/data/surfaces.ini, used only when a key
# is not defined by the track itself (or by --ac-root). Pass --ac-root to use
# the real values from your Assetto Corsa install instead.
BUILTIN_SURFACES = {
    "ROAD": {"FRICTION": 0.99, "DAMPING": 0.0, "IS_VALID_TRACK": 1, "DIRT_ADDITIVE": 0.0},
    "KERB": {"FRICTION": 0.93, "DAMPING": 0.0, "IS_VALID_TRACK": 1, "DIRT_ADDITIVE": 0.0},
    "GRASS": {"FRICTION": 0.60, "DAMPING": 0.0, "IS_VALID_TRACK": 0, "DIRT_ADDITIVE": 1.0},
    "SAND": {"FRICTION": 0.60, "DAMPING": 0.10, "IS_VALID_TRACK": 0, "DIRT_ADDITIVE": 1.0},
    "WALL": {"FRICTION": 0.40, "DAMPING": 0.0, "IS_VALID_TRACK": 0, "DIRT_ADDITIVE": 0.0},
}

_LAYOUT_SKIP_DIRS = {"data", "ui", "ai", "skins", "extension", "texture", "textures", "sfx", "cache"}


def parse_ini(path: str) -> dict:
    """Tolerant AC-style INI parser. Returns {SECTION: {KEY: raw_value}}."""
    sections: dict = {}
    current = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.split(";", 1)[0]
            if "//" in line:
                line = line.split("//", 1)[0]
            line = line.strip()
            if not line:
                continue
            if line.startswith("[") and "]" in line:
                current = line[1:line.index("]")].strip().upper()
                sections.setdefault(current, {})
                continue
            if current is None or "=" not in line:
                continue
            key, value = line.split("=", 1)
            sections[current][key.strip().upper()] = value.strip()
    return sections


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _vec3(v) -> list:
    parts = [p for p in re.split(r"[,\s]+", v or "") if p]
    out = [_num(p) for p in parts[:3]]
    return out + [0.0] * (3 - len(out))


def read_surfaces(path: str) -> dict:
    """Read a surfaces.ini into {KEY: {PROP: value}}."""
    result = {}
    if not path or not os.path.isfile(path):
        return result
    for sec_name, sec in parse_ini(path).items():
        if not sec_name.startswith("SURFACE_"):
            continue
        key = sec.get("KEY", "").strip().upper()
        if not key:
            continue
        props = {}
        for k, v in sec.items():
            if k == "KEY":
                continue
            try:
                props[k] = float(v)
            except ValueError:
                props[k] = v
        result[key] = props
    return result


def match_surface(mesh_name: str, keys) -> Optional[str]:
    """AC physics surfaces are meshes named <digits><KEY>..., e.g. 1ROAD, 2KERB_01.

    Returns the longest surface key that the name (after its leading digits)
    starts with, or None.
    """
    m = re.match(r"^(\d+)(.+)$", mesh_name)
    if not m:
        return None
    rest = m.group(2).upper()
    best = None
    for key in keys:
        if rest.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return best


@dataclass
class ModelRef:
    kn5_path: str
    position: list = field(default_factory=lambda: [0.0, 0.0, 0.0])  # AC space, meters
    rotation: list = field(default_factory=lambda: [0.0, 0.0, 0.0])


@dataclass
class Layout:
    name: str
    models: list
    surfaces_path: Optional[str]
    surfaces: dict = field(default_factory=dict)


@dataclass
class Track:
    name: str
    root: str
    layouts: list


def _models_from_ini(track_root: str, ini_path: str) -> list:
    models = []
    sections = parse_ini(ini_path)
    for sec_name in sorted(sections, key=lambda s: (len(s), s)):
        if not sec_name.startswith("MODEL"):
            continue
        sec = sections[sec_name]
        file = sec.get("FILE")
        if not file:
            continue
        kn5 = os.path.normpath(os.path.join(track_root, file.replace("\\", os.sep)))
        models.append(ModelRef(kn5, _vec3(sec.get("POSITION")), _vec3(sec.get("ROTATION"))))
    return models


def _find_data_dir(track_root: str, layout_dir: Optional[str]) -> Optional[str]:
    if layout_dir:
        p = os.path.join(track_root, layout_dir, "data")
        if os.path.isdir(p):
            return p
    p = os.path.join(track_root, "data")
    return p if os.path.isdir(p) else None


def load_track(path: str, system_surfaces: Optional[dict] = None,
               only_layouts: Optional[list] = None) -> Track:
    """Discover layouts and surfaces for a track folder or a single .kn5 file."""
    path = os.path.abspath(path)
    system_surfaces = system_surfaces or {}

    if os.path.isfile(path):
        if not path.lower().endswith(".kn5"):
            raise ValueError(f"{path} is not a .kn5 file or a track folder")
        track_root = os.path.dirname(path)
        name = os.path.splitext(os.path.basename(path))[0]
        layouts = [Layout("default", [ModelRef(path)], None)]
        data_dir = _find_data_dir(track_root, None)
        if data_dir:
            layouts[0].surfaces_path = os.path.join(data_dir, "surfaces.ini")
    else:
        track_root = path
        name = os.path.basename(os.path.normpath(path))
        entries = os.listdir(track_root)
        layouts = []
        for entry in sorted(entries):
            low = entry.lower()
            full = os.path.join(track_root, entry)
            if not os.path.isfile(full) or not low.endswith(".ini"):
                continue
            if low == "models.ini":
                data = _find_data_dir(track_root, None)
                layouts.append(Layout("default", _models_from_ini(track_root, full),
                                      os.path.join(data, "surfaces.ini") if data else None))
            elif low.startswith("models_"):
                lname = entry[len("models_"):-len(".ini")]
                data = _find_data_dir(track_root, lname)
                layouts.append(Layout(lname, _models_from_ini(track_root, full),
                                      os.path.join(data, "surfaces.ini") if data else None))

        if not layouts:
            # No models ini: AC loads <trackname>.kn5 for every layout.
            main = os.path.join(track_root, name + ".kn5")
            if os.path.isfile(main):
                kn5s = [main]
            else:
                kn5s = sorted(os.path.join(track_root, e) for e in entries
                              if e.lower().endswith(".kn5"))
            if not kn5s:
                raise ValueError(f"No .kn5 files or models*.ini found in {track_root}")
            layout_dirs = sorted(
                e for e in entries
                if os.path.isdir(os.path.join(track_root, e)) and e.lower() not in _LAYOUT_SKIP_DIRS
                and os.path.isdir(os.path.join(track_root, e, "data")))
            if os.path.isdir(os.path.join(track_root, "data")) or not layout_dirs:
                data = _find_data_dir(track_root, None)
                layouts.append(Layout("default", [ModelRef(k) for k in kn5s],
                                      os.path.join(data, "surfaces.ini") if data else None))
            for d in layout_dirs:
                data = _find_data_dir(track_root, d)
                layouts.append(Layout(d, [ModelRef(k) for k in kn5s],
                                      os.path.join(data, "surfaces.ini") if data else None))

    if only_layouts:
        wanted = {l.lower() for l in only_layouts}
        layouts = [l for l in layouts if l.name.lower() in wanted]
        if not layouts:
            raise ValueError(f"None of the requested layouts were found: {only_layouts}")

    for layout in layouts:
        merged = {k: dict(v, _SOURCE="builtin-default") for k, v in BUILTIN_SURFACES.items()}
        for k, v in system_surfaces.items():
            merged[k] = dict(v, _SOURCE="ac-system")
        for k, v in read_surfaces(layout.surfaces_path).items():
            merged[k] = dict(v, _SOURCE="track")
        layout.surfaces = merged
        layout.models = [m for m in layout.models if m.kn5_path]

    return Track(name=name, root=track_root, layouts=layouts)
