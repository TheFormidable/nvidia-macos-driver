#!/usr/bin/env python3
"""nvencvt_header_abi.py - compare the NVIDIA headers libnvencvt is compiled against with those of the kernel module.

libnvencvt (vendor/nvenc) is compiled against open-gpu-kernel-modules 610.57.04 (build/build_nvencvt.sh); the resource
manager it talks to is built from 620.70.06. A struct it passes to RM, or a constant it sends, that differs between the
two changes what goes over the wire without a compile error on either side. This compares, between the two trees, every
struct, union, enum, typedef and define of the headers the library includes that its source names:

  * directly: any identifier in vendor/nvenc/src and vendor/nvenc/vt;
  * through the DRF macros, which paste a name together from their arguments: DRF_DEF(OS32, _ATTR, _LOCATION, _PCI)
    is NVOS32_ATTR_LOCATION and NVOS32_ATTR_LOCATION_PCI;
  * or through one of those: a struct inside a struct it passes, the type of a field, a constant that sizes an array,
    a define whose value is another define.

The video class headers the library also includes (clc9b0.h, nvdec_drv.h and the others, from open-gpu-doc) are in
neither tree and are not compared: the library is built against one pinned revision of them.

A used name that differs or is gone in the second tree is a failure, unless ALLOWED below lists it with the reason it
does not matter. tools/nvk_header_abi.py does the same for NVK and supplies the comparison; this file adds what the
library needs beyond it: structs and enums without a tag, nested braces, plain typedefs, the DRF names, the names
reached through others, and the headers found by following the includes instead of a list.

usage: nvencvt_header_abi.py [--all] [--selftest] [--source <vendor/nvenc>] <reference tree> <tree> [<tree>...]
  <reference tree>  the open-gpu-kernel-modules tree the library is compiled against
  <tree>            a tree the kernel module is built from
  --all             every name in those headers instead of the used ones. Run it once as a control: it must report
                    differences between two versions that really differ, or "nothing used differs" proves nothing
exit: 0 nothing differs outside ALLOWED, 1 something does, 2 the arguments or a tree are not usable"""
import importlib.util, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
# the two folders the library's NVIDIA includes come from (build/build_nvencvt.sh: its -I lines and sparse checkout)
DIRS = ("src/common/sdk/nvidia/inc", "src/nvidia/arch/nvalloc/unix/include")
# name -> why a difference in it does not matter. Keep the reason true: read the code it names before adding one.
ALLOWED = {
    "NVOS33_FLAGS_CACHING_TYPE":
        "620 no longer returns a mapping's caching type in the map flags and defines nothing in those bits (25:23), so "
        "the library reads back the zero it sent. src/rm.c uses the field only to choose, in its own IOConnectMapMemory64 "
        "call, between a write-combined and a default mapping of video memory, so against this RM it takes the default "
        "one unless RM_FORCE_WC is set: slower CPU writes into video memory, the same bytes, and nothing RM receives "
        "changes. The library as shipped already runs that way",
    "NVOS33_FLAGS_CACHING_TYPE_WRITECOMBINED": "a value of NVOS33_FLAGS_CACHING_TYPE, gone with it",
}


def nvk():
    spec = importlib.util.spec_from_file_location("nvk_header_abi", os.path.join(HERE, "nvk_header_abi.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


COMMENT = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)


def strip(txt):
    """comments out, continued lines joined. In one pass, as a compiler reads them: a "/*" inside a // comment or
    inside a string does not start a comment (the library's comments name paths such as src/*.c)"""
    txt = COMMENT.sub(lambda m: " " if m.group(0).startswith("/") else m.group(0), txt)
    return txt.replace("\\\n", " ")


def squeeze(s):
    return " ".join(s.split())


def types(txt):
    """-> {name: {definition}} for every struct, union and enum with a body (by its tag and by each typedef name), every
    plain typedef, and every enum member (as the enum up to and including it: a member's value is its place)"""
    out = {}

    def add(name, what):
        out.setdefault(name, set()).add(what)

    spans = []
    for m in re.finditer(r"\b(struct|union|enum)\b\s*(\w+)?\s*\{", txt):
        if spans and m.start() < spans[-1][1]:
            continue                                    # nested in the one before: part of its body
        depth, i = 1, m.end()
        while i < len(txt) and depth:
            depth += {"{": 1, "}": -1}.get(txt[i], 0)
            i += 1
        if depth:
            continue
        body, end = squeeze(txt[m.end():i - 1]), txt.find(";", i)
        names = [m.group(2)] if m.group(2) else []
        if re.search(r"\btypedef\s+(?:(?:volatile|const)\s+)*$", txt[max(0, m.start() - 40):m.start()]) and end >= 0:
            names += re.findall(r"\b([A-Za-z_]\w*)\b", txt[i:end])           # } NAME, *PNAME;
        spans.append((m.start(), end if end >= 0 else i))
        for n in names:
            add(n, m.group(1) + " { " + body + " }")
        if m.group(1) == "enum":
            seen = []
            for member in body.split(","):
                seen.append(squeeze(member))
                name = re.match(r"\s*([A-Za-z_]\w*)", member)
                if name:
                    add(name.group(1), "enum member: " + ", ".join(seen))
    # plain typedefs: "typedef NvU32 NvHandle;", "typedef NvU64 NvP64 NV_ALIGN_BYTES(8);", "typedef void (*Fn)(int);"
    for m in re.finditer(r"\btypedef\b([^;{}]*);", txt):
        what = squeeze(m.group(1))
        fn = re.search(r"\(\s*\*\s*(\w+)\s*\)", what)
        rest = what
        while re.search(r"\b\w+\s*\([^()]*\)\s*$", rest):                    # an attribute macro after the name
            rest = re.sub(r"\b\w+\s*\([^()]*\)\s*$", "", rest)
        last = re.findall(r"\b([A-Za-z_]\w*)\b", re.sub(r"\[[^\]]*\]", "", rest))
        name = fn.group(1) if fn else last[-1] if len(last) > 1 else None
        if name:
            add(name, "typedef " + what)
    return out


def defines(txt):
    """-> {name: {value}}: object-like and function-like macros, and the status codes, which a macro call declares"""
    out = {}
    for m in re.finditer(r"(?m)^[ \t]*#[ \t]*define[ \t]+(\w+)(\([^)\n]*\))?[ \t]*(.*)$", txt):
        out.setdefault(m.group(1), set()).add(squeeze((m.group(2) or "") + " " + m.group(3)))
    for m in re.finditer(r"\bNV_STATUS_CODE\s*\(\s*(\w+)\s*,\s*(\w+)", txt):
        out.setdefault(m.group(1), set()).add(m.group(2))
    return out


INCLUDE = re.compile(r'(?m)^[ \t]*#[ \t]*include[ \t]*["<]([^">\n]+)[">]')


def reached(tree, first):
    """the headers of the tree that the includes in `first` reach, as a compiler with the library's -I lines finds them:
    {path in the tree: text without comments}"""
    out, todo = {}, [(None, n) for n in first]
    while todo:
        frm, name = todo.pop()
        for c in ([os.path.join(os.path.dirname(frm), name)] if frm else []) + [os.path.join(d, name) for d in DIRS]:
            c = os.path.normpath(c)
            if c in out:
                break
            if os.path.isfile(os.path.join(tree, c)):
                out[c] = strip(open(os.path.join(tree, c), errors="ignore").read())
                todo += [(c, n) for n in INCLUDE.findall(out[c])]
                break
    return out


def parse(texts):
    """-> (types, defines, header count). A name that two headers, or two branches of an #if, define differently keeps
    every definition, so the result does not depend on the order of the files."""
    s, d = {}, {}
    for t in texts:
        for k, v in types(t).items():
            s.setdefault(k, set()).update(v)
        for k, v in defines(t).items():
            d.setdefault(k, set()).update(v)
    return {k: frozenset(v) for k, v in s.items()}, {k: frozenset(v) for k, v in d.items()}, len(texts)


def load(tree, first):
    h = reached(tree, first)
    return parse([h[k] for k in sorted(h)])


WORD = re.compile(r"\b[A-Za-z_]\w*\b")
DRF = re.compile(r"\b(?:DRF|FLD|REF)\w*\s*\(\s*(\w+)\s*,\s*(_\w+)\s*,\s*(_\w+)\s*(?:,\s*(_\w+)\s*[,)])?")


def names_in(text):
    """the identifiers in a piece of C, and the names its DRF-style macros paste together"""
    out = set(WORD.findall(text))
    for dev, grp, field, value in DRF.findall(text):
        out.add("NV" + dev + grp + field)
        if value:
            out.add("NV" + dev + grp + field + value)
    return out


def used_names(code, trees):
    """the names the code uses, closed over what those names are made of in any of the trees"""
    used, todo = set(), names_in(code)
    while todo:
        n = todo.pop()
        if n in used:
            continue
        used.add(n)
        for s, d, _ in trees:
            for body in tuple(s.get(n, ())) + tuple(d.get(n, ())):
                todo |= names_in(body) - used
    return used


def source(folder):
    code = ""
    for sub in ("src", "vt"):
        d = os.path.join(folder, sub)
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if f.endswith((".c", ".h")):
                code += open(os.path.join(d, f), errors="ignore").read() + "\n"
    return strip(code)


def differences(ref, cur, used):
    """-> [(kind, name)], by nvk_header_abi's comparison: type differs / type missing / define differs / define missing"""
    ds, ms, dd, md = nvk().compare(ref[:2], cur[:2], used)
    return ([("type differs", n) for n in ds] + [("type missing", n) for n in ms] +
            [("define differs", n) for n in dd] + [("define missing", n) for n in md])


SAMPLE = """
    // a comment that names src/*.c must not swallow what follows it
    typedef struct { NvU32 x; union { NvU32 a; NvU64 b; } u; INNER in; NvU8 list[LIST_MAX]; Handle h; } OUTER;
    typedef struct _INNER { NvU32 p; } INNER, *PINNER;
    typedef volatile struct CtlRec { NvU32 Put; } Ctl, CtlAlias;
    typedef enum { E_ZERO, E_ONE, E_TWO } E;
    typedef NvU32 Handle;
    typedef NvU64 Wide ALIGNED(8);
    #define LIST_MAX 8
    #define NVOS99_ATTR_PLACE 3:2
    #define NVOS99_ATTR_PLACE_HERE (0x00000001)
    #define ALIAS LIST_MAX
    #define UNUSED_THING 1
    #define LONG_ONE (1 + \\
                      2)
    /* a block comment further down: read carelessly, the one above would run on to the end of this one */
    NV_STATUS_CODE(NV_ERR_SAMPLE, 0x00000011, "sample")
    typedef struct { NvU32 z; } NOT_USED;
"""
CODE = ("OUTER o; x = DRF_DEF(OS99, _ATTR, _PLACE, _HERE); puts(\"/* in a string\"); y = E_TWO; z = ALIAS; puts(\"*/\"); "
        "if (s == NV_ERR_SAMPLE) t = LONG_ONE; ((CtlAlias *)p)->Put = 1; Wide w;")


def selftest():
    fails = 0

    def arm(name, ok):
        nonlocal fails
        print(("ok   " if ok else "FAIL ") + name)
        fails += 0 if ok else 1

    a, code = parse([strip(SAMPLE)]), strip(CODE)

    def changed(old, new, used="code"):
        assert old in SAMPLE or old == new
        b = parse([strip(SAMPLE.replace(old, new))])
        return differences(a, b, used_names(code, [a, b]) if used == "code" else None)

    u = used_names(code, [a])
    arm("two identical trees differ in nothing", changed("", "") == [])
    arm("a struct without a tag and with a nested union is seen whole, under its typedef name",
        "OUTER" in a[0] and any("INNER in" in b and "LIST_MAX" in b for b in a[0]["OUTER"]) and "u" not in a[0])
    arm("a comment that holds the start of another comment does not swallow the code after it", "OUTER" in a[0] and "LIST_MAX" in a[1])
    arm("the start of a comment inside a string is not one: the code between two such strings is read", {"E_TWO", "ALIAS"} <= u)
    arm("the names a DRF macro pastes together count as used", {"NVOS99_ATTR_PLACE", "NVOS99_ATTR_PLACE_HERE"} <= u)
    arm("so do a struct inside a used struct, the type of its field, the constant that sizes its array, a define behind a define",
        {"INNER", "Handle", "LIST_MAX", "ALIAS"} <= u)
    arm("a name the code never reaches is not used", "UNUSED_THING" not in u and "NOT_USED" not in u)
    arm("a field added inside the nested union of an untagged struct is reported",
        ("type differs", "OUTER") in changed("NvU64 b; } u;", "NvU64 b; NvU32 c; } u;"))
    arm("a change in a struct that a used struct holds is reported", ("type differs", "INNER") in changed("{ NvU32 p; }", "{ NvU64 p; }"))
    arm("a change in the plain typedef of a field's type is reported", ("type differs", "Handle") in changed("typedef NvU32 Handle;", "typedef NvU64 Handle;"))
    arm("a typedef with an attribute after its name is read under that name", ("type differs", "Wide") in changed("ALIGNED(8)", "ALIGNED(4)"))
    arm("a struct declared with a qualifier is seen under each of its typedef names",
        ("type differs", "CtlAlias") in changed("{ NvU32 Put; }", "{ NvU32 Ignored; NvU32 Put; }"))
    arm("a changed array size is reported through its constant", ("define differs", "LIST_MAX") in changed("#define LIST_MAX 8", "#define LIST_MAX 16"))
    arm("a field that moved is reported", ("define differs", "NVOS99_ATTR_PLACE") in changed("PLACE 3:2", "PLACE 5:4"))
    arm("a value that is gone is reported", ("define missing", "NVOS99_ATTR_PLACE_HERE") in changed("#define NVOS99_ATTR_PLACE_HERE (0x00000001)", ""))
    arm("an enum member that moved because one was put before it is reported", ("type differs", "E_TWO") in changed("E_ZERO, E_ONE,", "E_ZERO, E_NEW, E_ONE,"))
    arm("an enum member added after the ones in use is not", not [x for x in changed("E_TWO }", "E_TWO, E_THREE }") if x[1] in ("E_TWO", "E_ONE")])
    arm("a changed status code is reported", ("define differs", "NV_ERR_SAMPLE") in changed("0x00000011", "0x00000012"))
    arm("a define that continues on the next line is read whole", ("define differs", "LONG_ONE") in changed("2)", "3)"))
    arm("control: a change in names the code does not use is not reported",
        changed("#define UNUSED_THING 1", "#define UNUSED_THING 2") == [] and changed("{ NvU32 z; }", "{ NvU64 z; }") == [])
    arm("control: with every name compared those changes are reported",
        ("define differs", "UNUSED_THING") in changed("#define UNUSED_THING 1", "#define UNUSED_THING 2", None) and
        ("type differs", "NOT_USED") in changed("{ NvU32 z; }", "{ NvU64 z; }", None))
    return 1 if fails else 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    everything = "--all" in argv
    src = os.path.join(os.path.dirname(HERE), "vendor", "nvenc")
    args = [a for a in argv if a != "--all"]
    if "--source" in args:
        i = args.index("--source")
        if i + 1 >= len(args):
            print(__doc__)
            return 2
        src = args[i + 1]
        del args[i:i + 2]
    if len(args) < 2:
        print(__doc__)
        return 2
    code = source(src)
    first = INCLUDE.findall(code)
    if not first:
        print("STOP: no library source in %s" % src)
        return 2
    loaded = []
    for t in args:
        loaded.append(load(t, first))
        if not loaded[-1][2]:
            print("STOP: %s holds none of the headers the library includes (looked in %s)" % (t, ", ".join(DIRS)))
            return 2
    ref, bad = loaded[0], 0
    for t, cur in zip(args[1:], loaded[1:]):
        used = None if everything else used_names(code, [ref, cur])
        counts = sum(1 for n in ref[0] if used is None or n in used), sum(1 for n in ref[1] if used is None or n in used)
        diffs = sorted(differences(ref, cur, used))
        stop = [x for x in diffs if everything or x[1] not in ALLOWED]
        print("reference %s (%d headers reached from the library's includes), tree %s (%d headers): %s"
              % (args[0], ref[2], t, cur[2], "every name" if everything else "the names libnvencvt uses"))
        print("   compared: %d structs, unions, enum members and typedefs, %d defines" % counts)
        for kind, n in diffs[:200]:
            if n in ALLOWED and not everything:
                print("   allowed: %s: %s (%s)" % (kind, n, ALLOWED[n]))
                continue
            print("   %s: %s" % (kind, n))
            for lab, side in (("reference", ref), ("tree", cur)):
                for body in sorted(side[0].get(n, ())) + sorted(side[1].get(n, ())):
                    print("      %s: %s" % (lab, body[:300]))
        if not everything:
            for n in ALLOWED:
                if n not in [x[1] for x in diffs]:
                    print("   note: %s is allowed to differ and does not differ between these trees" % n)
        print("== %d differ, %d of them allowed" % (len(diffs), len(diffs) - len(stop)))
        bad += len(stop)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
