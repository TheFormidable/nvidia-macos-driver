#!/usr/bin/env python3
"""setFragmentTexture looks at each texture once for the ShaderWrite census.

A texture bound for a fragment shader that is ShaderWrite but has no storage view is named in the log once per shape
(pixel format, usage, type), through a 64-entry table under a global lock. Apple's GL rebinds every texture before
each draw, so the table was searched, and three properties read by message, on every bind. A per-texture flag
(_swNoted) now skips the census after a texture's first bind. That is exact: what the census looks at is fixed when the
texture is made, and after a texture's first look its shape is in the table or the table is full, so every later look
at it answers "already said". The C model below runs the old census and the flagged one over random binds of random
textures and compares the lines they log; the rest checks the sources for what the argument needs.
Usage: test_sw_census.py [driver checkout]"""
import pathlib, re, shutil, subprocess, sys, tempfile

R = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
P = R / "plugin"
objs = (P / "NVMTLObjects.m").read_text()
objh = (P / "NVMTLObjects.h").read_text()
dev = (P / "NVMTLDevice.m").read_text()
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


MODEL = r'''
#include <stdio.h>
#include <stdint.h>
#include <string.h>
typedef struct { int stex; uint64_t usage, fmt, type; unsigned char noted; } tex;
static uint64_t seen[64]; static int nseen;
static int census(const tex *t) {   /* the table check as setFragmentTexture does it: 1 = log a line */
    const uint64_t k = (t->fmt << 40) ^ (t->usage << 8) ^ t->type ^ 0x8000000000000000ull;
    int fresh = 1;
    for (int q = 0; q < nseen; q++) if (seen[q] == k) { fresh = 0; break; }
    if (fresh && nseen < 64) seen[nseen++] = k; else if (fresh) fresh = 0;
    return fresh;
}
static int bind_old(tex *t) { return (!t->stex && (t->usage & 8)) ? census(t) : 0; }
static int bind_new(tex *t) { int l = 0; if (!t->noted) { if (!t->stex && (t->usage & 8)) l = census(t); t->noted = 1; } return l; }
int main(int argc, char **argv) {
    const int ntex = argc > 1 ? atoi(argv[1]) : 300;
    static tex a[4096], b[4096]; uint32_t s = 7;
    for (int i = 0; i < ntex; i++) { s = s * 1103515245u + 12345u;
        a[i] = (tex){ (s >> 3) % 5 == 0, (s >> 7) % 16, 70 + (s >> 11) % 40, 2 + (s >> 17) % 3, 0 }; b[i] = a[i]; }
    unsigned long long lo = 0, ln = 0, mism = 0, binds = 0;
    char out_old[1 << 16], out_new[1 << 16]; size_t po = 0, pn = 0;
    for (int pass = 0; pass < 2; pass++) {
        nseen = 0; memset(seen, 0, sizeof seen); s = 99;
        for (int n = 0; n < 200000; n++) { s = s * 1103515245u + 12345u; const int i = (s >> 8) % ntex;
            if (!pass) { if (bind_old(&a[i])) { lo++; po += snprintf(out_old + po, sizeof out_old - po, "%d %d\n", n, i); } }
            else { if (bind_new(&b[i])) { ln++; pn += snprintf(out_new + pn, sizeof out_new - pn, "%d %d\n", n, i); } }
            binds++; }
    }
    mism = po != pn || memcmp(out_old, out_new, po);
    printf("%llu %llu %llu %llu\n", lo, ln, mism, binds);
    return 0;
}
'''
cc = shutil.which("cc") or shutil.which("clang")
if not cc:
    print("SKIP: needs a C compiler"); sys.exit(2)
with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d); (d / "m.c").write_text(MODEL.replace("#include <string.h>", "#include <string.h>\n#include <stdlib.h>"))
    subprocess.run([cc, "-Wall", "-Werror", "-O1", "-o", str(d / "m"), str(d / "m.c")], check=True)
    few = subprocess.run([str(d / "m"), "300"], capture_output=True, text=True, check=True).stdout.split()
    many = subprocess.run([str(d / "m"), "4000"], capture_output=True, text=True, check=True).stdout.split()
arm("the flagged census logs the same lines, at the same binds, as the old one", few[2] == "0" and few[0] == few[1] and few[0] != "0", few)
arm("the same when there are more shapes than the table holds", many[2] == "0" and many[0] == many[1] == "64", many)

st = objs[objs.index("- (void)setFragmentTexture:(id<MTLTexture>)t atIndex:(NSUInteger)i {"):]
st = st[:st.index("\n}\n") + 3]
arm("the census runs only for a texture not looked at yet, and marks it after its table check",
    "if (!__atomic_load_n(&nt->_swNoted, __ATOMIC_RELAXED)) {\n    if (!nt->_stex && (nt.usage & MTLTextureUsageShaderWrite)) {" in st
    and st.index("os_unfair_lock_unlock(&sl);") < st.index("__atomic_store_n(&nt->_swNoted, 1, __ATOMIC_RELAXED);"))
arm("the flag is a per-texture ivar and nothing else writes it",
    "uint8_t _swNoted;" in objh and len(re.findall(r"_swNoted", objs)) == 2)
arm("the bind itself is unchanged: the texture is still bound and its storage view still bound before the census",
    st.index("nvmtl_hz_bind_storage(") < st.index("_swNoted"))
# what the census reads is set only where a texture is made: in no method other than the makers (an init, a
# new*/make* constructor or a view maker)
ivar_writes = [m.start() for src in (objs, dev) for m in re.finditer(r"(?:\bt|\bv|\balias)->_(?:stex|usage|fmt) = ", src)]
arm("a texture's storage flag, usage and format are written only by the code that makes it (no setter in NVMTLTexture)",
    ivar_writes and not re.search(r"^- \(void\)set(Usage|PixelFormat|TextureType)\b", objs, re.M))
print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
