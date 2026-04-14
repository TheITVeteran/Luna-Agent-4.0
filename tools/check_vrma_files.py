"""Validate all .vrma under Luna animations: GLB readable, has animations, body-pool filter."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import luna_vrm as lv  # noqa: E402


def main() -> int:
    root = lv._luna_animations_root()
    all_files: list[str] = []
    if os.path.isdir(root):
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if fn.lower().endswith(".vrma"):
                    all_files.append(os.path.join(dirpath, fn))
    all_files = sorted(set(os.path.abspath(p) for p in all_files))

    seq = set(lv.idle_sequence_folder_paths_ordered())
    expr = set(lv.expression_vrma_paths())
    talk = set(lv.talk_vrma_paths())
    idle = set(lv.idle_vrma_paths())

    print("=== VRMA validation (Luna animations tree) ===\n")
    issues: list[str] = []
    for p in all_files:
        rel = os.path.relpath(p, lv._project_base())
        pools: list[str] = []
        if p in seq:
            pools.append("idle_sequence")
        if p in expr:
            pools.append("expression")
        if p in talk:
            pools.append("talk")
        if p in idle:
            pools.append("idle_body")

        j, buf = lv._read_glb_json_and_bin(p)
        ok_glb = bool(j and buf)
        anims = (j or {}).get("animations") or []
        stats = lv._vrma_motion_stats(p) if ok_glb else None
        passes_body = lv._vrma_passes_body_motion_minimum(p)

        pool_str = ", ".join(pools) if pools else "(not in standard API pools)"
        parts = [rel, f"pools: {pool_str}"]

        if not ok_glb:
            parts.append("FAIL: not a readable GLB (missing glTF/BIN or parse error)")
            issues.append(rel)
        elif len(anims) == 0:
            parts.append("FAIL: animations[] empty")
            issues.append(rel)
        else:
            if stats:
                parts.append(
                    f"rot_ch={stats['rotation_channels']} static_pose={stats['static_pose']} "
                    f"max_delta_rad={stats['max_rotation_delta_rad']}"
                )
            else:
                parts.append("(no rotation channel stats — morph-only or no sampled rotations)")
            if ("idle_body" in pools or "talk" in pools) and not passes_body:
                parts.append("WARN: excluded from idle/talk VRMA list by LUNA_VRMA_MIN_MOTION_RAD")

        print(" | ".join(parts))

    print()
    if issues:
        print(f"FAILED: {len(issues)} file(s)")
        for r in issues:
            print(f"  - {r}")
        return 1
    print("All .vrma files: readable GLB with at least one animation entry.")
    print()
    print(f"idle_sequence: {len(seq)}  expression: {len(expr)}  talk: {len(talk)}  idle_body: {len(idle)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
