"""One-off: scan Luna animations/**/*.vrma for static / near-static body motion."""
from __future__ import annotations

import json
import math
import os
import struct
import sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Luna animations")


def read_glb(path: str):
    with open(path, "rb") as f:
        if f.read(4) != b"glTF":
            return None, None
        f.read(8)
        jl, jt = struct.unpack("<I4s", f.read(8))
        if jt != b"JSON":
            return None, None
        jb = f.read(jl)
        rest = f.read(8)
        if len(rest) < 8:
            return json.loads(jb.decode("utf-8")), b""
        bl, bt = struct.unpack("<I4s", rest)
        bb = f.read(bl) if bt == b"BIN\x00" else b""
    return json.loads(jb.decode("utf-8")), bb


def get_rot_stats(j: dict, buf: bytes) -> dict | None:
    anims = j.get("animations") or []
    if not anims:
        return None
    a0 = anims[0]
    chans = a0.get("channels") or []
    samps = a0.get("samplers") or []
    acc = j.get("accessors") or []
    bvs = j.get("bufferViews") or []

    def get_vals(ai):
        if ai is None or ai >= len(acc):
            return []
        a = acc[ai]
        bv_i = a.get("bufferView")
        if bv_i is None:
            return []
        bv = bvs[bv_i]
        off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        cnt = a["count"]
        typ = a["type"]
        el = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[typ]
        strd = el * 4
        out = []
        for i in range(cnt):
            o = off + i * strd
            t = struct.unpack_from("<" + "f" * el, buf, o)
            out.append(t)
        return out

    max_d = 0.0
    rot_n = 0
    all_const = True
    for ch in chans:
        if (ch.get("target") or {}).get("path") != "rotation":
            continue
        si = ch.get("sampler")
        if si is None or si >= len(samps):
            continue
        samp = samps[si]
        rots = get_vals(samp.get("output"))
        if len(rots) < 1:
            continue
        rot_n += 1
        q0 = rots[0]
        for q in rots[1:]:
            if any(abs(a - b) > 1e-7 for a, b in zip(q0, q)):
                all_const = False
                break
        for i in range(1, len(rots)):
            qa, qb = rots[i - 1], rots[i]
            dot = abs(sum(a * b for a, b in zip(qa, qb)))
            dot = min(1, max(-1, dot))
            max_d = max(max_d, 2 * math.acos(dot))
    if rot_n == 0:
        return None
    return {"rot_ch": rot_n, "static": all_const, "max_delta": max_d}


def main() -> int:
    base = os.path.abspath(ROOT)
    if not os.path.isdir(base):
        print("No Luna animations folder", base)
        return 1
    rows = []
    for walk_root, _dirs, files in os.walk(base):
        for fn in sorted(files, key=str.lower):
            if not fn.lower().endswith(".vrma"):
                continue
            p = os.path.join(walk_root, fn)
            rel = os.path.relpath(p, base)
            j, buf = read_glb(p)
            if not j or not buf:
                rows.append((rel, None, "READ_FAIL"))
                continue
            st = get_rot_stats(j, buf)
            if not st:
                rows.append((rel, None, "NO_ROT_ANIM"))
                continue
            flag = "ok"
            if st["static"]:
                flag = "STATIC"
            elif st["max_delta"] < 0.05:
                flag = "NEAR_STATIC"
            rows.append((rel, st, flag))

    static_files = [r[0] for r in rows if r[2] == "STATIC"]
    near = [r[0] for r in rows if r[2] == "NEAR_STATIC"]

    for rel, st, flag in rows:
        if st:
            print(f"{rel}: rot={st['rot_ch']} max_delta_rad={st['max_delta']:.4f} [{flag}]")
        else:
            print(f"{rel}: [{flag}]")

    print()
    print("STATIC (no bone motion over time — will look frozen / wrong if bind pose is T):")
    for s in static_files:
        print(" ", s)
    print("NEAR_STATIC (almost no motion):")
    for s in near:
        print(" ", s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
