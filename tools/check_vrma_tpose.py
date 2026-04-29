"""Scan Luna .vrma files with luna_vrm._vrma_motion_stats (static / low motion / few rotation channels)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luna_vrm import (  # noqa: E402
    _fbx_animations_dir,
    _list_vrma_files_in_dir,
    _min_body_motion_radians,
    _vrma_motion_stats,
    expression_vrma_paths,
    idle_vrma_paths,
    talk_vrma_paths,
)


def collect() -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for label, fn in [
        ("expressions", expression_vrma_paths),
        ("talking", talk_vrma_paths),
        ("idle_root", idle_vrma_paths),
    ]:
        for p in fn():
            k = os.path.normcase(os.path.abspath(p))
            if k in seen:
                continue
            seen.add(k)
            out.append((label, p))
    d = _fbx_animations_dir()
    for p in _list_vrma_files_in_dir(d):
        k = os.path.normcase(os.path.abspath(p))
        if k in seen:
            continue
        seen.add(k)
        out.append(("fbx_animations", p))
    out.sort(key=lambda x: (x[0], x[1].lower()))
    return out


def main() -> None:
    min_r = _min_body_motion_radians()
    print("LUNA_VRMA_MIN_MOTION_RAD =", min_r)
    print("Note: expression VRMAs are often low body rotation (face/morph) - few tracks can be expected.")
    print()

    for pool, path in collect():
        name = os.path.basename(path)
        st = _vrma_motion_stats(path)
        if st is None:
            print(f"[{pool}] {name}: (no animation stats in file)")
            print()
            continue
        ch = st["rotation_channels"]
        static = st["static_pose"]
        peak = st["max_rotation_delta_rad"]
        flags: list[str] = []
        if static:
            flags.append("static_pose (all rotation keys identical = frozen / T-pose export)")
        if ch < 14:
            flags.append(f"few bone rotation samplers={ch} (vrm_viewer warns <14: possible retarget T-pose)")
        if 0 < peak < 0.01 and not static:
            flags.append("near_zero_peak_motion (between keyframes)")
        if min_r > 0 and peak < min_r and pool != "expressions":
            flags.append(f"below body threshold {min_r} rad (omitted from idle/talk list by luna_vrm)")

        print(f"[{pool}] {name}")
        print(
            f"         rot_channels={ch}  max_keyframe_delta_rad={peak!r}  static_pose={static}"
        )
        if flags:
            for f in flags:
                print(f"         ! {f}")
        else:
            print("         -> OK (no T-pose / static heuristics triggered)")
        print()


if __name__ == "__main__":
    main()
