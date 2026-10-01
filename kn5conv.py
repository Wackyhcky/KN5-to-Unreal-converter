#!/usr/bin/env python3
"""Convert an Assetto Corsa track or car (folder or .kn5) into an Unreal-ready package.

Car folders (content/cars/<name>) are detected automatically; --car forces it.

Examples:
  python kn5conv.py "C:/Steam/.../assettocorsa/content/tracks/mytrack" out/mytrack
  python kn5conv.py mytrack.kn5 out/mytrack
  python kn5conv.py <track> out --ac-root "C:/Steam/steamapps/common/assettocorsa"
  python kn5conv.py <track> out --layouts gp,national

Then, in Unreal (Output Log, Cmd set to "Cmd"):
  py "C:/path/to/unreal/ue_import_track.py" "C:/path/to/out/mytrack/manifest.json"
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ac2ue.convert import convert  # noqa: E402
from ac2ue.car import convert_car, is_car_folder  # noqa: E402
from ac2ue.kn5_reader import Kn5Error  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description="Assetto Corsa KN5 track or car -> Unreal Engine package")
    p.add_argument("input", help="AC track folder (content/tracks/<name>), car folder "
                                 "(content/cars/<name>) or a single .kn5 file")
    p.add_argument("output", help="output folder (created if missing)")
    p.add_argument("--ac-root", help="Assetto Corsa install folder, used for system surfaces.ini")
    p.add_argument("--layouts", help="comma-separated layout names to convert (default: all)")
    p.add_argument("--include-inactive", action="store_true",
                   help="also export meshes under inactive nodes (normally hidden in AC)")
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 4,
                   help="parallel texture conversion threads")
    p.add_argument("--car", action="store_true", help="treat the input as a car (auto-detected)")
    p.add_argument("--track", action="store_true", help="treat the input as a track")
    p.add_argument("--skins", choices=["all", "default"], default="all",
                   help="cars: import every livery or only the default one")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    layouts = [l.strip() for l in args.layouts.split(",")] if args.layouts else None
    try:
        if args.car or (not args.track and is_car_folder(args.input)):
            convert_car(args.input, args.output, args.skins, args.jobs, args.verbose)
        else:
            convert(args.input, args.output, args.ac_root, layouts,
                    args.include_inactive, args.jobs, args.verbose)
    except (Kn5Error, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
