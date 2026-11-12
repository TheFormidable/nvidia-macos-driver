#!/usr/bin/env python3
"""libnvencvt on Blackwell and on the Turing chips other than TU117: what the library needs to know about them.

The vendored library knew Ada, consumer Ampere and TU117 only. On an RTX 50 card it found no channel class, no NVDEC
class and no card kind, so it registered nothing there, and it would have read a decoded plane in the wrong byte order
had it got that far. This checks, without a GPU:

  * the byte order of a decoded plane on Blackwell, against a model written from Mesa NIL's tables (copy.rs), with the
    older layout as the control: it must NOT give the picture back;
  * which layout a plane gets from the NVDEC class and its element size;
  * the class table (control: the revision before this change has no Blackwell class);
  * the card kind from the PCI device ID, asked of the card's display function only: the scan of the I/O registry is
    compiled out of the source and run on made-up registries (control: the audio function of an RTX 5050, whose ID
    lies in the GA10x block, is taken for a GA10x card once it is given a display class);
  * that on Blackwell nothing registers before the H.264 decode self-test has passed in the process: the two entry
    points, compiled out of the source and run on a table of card kinds and verdicts (controls: the same card with a
    passing self-test registers; every other kind registers without being asked);
  * that a channel which stopped fetching cannot make the library write past its push buffer (control: the function
    as it was before does).

usage: test_nvencvt_blackwell.py [repo root]"""
import os, pathlib, platform, random, re, shutil, subprocess, sys, tempfile

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
V = root / 'vendor/nvenc'
BEFORE = '4f4f70d6'      # the revision this change started from
fails = 0


def check(name, ok, detail=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + (f' ({detail})' if detail else ''))
    fails += 0 if ok else 1


def function(src, signature):
    i = src.find(signature)
    return None if i < 0 else src[i:src.index('\n}\n', i) + 3]


def old(path):
    r = subprocess.run(['git', '-C', str(root), 'show', f'{BEFORE}:{path}'], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


# ---- the model: where each run of bytes of a 64 x 8 GOB lies, as Mesa NIL's for_each_gob_line lists them ----
def gob_lines(kind):
    """-> [(offset in the GOB, x, y, run length)]"""
    out = []
    if kind == 'blackwell8':
        for x in range(8):
            for y in range(8):
                out.append((x * 0x40 + y * 8, x * 8, y, 8))
        return out
    groups = {'turing': [(0x00, 0, 0), (0x40, 16, 0), (0x80, 0, 4), (0xc0, 16, 4)],
              'blackwell16': [(0x00, 0, 0), (0x80, 16, 0), (0x40, 0, 4), (0xc0, 16, 4)]}[kind]
    for i in range(2):
        for base, x, y0 in groups:
            for r in range(4):
                out.append((i * 0x100 + base + r * 0x10, i * 32 + x, y0 + r, 16))
    return out


def tile(linear, width, rows, pitch, kind):
    """A plane as the engine writes it: rows of 64 x 16 tiles, each two GOBs one above the other."""
    size = pitch * ((rows + 15) // 16) * 16
    plane = bytearray(b'\xee' * size)
    lines = gob_lines(kind)
    for ty in range((rows + 15) // 16):
        for tx in range(pitch // 64):
            for g in range(2):
                base = ty * pitch * 16 + tx * 1024 + g * 512
                for off, x, y, n in lines:
                    X, Y = tx * 64 + x, ty * 16 + g * 8 + y
                    if Y >= rows or X >= width:
                        continue
                    m = min(n, width - X)
                    plane[base + off:base + off + m] = linear[Y * width + X:Y * width + X + m]
    return bytes(plane)


HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "blocklinear.h"
int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "layouts")) {
        printf("%d %d %d %d %d\n", gob_layout_for(0xcfb0, 1), gob_layout_for(0xcfb0, 2), gob_layout_for(0xcfb0, 4),
               gob_layout_for(0xc9b0, 1), gob_layout_for(0xc4b0, 2));
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "bijection")) {
        for (int l = 0; l < 3; l++) {
            static unsigned char seen[256 * 48]; memset(seen, 0, sizeof seen); int bad = 0;
            for (size_t y = 0; y < 48; y++) for (size_t x = 0; x < 256; x++) {
                size_t o = gob_offset(l, x, y, 256);
                if (o >= sizeof seen || seen[o]++) bad++;
            }
            printf("%d ", bad);
        }
        printf("\n"); return 0;
    }
    // untile <layout> <width> <rows> <pitch> <in> <out>
    int layout = atoi(argv[2]); size_t w = (size_t)atol(argv[3]), rows = (size_t)atol(argv[4]), pitch = (size_t)atol(argv[5]);
    size_t size = pitch * ((rows + 15) / 16) * 16;
    unsigned char *src; if (posix_memalign((void **)&src, 64, size)) return 2;
    unsigned char *dst = malloc(w * rows);
    FILE *f = fopen(argv[6], "rb"); if (!f || fread(src, 1, size, f) != size) return 2; fclose(f);
    gob_untile_plane(layout, dst, w, src, w, rows, pitch);
    f = fopen(argv[7], "wb"); fwrite(dst, 1, w * rows, f); fclose(f);
    return 0;
}
'''

bl = V / 'src/blocklinear.c'
have_cc = shutil.which('clang') and platform.system() == 'Darwin'
check('the library has a layout per generation', 'gob_untile_plane' in bl.read_text() and 'gob_layout_for' in bl.read_text())
if have_cc and 'gob_untile_plane' in bl.read_text():
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / 'h.c').write_text(HARNESS)
        r = subprocess.run(['clang', '-arch', 'x86_64', '-O1', '-I', str(V / 'src'), '-o', str(d / 'h'), str(d / 'h.c'), str(bl),
                            '-framework', 'Accelerate'], capture_output=True, text=True)
        check('the layout code builds on its own', r.returncode == 0, r.stderr.strip()[-400:])
        if r.returncode == 0:
            run = lambda *a, env=None: subprocess.run([str(d / 'h'), *map(str, a)], capture_output=True, text=True, env=env)
            import os
            clean = {k: v for k, v in os.environ.items() if k != 'NVDEC_GOB'}
            got = run('layouts', env=clean).stdout.split()
            check('Blackwell: 1-byte elements, 2-byte elements and wider ones each get their layout; older cards keep Turing\'s',
                  got == ['1', '2', '0', '0', '0'], ' '.join(got))
            got = run('layouts', env=dict(clean, NVDEC_GOB='turing')).stdout.split()
            check('NVDEC_GOB=turing overrides the class', got == ['0', '0', '0', '0', '0'], ' '.join(got))
            got = run('layouts', env=dict(clean, NVDEC_GOB='blackwell')).stdout.split()
            check('NVDEC_GOB=blackwell overrides the class', got == ['1', '2', '0', '1', '2'], ' '.join(got))
            got = run('bijection').stdout.split()
            check('every layout maps each byte of a plane to its own place', got == ['0', '0', '0'], ' '.join(got))
            rnd = random.Random(29)
            # sizes: a width that is no multiple of 64 or of the run, rows that end inside a tile; and a plane tall
            # enough for the threaded path
            for width, rows, pitch in ((200, 70, 256), (1920, 1080, 1920), (100, 9, 128)):
                linear = bytes(rnd.getrandbits(8) for _ in range(width * rows))
                for layout, kind in ((1, 'blackwell8'), (2, 'blackwell16'), (0, 'turing')):
                    (d / 'in').write_bytes(tile(linear, width, rows, pitch, kind))
                    run('untile', layout, width, rows, pitch, d / 'in', d / 'out')
                    out = (d / 'out').read_bytes() if (d / 'out').exists() else b''
                    check(f'{kind} plane {width}x{rows} comes back as the picture', out == linear)
                    if kind != 'turing':
                        run('untile', 0, width, rows, pitch, d / 'in', d / 'out')
                        check(f'control: the Turing layout does not read a {kind} plane {width}x{rows}', (d / 'out').read_bytes() != linear)
else:
    print('SKIP the layout arms need clang on a Mac')

# ---- classes ----
rm = (V / 'src/rm.c').read_text()
want = function(rm, 'NvU32 rm_class(')
check('the class table names Blackwell\'s encoder, decoder and channel classes',
      want is not None and all(c in want for c in ('0xcfb7', '0xcfb0', '0xc96f')))
check('the older classes are all still there',
      want is not None and all(c in want for c in ('0xc9b7', '0xc7b7', '0xc4b7', '0xb4b7', '0xc9b0', '0xc7b0', '0xc4b0', '0xc56f', '0xc46f', '0xc361')))
o = old('vendor/nvenc/src/rm.c')
if o is None:
    print('skip the control for the class table: this is not a full clone')
else:
    check('control: the revision before had no Blackwell class', '0xc96f' not in o and '0xcfb0' not in o)

# ---- card kinds ----
vt = (V / 'vt/nvencvt.c').read_text()
kind = function(vt, 'static int gpu_kind_for_device(')
check('the card kind is a function of the PCI device ID', kind is not None)
cc = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
if not cc:
    print('SKIP the card kind tables need a C compiler')
if kind and cc:
    enum = vt[vt.index('enum { GPU_NONE'):vt.index('\n', vt.index('enum { GPU_NONE'))]
    src = ('#include <stdio.h>\n' + enum + '\n' + kind +
           'int main(void) { unsigned ids[] = { 0x2d05, 0x2b85, 0x2f04, 0x2684, 0x2484, 0x1f82, 0x1f08, 0x1e84, 0x2184, 0x1c03, 0x3000 };\n'
           '  for (int i = 0; i < 11; i++) printf("%d ", gpu_kind_for_device(ids[i])); return 0; }\n')
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d); (d / 'k.c').write_text(src)
        r = subprocess.run([cc, '-o', str(d / 'k'), str(d / 'k.c')], capture_output=True, text=True)
        got = subprocess.run([str(d / 'k')], capture_output=True, text=True).stdout.split() if r.returncode == 0 else [r.stderr[-200:]]
        # RTX 5060, 5090, 5070 -> Blackwell 4; RTX 4090 -> Ada 1; RTX 3070 -> Ampere 3; GTX 1650 -> TU117 2;
        # RTX 2060, 2080, GTX 1660 -> Turing 5; GTX 1060 (Pascal) and an ID past Blackwell -> none
        check('RTX 50 is Blackwell, RTX 20 and GTX 1660 are Turing, the kinds known before are unchanged, Pascal is none',
              got == ['4', '4', '4', '1', '3', '2', '5', '5', '5', '0', '0'], ' '.join(got))

# The device ID is asked of the GPU and of nothing else. The audio function of an RTX 40 card, of most RTX 50 cards and
# of the smaller RTX 30 cards has an ID (0x228b-0x22ec) inside the GA10x block, and the registry does not promise to
# list a card's functions in order. The scan is compiled out of the source and run on made-up registries.
REGISTRY = r'''
#include <stdio.h>
#include <string.h>
/* stand-ins for the I/O registry: PCI functions in the order the registry hands them out, every property four bytes,
   as IOPCIFamily publishes vendor-id, device-id and class-code */
typedef unsigned io_iterator_t, io_object_t, CFTypeID;
typedef unsigned char UInt8;
typedef long CFIndex;
typedef const void *CFTypeRef, *CFStringRef;
typedef struct { CFIndex location, length; } CFRange;
static CFRange CFRangeMake(CFIndex l, CFIndex n) { CFRange r = { l, n }; return r; }
enum { KERN_SUCCESS = 0, kIOMainPortDefault = 0 };
#define CFSTR(s) (s)
typedef struct { unsigned vendor, device, cls; } Fn;
static const Fn *reg;
static unsigned nreg, at, named, held;   /* named: the function the card's name was read from; held: properties not released */
static const char *IOServiceMatching(const char *name) { return name; }
static int IOServiceGetMatchingServices(int port, const char *match, io_iterator_t *it) { (void)port; (void)match; at = 0; *it = 1; return KERN_SUCCESS; }
static io_object_t IOIteratorNext(io_iterator_t it) { (void)it; return at < nreg ? ++at : 0; }
static void IOObjectRelease(unsigned o) { (void)o; }
static CFTypeRef IORegistryEntryCreateCFProperty(io_object_t d, CFStringRef key, void *alloc, unsigned opts)
{
    const Fn *f = &reg[d - 1]; (void)alloc; (void)opts;
    const unsigned *p = !strcmp(key, "vendor-id") ? &f->vendor : !strcmp(key, "device-id") ? &f->device
                      : !strcmp(key, "class-code") ? &f->cls : NULL;
    if (p) held++;
    return p;
}
static CFTypeID CFDataGetTypeID(void) { return 20; }
static CFTypeID CFGetTypeID(CFTypeRef p) { (void)p; return 20; }
static CFIndex CFDataGetLength(CFTypeRef p) { (void)p; return 4; }
static void CFDataGetBytes(CFTypeRef p, CFRange r, UInt8 *out) { memcpy(out, (const UInt8 *)p + r.location, (size_t)r.length); }
static void CFRelease(CFTypeRef p) { (void)p; held--; }
static void card_name_from(io_object_t d) { named = d; }
%(enum)s
%(kind)s
%(function)s
%(scan)s
#define RUN(...) do { static const Fn r[] = { __VA_ARGS__ }; reg = r; nreg = sizeof r / sizeof r[0]; named = 0; \
                      const int k = pci_gpu_kind(); printf("%%d %%u %%u\n", k, named, held); } while (0)
enum { NV = 0x10de, VGA = 0x030000, THREE_D = 0x030200, AUDIO = 0x040300, USB = 0x0c0330, SERIAL = 0x0c8000 };
int main(void)
{
    printf("%%d\n", gpu_kind_for_device(0x22ec));
    RUN({ NV, 0x2d83, VGA }, { NV, 0x22ec, AUDIO });                             /* an RTX 5050, then its audio function */
    RUN({ NV, 0x22ec, AUDIO }, { NV, 0x2d83, VGA });                             /* the same card, the audio function first */
    RUN({ NV, 0x22ba, AUDIO }, { NV, 0x2684, VGA });                             /* an RTX 4090, audio first */
    RUN({ NV, 0x228b, AUDIO }, { NV, 0x2484, VGA });                             /* an RTX 3070, audio first */
    RUN({ NV, 0x22ec, AUDIO });                                                  /* no display function at all */
    RUN({ 0x1002, 0x2684, VGA }, { NV, 0x2d83, THREE_D });                       /* another vendor's GPU with an ID of ours, then a 3D controller */
    RUN({ NV, 0x1c03, VGA }, { NV, 0x10f1, AUDIO });                             /* a GTX 1060 */
    RUN({ NV, 0x1c03, VGA }, { NV, 0x2d83, VGA });                               /* a GTX 1060, then an RTX 5050 */
    RUN({ NV, 0x2684, VGA }, { NV, 0x2d83, VGA });                               /* an RTX 4090, then an RTX 5050 */
    RUN({ NV, 0x1ad8, USB }, { NV, 0x1ad9, SERIAL }, { NV, 0x10f8, AUDIO }, { NV, 0x1e87, VGA });   /* an RTX 2080's four functions, the GPU last */
    RUN({ NV, 0x22ec, VGA }, { NV, 0x2d83, VGA });                               /* control: that ID with a display class */
    reg = NULL; nreg = 0; named = 0;
    printf("%%d %%u %%u\n", pci_gpu_kind(), named, held);                         /* an empty registry */
    return 0;
}
'''
scan = function(vt, 'static int pci_gpu_kind(')
perfn = function(vt, 'static int gpu_kind_for_function(')
check('the registry scan asks each function for its class as well as its ID',
      scan is not None and perfn is not None and 'gpu_kind_for_function(' in scan and 'gpu_kind_for_device(' not in scan
      and 'CFSTR("class-code")' in scan)
if kind and scan and perfn and cc:
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / 'r.c').write_text(REGISTRY % {'enum': enum, 'kind': kind, 'function': perfn, 'scan': scan})
        r = subprocess.run([cc, '-Wall', '-Werror', '-Wno-unused-function', '-o', str(d / 'r'), str(d / 'r.c')], capture_output=True, text=True)
        check('the registry scan compiles on its own', r.returncode == 0, r.stderr.strip()[-400:])
        if r.returncode == 0:
            out = subprocess.run([str(d / 'r')], capture_output=True, text=True).stdout.split('\n')
            alone, rows = out[0].strip(), [tuple(map(int, l.split())) for l in out[1:] if l.strip()]
            # a row: the kind found (-1: no NVIDIA display function, the library then asks RM), the function the card's
            # name was read from (counted from 1, 0: none), registry properties still held
            check('a card is its display function: the audio function\'s ID decides nothing, wherever the registry lists it',
                  rows[:4] == [(4, 1, 0), (4, 2, 0), (1, 2, 0), (3, 2, 0)], str(rows[:4]))
            check('without an NVIDIA display function there is no card to name', rows[4] == (-1, 0, 0) and rows[11] == (-1, 0, 0), str(rows[4:5] + rows[11:]))
            check('a 3D controller is a display function, and another vendor\'s GPU is not ours whatever its ID', rows[5] == (4, 2, 0), str(rows[5]))
            check('a GPU of no known kind is still the card (0, not -1), unless a known one follows; the first known one stays',
                  rows[6:10] == [(0, 0, 0), (4, 2, 0), (1, 1, 0), (5, 4, 0)], str(rows[6:10]))
            check('control: by its ID alone that audio function is a GA10x card, and the scan would say so had it a display class',
                  alone == '3' and rows[10] == (3, 1, 0), f'{alone} {rows[10]}')
            check('every property the scan reads is released', all(r[2] == 0 for r in rows))
dec = (V / 'vt/nvencvt_dec.c').read_text()
reg = function(dec, 'OSStatus NVDecVT_Register(')
check('Blackwell registers the H.264 decoder alone',
      reg is not None and 'h264Only = gNvencvtGpu == 4' in reg and reg.count('!h264Only') == 3)
regenc = function(vt, '__attribute__((visibility("default"))) OSStatus NVEncVT_Register(')
check('no encoder registers on Blackwell or on the Turing chips without an encoder setup',
      regenc is not None and 'encoders = gpu == GPU_ADA || gpu == GPU_TU117 ||' in regenc)
h264 = (V / 'src/dec.c').read_text()
check('the H.264 decoder reads its planes in the card\'s layout, cropped pictures included',
      'nvenc_in_untile_plane' not in h264 and 'nvenc_in_offset' not in h264 and h264.count('gob_offset(d->gob') == 4)

# ---- the decode self-test gate ----
GATE = 'if (gpu == GPU_BLACKWELL && !blackwell_decode_proven()) return kVTCouldNotFindVideoDecoderErr;'
proven = function(vt, 'static bool blackwell_decode_proven(')
regdec = function(vt, '__attribute__((visibility("default"))) OSStatus NVEncVT_RegisterDecoders(')
before = lambda fn, *later: fn is not None and GATE in fn and all(fn.index(GATE) < fn.index(l) for l in later)
check('both entry points ask for the Blackwell self-test before anything registers',
      before(regenc, 'NVDecVT_Register', 'register_one(') and before(regdec, 'NVDecVT_Register'))
CALL = 'NVDecVT_Register(rating)'
# every mention of the registering function, whatever its argument: its definition, and the two entry points
named = lambda text: len(re.findall(r'\bNVDecVT_Register\b', text))
elsewhere = sum(named(f.read_text(errors='ignore')) for f in sorted(V.rglob('*'))
                if f.suffix in ('.c', '.h', '.m') and f.name not in ('nvencvt.c', 'nvencvt_dec.c'))
check('and no third place registers a decoder: the two entry points hold every call, behind the gate',
      regenc is not None and regdec is not None and reg is not None
      and vt.count(CALL) == regenc.count(CALL) + regdec.count(CALL) == 3 and CALL not in dec
      and named(vt) == named(regenc) + named(regdec) and named(dec) == 1 and elsewhere == 0
      and all(f.index(GATE) < f.index(CALL) for f in (regenc, regdec) if GATE in f)
      and dec.count('VTRegisterVideoDecoderWithInfo') == reg.count('VTRegisterVideoDecoderWithInfo') > 0)
check('nor an encoder: the one function that registers encoders is called from the entry point alone',
      regenc is not None and vt.count('register_one(') == regenc.count('register_one(') + 1
      and vt.count('VTRegisterVideoEncoderWithInfo(') == 2 and 'VTRegisterVideoEncoderWithInfo' not in dec)
check('the self-test is the one of src/h264kat.c, asked once per process',
      proven is not None and 'h264kat_proven("Blackwell"' in proven and '#include "h264kat.h"' in vt)
kat = (V / 'src/h264kat.c').read_text() if (V / 'src/h264kat.c').exists() else ''
check('it decodes with the entry points a VideoToolbox session uses',
      all(f in kat for f in ('h264dec_open(', 'h264dec_nal(', 'h264dec_flush(', 'h264dec_pop(', 'h264dec_read_nv12(',
                             'h264dec_release(', 'h264dec_close(')))
check('it reports the layout a plane would have been right in and never changes how the decoder reads',
      'gob_untile_plane(' in kat and not re.search(r'gob[YC]\s*=[^=]', kat))
# The plugin asks for the library on Blackwell by default (in the decode service), which is only right beside a build
# that has the gate above. The build says so with one exported marker, and the plugin refuses a library without it on
# Blackwell (tools/test_nvencvt_selection.py holds the plugin's side).
MARKER = '__attribute__((visibility("default"))) const int NVEncVT_BlackwellSelfTest = 1;'
exported = sorted('_' + m for f in sorted(V.rglob('*.c'))
                  for m in re.findall(r'(?m)^__attribute__\(\(visibility\("default"\)\)\)[^;{(=]*?(\w+)\s*[(=]', f.read_text(errors='ignore')))
build = (root / 'build/build_nvencvt.sh').read_text()
want = re.search(r'(?m)^want="([^"]*)"', build)
check('a build with the gate says so: it exports a marker for the plugin to look up',
      vt.count(MARKER) == 1 and proven is not None and vt.index(proven) < vt.index(MARKER) < vt.index(regenc or MARKER))
check('the build holds the library to exactly the symbols the source exports, the marker among them',
      want is not None and want.group(1).split() == exported and '_NVEncVT_BlackwellSelfTest' in exported and len(exported) == 5,
      ' '.join(exported))
TABLE = r'''
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
typedef int OSStatus;
enum { kVTCouldNotFindVideoDecoderErr = -12906, kVTCouldNotFindVideoEncoderErr = -12908, H264KAT_LINE = 800 };
%(enum)s
#define LOG(...) do { } while (0)
static int kind, verdict, asked, registered;   /* the card, the self-test's answer, and what was called */
static bool h264kat_proven(const char *card, char *line, size_t n, bool *first)
{
    snprintf(line, n, "%%s", card); *first = !asked++;
    return verdict;
}
static int pci_gpu_kind(void) { return kind; }
static int gpu_nvenc_kind(void) { return kind; }
int gNvencvtGpu;
OSStatus NVDecVT_Register(int rating) { (void)rating; registered++; return 0; }
%(proven)s
%(regdec)s
%(regenc)s
    return 1000;   /* the rest of the function: it got past the check */
}
int main(void)
{
    for (kind = 1; kind <= 5; kind++) for (verdict = 0; verdict < 2; verdict++) {
        asked = registered = 0;
        const int a = NVEncVT_RegisterDecoders(1100), ra = registered, b = NVEncVT_Register(1100);
        printf("%%d %%d %%d %%d %%d %%d\n", kind, verdict, a, ra, b, asked);
    }
    return 0;
}
'''
cc = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
if proven and before(regenc) and before(regdec) and cc:
    enum = vt[vt.index('enum { GPU_NONE'):vt.index('\n', vt.index('enum { GPU_NONE'))]
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / 'g.c').write_text(TABLE % {'enum': enum, 'proven': proven, 'regdec': regdec,
                                        'regenc': regenc[:regenc.index(GATE) + len(GATE)]})
        r = subprocess.run([cc, '-Wall', '-Werror', '-o', str(d / 'g'), str(d / 'g.c')], capture_output=True, text=True)
        check('the entry points up to the check compile on their own', r.returncode == 0, r.stderr.strip()[-400:])
        if r.returncode == 0:
            env = {k: v for k, v in os.environ.items() if k != 'NVENCVT_ANY_GPU'}
            rows = [tuple(map(int, l.split())) for l in subprocess.run([str(d / 'g')], capture_output=True, text=True, env=env).stdout.split('\n') if l.strip()]
            got = {(k, v): rest for k, v, *rest in rows}
            # kinds: 1 Ada, 2 TU117, 3 Ampere, 4 Blackwell, 5 Turing; rest: RegisterDecoders' status, decoders registered by
            # it, Register's status (1000: past the check), times the self-test was asked for
            check('Blackwell with a failed self-test: both entry points return the decoder error and nothing registers',
                  got.get((4, 0)) == [-12906, 0, -12906, 2])
            check('control: Blackwell with a passed self-test registers the decoders and goes on',
                  got.get((4, 1)) == [0, 1, 1000, 2])
            check('every other kind registers as before and the self-test is never asked for',
                  all(got.get((k, v)) == [0, 1, 1000, 0] for k in (1, 2, 3, 5) for v in (0, 1)))
elif not cc:
    print('SKIP the table of entry points needs a C compiler')

# ---- the push buffer ----
STUB = r'''
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
typedef unsigned NvU32;
typedef struct { NvU32 *cur, *end; bool overflow; } Chan;
#define DRF_NUM(a, b, c, v) ((NvU32)(v))
#define NVC56F_DMA_SEC_OP_INC_METHOD 1
%s
int main(void) {
    static NvU32 mem[64 + 64];
    for (int i = 0; i < 64; i++) mem[64 + i] = 0xfeedfaceu;
    Chan c = { mem, mem + 64, false };
    for (int i = 0; i < 100; i++) chan_mthd(&c, 0, 0x400, 3, 1u, 2u, 3u);
    int past = 0; for (int i = 0; i < 64; i++) past += mem[64 + i] != 0xfeedfaceu;
    printf("%%d %%d\n", past, (int)c.overflow); return 0; }
'''
ch = (V / 'src/channel.c').read_text()
if have_cc:
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)

        def past(source):
            fn = function(source, 'void chan_mthd(')
            (d / 'p.c').write_text(STUB % fn)
            r = subprocess.run(['clang', '-o', str(d / 'p'), str(d / 'p.c')], capture_output=True, text=True)
            if r.returncode:
                return None, r.stderr[-300:]
            return subprocess.run([str(d / 'p')], capture_output=True, text=True).stdout.split(), ''
        got, err = past(ch)
        check('methods that do not fit the push buffer are dropped and the channel is marked', got == ['0', '1'], err or ' '.join(got or []))
        oc = old('vendor/nvenc/src/channel.c')
        if oc is None:
            print('skip the control for the push buffer: this is not a full clone')
        else:
            got, err = past(oc)
            check('control: the function as it was wrote past the buffer', got is not None and int(got[0]) > 0, err or ' '.join(got or []))
check('a submission after an overflow fails instead of sending a cut picture',
      'if (c->overflow)' in function(ch, 'NV_STATUS chan_submit('))
check('the push buffer is reused after a completion the decoder saw, not only by the GPGet word',
      '!c->settled && chan_gpget(c) != c->gpPut' in ch and 'chan_done(c);' in h264)

print(f'{fails} failed' if fails else 'all passed')
sys.exit(1 if fails else 0)
