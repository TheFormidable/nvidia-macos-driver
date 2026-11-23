#!/usr/bin/env python3
"""nak_stats_diff.py: compare two runs of the shader compiler's per-shader statistics (NAK_STATS=1).

  nak_stats_diff.py BASE NEW [--gate-instrs PCT] [--gate-spills] [--top N]

BASE and NEW are files of NAK_STATS lines: the JSON lines NVK's compiler writes on stderr when NAK_STATS=1, one per
compiled shader stage (nvk/nvk-nak-stats.patch), or the output of tools/nak-corpus/run.sh, which adds the case name.
Any other line is skipped, so a raw stderr log can be passed as it is. A shader is matched by its case and stage when
the line names a case, otherwise by its stage and the hash of its SPIR-V; a key seen twice gets a running number.

It prints, for each figure, the total over the shaders both runs have, the change, and how many shaders went up and
down, then the shaders with the largest change in instructions, and the shaders only one run has. The figures are the
ones every compiler phase is judged by: instructions (all, main pipe, uniform), registers, warps per SM, spills
(the four spill and fill counts added up) and static cycles.

  --gate-instrs PCT   exit 1 if the total of instructions rises by more than PCT percent
  --gate-spills       exit 1 if any shader spills or fills more than in BASE
Exit 0 otherwise; exit 2 when a file cannot be read or has no NAK_STATS line."""
import argparse
import json
import sys

# what nvk-nak-stats.patch writes after "nak_stats", "stage", "sm" and "blake3", in its order
FIELDS = ("instrs", "instrs_warp", "instrs_uniform", "gprs", "warps_per_sm", "static_cycles",
          "spills_to_mem", "fills_from_mem", "spills_to_reg", "fills_from_reg", "slm")
SPILLS = ("spills_to_mem", "fills_from_mem", "spills_to_reg", "fills_from_reg")
# the rows of the table: a field, or "spills" for the four added up
FIGURES = ("instrs", "instrs_warp", "instrs_uniform", "gprs", "warps_per_sm", "spills", "static_cycles")


def figure(rec, name):
    return sum(rec[k] for k in SPILLS) if name == "spills" else rec[name]


def load(path):
    """-> {key: record} for the NAK_STATS lines of path, in file order."""
    out, seen = {}, {}
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{") or '"nak_stats"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("nak_stats") != 1 or not all(isinstance(rec.get(k), int) for k in FIELDS):
                continue
            base = (rec["case"], rec["stage"]) if "case" in rec else (rec["stage"], rec.get("blake3", ""))
            n = seen.get(base, 0)
            seen[base] = n + 1
            out[base + ((n,) if n else ())] = rec
    return out


def name(key):
    return "/".join(str(k) for k in key)


def compare(base, new, top=10):
    """-> (lines of the report, totals {figure: (base, new)}, keys whose spills rose)."""
    both = [k for k in base if k in new]
    lines, totals = [], {}
    lines.append(f"{len(both)} shaders in both runs, {len(base) - len(both)} only in BASE, "
                 f"{len(new) - len([k for k in new if k in base])} only in NEW")
    lines.append(f"{'figure':<16}{'BASE':>12}{'NEW':>12}{'change':>12}{'%':>9}{'up':>6}{'down':>6}")
    for fig in FIGURES:
        a = sum(figure(base[k], fig) for k in both)
        b = sum(figure(new[k], fig) for k in both)
        up = sum(figure(new[k], fig) > figure(base[k], fig) for k in both)
        down = sum(figure(new[k], fig) < figure(base[k], fig) for k in both)
        pct = f"{100.0 * (b - a) / a:+.2f}" if a else ("0.00" if b == a else "new")
        lines.append(f"{fig:<16}{a:>12}{b:>12}{b - a:>+12}{pct:>9}{up:>6}{down:>6}")
        totals[fig] = (a, b)
    moved = sorted((k for k in both if new[k]["instrs"] != base[k]["instrs"]),
                   key=lambda k: -abs(new[k]["instrs"] - base[k]["instrs"]))
    if moved:
        lines.append(f"largest changes in instructions ({min(top, len(moved))} of {len(moved)}):")
        for k in moved[:top]:
            a, b = base[k], new[k]
            lines.append(f"  {name(k)}: instrs {a['instrs']} -> {b['instrs']}, gprs {a['gprs']} -> {b['gprs']}, "
                         f"warps {a['warps_per_sm']} -> {b['warps_per_sm']}, spills {figure(a, 'spills')} -> "
                         f"{figure(b, 'spills')}, cycles {a['static_cycles']} -> {b['static_cycles']}")
    for label, src, other in (("only in BASE", base, new), ("only in NEW", new, base)):
        only = [k for k in src if k not in other]
        if only:
            lines.append(f"{label}: " + ", ".join(name(k) for k in only[:top]) + (" ..." if len(only) > top else ""))
    spill_rise = [k for k in both if figure(new[k], "spills") > figure(base[k], "spills")]
    return lines, totals, spill_rise


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("base")
    ap.add_argument("new")
    ap.add_argument("--gate-instrs", type=float, metavar="PCT")
    ap.add_argument("--gate-spills", action="store_true")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    try:
        base, new = load(a.base), load(a.new)
    except OSError as e:
        print(f"nak_stats_diff: {e}", file=sys.stderr)
        return 2
    for path, recs in ((a.base, base), (a.new, new)):
        if not recs:
            print(f"nak_stats_diff: no NAK_STATS line in {path}", file=sys.stderr)
            return 2
    lines, totals, spill_rise = compare(base, new, a.top)
    print("\n".join(lines))
    rc = 0
    if a.gate_instrs is not None:
        x, y = totals["instrs"]
        if x and 100.0 * (y - x) / x > a.gate_instrs:
            print(f"GATE: instructions rose {100.0 * (y - x) / x:+.2f} %, more than {a.gate_instrs} %")
            rc = 1
    if a.gate_spills and spill_rise:
        print(f"GATE: {len(spill_rise)} shaders spill more: " + ", ".join(name(k) for k in spill_rise[:a.top]))
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
