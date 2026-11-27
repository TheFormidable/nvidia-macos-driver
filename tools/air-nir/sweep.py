#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# usage: tools/air-nir/sweep.py AIRSWEEP NVK.dylib LLVM.dylib OUT.tsv FILE|@LIST...
# (a LIST line may be "path<TAB>entry<TAB>index=hex,...": one kernel with function-constant values, airsweep.c)
#
# Runs airsweep (airsweep.c) over the files, restarting it after the file it was in when it crashed (that file's
# kernels are recorded as CRASH), writes every kernel's line to OUT.tsv and prints the counts: kernels translated and
# compiled, refused (grouped by reason, with numbers and names folded), pipelines NVK would not create, crashes.
import os, re, subprocess, sys, tempfile, collections

def main():
    exe, nvk, llvm, out = sys.argv[1:5]
    files = []
    for a in sys.argv[5:]:   # @LIST: the paths in LIST, one per line
        files += [l.rstrip("\n") for l in open(a[1:]) if l.strip()] if a.startswith("@") else [a]
    lines = []
    env = dict(os.environ, MESA_SHADER_CACHE_DISABLE="true")
    while files:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as t:
            t.write("\n".join(files) + "\n")
            lst = t.name
        p = subprocess.Popen([exe, nvk, llvm, lst], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        current, done = None, set()
        for line in p.stdout:
            f = line.rstrip("\n").split("\t")
            if f[0] == "BEGIN":
                if current:
                    done.add(current)
                current = f[1]
                continue
            lines.append(f)
        rc = p.wait()
        os.unlink(lst)
        if current:
            done.add(current)
        if rc == 0:
            break
        if rc < 0 and current:
            lines.append(["CRASH", current, "-", "signal %d" % -rc])
            at = next(i for i, f in enumerate(files) if f.split("\t")[0] == current)
            files = files[at + 1:]
            continue
        sys.exit("airsweep stopped with status %d" % rc)
    with open(out, "w") as o:
        for f in lines:
            o.write("\t".join(f) + "\n")
    kinds = collections.Counter(f[0] for f in lines)
    print("kernels: " + ", ".join("%s %d" % kv for kv in sorted(kinds.items())))
    why = collections.Counter()
    for f in lines:
        if f[0].startswith("REFUSED") or f[0].startswith("PIPELINE") or f[0] in ("CRASH", "NOTEXT"):
            m = f[3]
            m = re.sub(r"\(.*?\)", "(..)", m)
            m = re.sub(r"\b(air|llvm)\.([a-z_0-9]+(?:\.[a-z_0-9]+)*?)(\.(?:[usf]\.)?v?\d*[if]\d+)+\b", r"\1.\2", m)
            m = re.sub(r"\b\d+\b", "N", m)
            why[f[0] + " " + m] += 1
    for m, n in why.most_common():
        print("%6d  %s" % (n, m))

main()
