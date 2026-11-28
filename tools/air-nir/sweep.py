#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# usage: tools/air-nir/sweep.py AIRSWEEP NVK.dylib LLVM.dylib OUT.tsv FILE|@LIST...
#        tools/air-nir/sweep.py --reflect AIRSWEEP NVK.dylib LLVM.dylib TRANSLATOR.dylib OUT.tsv FILE|@LIST...
# (a LIST line may be "path<TAB>entry<TAB>index=hex,...": one kernel with function-constant values, airsweep.c)
#
# Runs airsweep (airsweep.c) over the files, restarting it after the file it was in when it crashed (that file's
# kernels are recorded as CRASH), writes every kernel's line to OUT.tsv and prints the counts: kernels translated and
# compiled, refused (grouped by reason, with numbers and names folded), pipelines NVK would not create, crashes.
#
# --reflect (round 5): every kernel, vertex and fragment function, the frontend's reflection against the translator's
# (nvmtl_reflect): for each function the frontend translates, every key the frontend gives must equal the
# translator's (compare() below: the bindings matched as a set, the translator's BufferAddressTable left out, which no
# reader of the plugin's takes); prints equal, different by key, not reflected by the frontend (why) and refused.
import json, os, re, subprocess, sys, tempfile, collections

def bkey(b):
    d = b.get("descriptor") or {}
    return (b.get("kind") or "", b.get("metal_index") if b.get("metal_index") is not None else -1,
            b.get("param_index") if b.get("param_index") is not None else -1, d.get("binding", -1) if d else -1)

def same_num(a, b):
    return a == b or (isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool)
                      and not isinstance(b, bool) and abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b)))

# every key path where the frontend's value differs from the translator's; the frontend's keys only
def compare(fe, tr, path, out):
    if isinstance(fe, dict):
        if not isinstance(tr, dict):
            out.append(path or "(top)")
            return
        for k, v in fe.items():
            if k == "reflection_source" or k.startswith("air_nir_"):   # the frontend's own notes
                continue
            compare(v, tr.get(k), path + "." + k if path else k, out)
        return
    if isinstance(fe, list):
        if not isinstance(tr, list):
            out.append(path)
            return
        if path == "bindings":
            tr = [b for b in tr if b.get("kind") != "BufferAddressTable"]
            fe, tr = sorted(fe, key=bkey), sorted(tr, key=bkey)
        if len(fe) != len(tr):
            out.append(path + "#count")
            if path == "bindings":
                fk, tk = collections.Counter(b.get("kind") for b in fe), collections.Counter(b.get("kind") for b in tr)
                for k in sorted(set(fk) | set(tk)):
                    if fk[k] != tk[k]:
                        out.append("bindings#count[%s]" % k)
            return
        for a, b in zip(fe, tr):
            compare(a, b, path + "[]", out)
        return
    if not same_num(fe, tr):
        out.append(path)

def reflect_main(args):
    exe, nvk, llvm, xlate, out = args[:5]
    files = []
    for a in args[5:]:
        files += [l.rstrip("\n") for l in open(a[1:]) if l.strip()] if a.startswith("@") else [a]
    env = dict(os.environ, MESA_SHADER_CACHE_DISABLE="true")
    counts = collections.Counter(); keys = collections.Counter(); why = collections.Counter(); refused = collections.Counter()
    by_stage = collections.Counter(); descs = collections.Counter()
    o = open(out, "w")
    while files:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as t:
            t.write("\n".join(files) + "\n")
            lst = t.name
        p = subprocess.Popen([exe, "--reflect", nvk, llvm, xlate, lst], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, env=env, errors="replace")
        current = None
        for line in p.stdout:
            f = line.rstrip("\n").split("\t")
            if f[0] == "BEGIN":
                current = f[1]
                continue
            if f[0] == "NOTEXT":
                counts["file does not parse"] += 1
                continue
            if f[0] == "DSC" and len(f) >= 7:
                # round 9: the buffers the translator keeps as descriptors (airsweep.c), asked for or (AIRSWEEP_KEPT_ALL) not
                wanted, kept, st2 = f[4], f[5], f[6]
                if wanted != "0":
                    descs["asked for descriptor buffers"] += 1
                    descs["... then " + ("translated" if st2 == "0" else "refused")] += 1
                    if kept.startswith("ERR:"):
                        descs["... the translator's module not read"] += 1
                    elif kept == "0":
                        descs["... the translator keeps none of them"] += 1
                elif kept not in ("0", "0x0") and not kept.startswith("ERR:"):
                    descs["KEPT WITHOUT ASKING (the road would read a kept buffer through its address)"] += 1
                    o.write("UNASKED\t%s\t%s\t%s\tkept %s\n" % (f[1], f[2], f[3], kept))
                continue
            if f[0] == "KSELF" and len(f) >= 7:
                # round 12: kept buffers the frontend decided itself, against the translator's module
                descs["decided by the frontend: " + ("same as the translator" if f[6] == "SAME" else f[6][:40])] += 1
                if f[6] != "SAME":
                    o.write("KSELF\t%s\t%s\t%s\tfrontend %s translator %s %s\n" % (f[1], f[2], f[3], f[4], f[5], f[6]))
                continue
            if f[0] != "RFL" or len(f) < 8:
                continue
            st, path, name, stage, msg, fej, trj = f[1], f[2], f[3], f[4], f[5], f[6], f[7]
            if st != "0":
                counts["refused"] += 1
                refused[re.sub(r"\b\d+\b", "N", re.sub(r"\(.*?\)", "(..)", msg))[:110]] += 1
                o.write("REFUSED\t%s\t%s\t%s\t%s\n" % (path, name, stage, msg))
                continue
            if fej == "-":
                counts["not reflected by the frontend"] += 1
                why[msg] += 1
                o.write("NOREFL\t%s\t%s\t%s\t%s\n" % (path, name, stage, msg))
                continue
            if trj.startswith("ERR:"):
                counts["translator reflection failed"] += 1
                o.write("TRERR\t%s\t%s\t%s\t%s\n" % (path, name, stage, trj[4:]))
                continue
            try:
                fe, tr = json.loads(fej), json.loads(trj)
            except ValueError as e:
                counts["bad JSON"] += 1
                o.write("BADJSON\t%s\t%s\t%s\t%s\n" % (path, name, stage, e))
                continue
            diff = []
            compare(fe, tr, "", diff)
            if diff:
                counts["different"] += 1
                by_stage["different " + stage] += 1
                for k in sorted(set(diff)):
                    keys[k] += 1
                o.write("DIFF\t%s\t%s\t%s\t%s\n" % (path, name, stage, " ".join(sorted(set(diff)))))
            else:
                counts["equal"] += 1
                by_stage["equal " + stage] += 1
                o.write("EQUAL\t%s\t%s\t%s\n" % (path, name, stage))
        rc = p.wait()
        os.unlink(lst)
        if rc == 0:
            break
        if rc < 0 and current:
            counts["crash"] += 1
            o.write("CRASH\t%s\t-\t-\tsignal %d\n" % (current, -rc))
            at = next(i for i, f in enumerate(files) if f.split("\t")[0] == current)
            files = files[at + 1:]
            continue
        sys.exit("airsweep stopped with status %d" % rc)
    o.close()
    print("functions: " + ", ".join("%s %d" % kv for kv in sorted(counts.items())))
    print("by stage: " + ", ".join("%s %d" % kv for kv in sorted(by_stage.items())))
    print("different, by key (functions):")
    for k, n in keys.most_common(20):
        print("%6d  %s" % (n, k))
    print("not reflected by the frontend:")
    for k, n in why.most_common(10):
        print("%6d  %s" % (n, k))
    print("refused by the frontend (top 15):")
    for k, n in refused.most_common(15):
        print("%6d  %s" % (n, k))
    print("descriptor buffers: " + (", ".join("%s %d" % kv for kv in sorted(descs.items())) or "none asked for"))

def main():
    if sys.argv[1] == "--reflect":
        return reflect_main(sys.argv[2:])
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
