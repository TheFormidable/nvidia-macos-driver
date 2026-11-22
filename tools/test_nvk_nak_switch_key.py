#!/usr/bin/env python3
"""Every switch the shader compiler reads is part of NVK's shader cache key (nvk/nvk-nak-switch-key.patch).

nvk_physical_device_init_pipeline_cache (src/nouveau/vulkan/nvk_physical_device.c) hashes the key into
pipelineCacheUUID and shaderBinaryUUID and hands it to the disk cache. A switch read on the way from SPIR-V to the
cached nvk_shader that is not in the key gets, from the disk cache or from the VkPipelineCache data Zink keeps, a
binary compiled under the other setting. The patch puts a digest of every such switch that is set into bits 16..47 of
the value of nvk_physical_device_compiler_flags there. This test keeps that list complete without a Mesa tree:

  the reads    every patch of the 3.0 chain, in chain order (build/build_nvk.sh, read by tools/switch_registry.py), is
               replayed line by line: a name read on a + line of a file on the compile path counts once more, on a -
               line once less; the files build_nvk.sh copies into the tree after the chain count too. Each name still
               read at the end must be in the key's table, or in COVERED or EXEMPT below with the reason. A read by a
               name that is not a string literal on that path fails too, and so does one whose name is on the next
               line, and a Rust file that imports std::env's functions by name (the reads would then not be seen).
  upstream     the names in UPSTREAM are read by Mesa's own SPIR-V reader, on no + line of the chain; they change what
               a release build compiles and upstream keys them nowhere, so the table has to have them.
  the reader   each name is read in the key the way the compiler reads it, and only that way (getenv, or
               nvmtl_cfg_getenv, which also looks in nvrm610.conf): a key that saw a settings-file line the compiler
               does not see would file a binary compiled without the switch under the key of one compiled with it,
               and a switch the compiler reads both ways has no reader the key can copy.
  the digest   the patch's own digest code, cut out of the patch and built with a stand-in environment: 0 with nothing
               set (the key is then the one 3.0.8 computes), never 0 with something set, a different value for each
               switch and for "0", "" and unset, every one of its four bytes in use, two switches together unlike
               either alone, and the settings file counted for NVMTL_NVK_NO_LCONST16 only.
  the key      the digest goes to bits 16..47 of the value both the UUIDs and the disk cache get; in the hunks of
               nvk_physical_device_compiler_flags over the whole chain every bit is written as `<< N` with N one of
               the bits that function has today, and nothing there puts bits in the key another way (a hunk belongs
               to the function when it shows one of its lines, the lines earlier hunks added included); and the patch
               sits in the 3.0 chain once, after every other patch that changes nvk_physical_device.c, and not in the
               chains of 2.9 and 2.8.
  controls     copies of the tree with one rule broken each: a switch dropped from the table, an upstream one dropped,
               the wrong reader, a later patch that reads a new switch, one that reads a keyed switch the other way,
               one that reads through a variable, one that reads through a macro of Mesa's, one that takes a bit of
               the key, one that takes it without a marker line in its hunk and without a shift, a digest that is not
               0 with nothing set, a digest of 24 bits, a digest put at the wrong bits, a disk cache left with the old
               key, and the patch moved before one that changes the same file. Each has to fail the arm that names
               its rule and no other; a later patch that shifts by 16 outside the key's function has to pass.

Blind spots: a read through a helper this test does not know by name (a new wrapper that hands its parameter to getenv
is caught as a read that is not a literal, nvmtl_cfg.h's own apart; the names its callers pass are then not seen); a
switch read on the compile path from a file outside COMPILE_PATH (an NVK file other than nvk_shader.c and nvk_nir_*.c
that a later patch calls from the compile), or written into the tree by nvk/dlss/patch_dlss_cache.py; a read upstream
Mesa has that is not in UPSTREAM (that list was made by reading src/compiler/spirv and src/compiler/nir of the pinned
Mesa, where NIR_DEBUG and NIR_SKIP are compiled out of a release build, and a new Mesa base needs that reading again);
a bit of the key computed outside nvk_physical_device_compiler_flags and brought in under a name; and a switch whose
effect is in what NVK computes at draw or dispatch time from a cached shader, which needs no key. It does not show
that the process's environment is final when the key is computed: in a process where the Metal plugin loads after NVK
made its device, the plugin's own changes to the environment come after the key.
Without a C compiler (cc, clang or $CC) the arms that read text still run and fail the test if one fails; the digest
arms and the controls are skipped and the test says so (exit 2).
Usage: test_nvk_nak_switch_key.py"""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import switch_registry as sr  # noqa: E402  (the chain as build/build_nvk.sh applies it)

NAME = "nvk-nak-switch-key"
KEYFILE = "src/nouveau/vulkan/nvk_physical_device.c"   # not on COMPILE_PATH: its reads are the key's own
KEYFN_FILE = "src/nouveau/vulkan/nvk_shader.c"         # where nvk_physical_device_compiler_flags is
# the files whose code runs between SPIR-V and the nvk_shader NVK serializes
COMPILE_PATH = re.compile(r"^src/nouveau/compiler/"
                          r"|^src/nouveau/vulkan/(?:nvk_shader\.[ch]|nvk_nir[^/]*\.[ch]|nvmtl_cfg\.h)$"
                          r"|^src/compiler/(?:nir|spirv)/"
                          r"|^src/vulkan/runtime/vk_(?:pipeline|shader|nir)[^/]*\.[ch]$")
# read by a patch of the chain on the compile path, and in the key by another route
COVERED = {
    "NVMTL_NAK_ARR_SCRATCH": "bit 15 of the key since nvk-arrscratch",
}
# read by a patch of the chain on the compile path, and rightly not in the key
EXEMPT = {
    "NAK_SMEM_TARGET_CAP_KB": "qmd.rs: changes only the QMD, which nak_fill_qmd fills at every dispatch (nvk_cmd_dispatch.c)",
    "NAK_QMD_NO_PREFETCH": "qmd.rs: changes only the QMD, which nak_fill_qmd fills at every dispatch (nvk_cmd_dispatch.c)",
    "NVK_DUMP_SHADER_DIR": "nvk_shader_upload writes the dump, and a shader from a cache is uploaded too",
}
# Upstream's own reads on that path are on no + line, so the replay does not see them. These two change what a release
# build compiles and upstream keys neither, so the key's table has to have them; both are read from the environment.
# The others need nothing: NAK_DEBUG is in the key (bits 48..63, nak_debug_flags), NVK_DEBUG's no_cbuf and edb_bview
# are bits 12 and 13, NIR_DEBUG and NIR_SKIP are compiled out of a release build, MESA_SPIRV_DEBUG, MESA_SPIRV_LOG_LEVEL
# and the two MESA_SPIRV_*DUMP_PATH only print or write a file, NAK_TEST_DEVICE is read by the hardware test binary
# only, and NV50_PROG_DEBUG acts in nvk_shader_upload in debug builds, which a shader from a cache goes through too.
UPSTREAM = {
    "MESA_SPIRV_FORCE_UNSTRUCTURED": "vtn_cfg.c (debug_get_bool_option): spirv_to_nir builds unstructured control flow",
    "MESA_SPIRV_READ_PATH": "spirv_to_nir.c (os_get_option_secure): a module is swapped for a file after its hash was taken",
}
KEY_BITS = {12, 13, 14, 15, 48}   # what nvk_physical_device_compiler_flags itself uses, with the chain applied
# The lines of nvk_physical_device_compiler_flags in the pinned Mesa that no other function of nvk_shader.c has. A hunk
# of that file belongs to the function when it shows one of them between two ends of functions, as context or as a
# changed line; the lines such a hunk adds join the list for the hunks after it.
KEY_FUNCTION = ("nvk_physical_device_compiler_flags(const struct nvk_physical_device *pdev)",
                "bool no_cbufs = pdev->debug_flags & NVK_DEBUG_NO_CBUF;",
                "bool use_edb_buffer_views = nvk_use_edb_buffer_views(pdev);",
                "uint64_t nak_flags = nak_debug_flags(pdev->nak);",
                "assert(nak_flags <= UINT16_MAX);",
                "return (no_cbufs ? 1 << 12 : 0)",
                "| (use_edb_buffer_views ? 1 << 13 : 0)",
                "| (instance->drirc.misc.ssbo_align_4b ? 1 << 14 : 0)",
                "| (nak_flags << 48);")
# a reader's name, then its argument: a literal name, something else, or nothing more on the line
ARG = r"\s*(\"([A-Za-z0-9_]+)\"|[^,)\s][^,)]*|\)|$)"
C_READ = re.compile(r"\b(getenv|secure_getenv|os_get_option\w*|debug_get_\w*option\w*|nvmtl_cfg_getenv)\s*\(" + ARG)
C_ONCE = re.compile(r"\b(DEBUG_GET_ONCE_\w*OPTION)\s*\(\s*\w+\s*," + ARG)      # the name is the second argument
RS_READ = re.compile(r"\b(env::var_os|env::vars_os|env::vars|env::var)\s*\(" + ARG)
RS_USE = re.compile(r"\buse\s+std::env::\s*[{\w*]")
A_MISSING = "every switch the compile path reads is in the key, or in COVERED or EXEMPT with its reason"
A_LITERAL = "no switch on the compile path is read by a name this test cannot see (not a string literal on the line)"
A_READER = "each switch is read in the key the way the compiler reads it, and the compiler reads it one way only"
A_UPSTREAM = "the switches of upstream's SPIR-V reader that change a release build's code are in the key"
A_ZERO = "with no switch set the digest is 0, so the key is the one 3.0.8 computes"
A_FILE = "the settings file counts for the switches the compiler reads through nvmtl_cfg_getenv, and only for them"
A_WIDE = "the digest is 32 bits wide: every one of its four bytes varies over the switches and values"
A_PLACE = "the digest goes to bits 16..47 of the key that pipelineCacheUUID hashes"
A_DISK = "the disk cache gets the same key"
A_BITS = "in nvk_physical_device_compiler_flags every bit is a `<< N` the function has today, none of them in 16..47"
A_ORDER = "the patch is in the 3.0 chain once, after every other patch that changes nvk_physical_device.c"
CC = shlex.split(os.environ.get("CC", "")) or [c for c in (shutil.which("cc"), shutil.which("clang")) if c][:1]

HARNESS_HEAD = r'''
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
static const char *env_name[2], *env_value[2], *cfg_name, *cfg_value;
static const char *fake_getenv(const char *n)
{
   for (int i = 0; i < 2; i++)
      if (env_name[i] && !strcmp(n, env_name[i])) return env_value[i];
   return NULL;
}
static const char *nvmtl_cfg_getenv(const char *n)
{ const char *e = fake_getenv(n); return e ? e : (cfg_name && !strcmp(n, cfg_name) ? cfg_value : NULL); }
#define getenv fake_getenv
'''
HARNESS_TAIL = r'''
#undef getenv
int main(int argc, char **argv)
{
   static const char *values[] = { "1", "0", "", "2", "32" };
   printf("none %u\n", nvk_nak_switch_digest());
   for (int i = 1; i < argc; i++)
      for (unsigned v = 0; v < ARRAY_SIZE(values); v++) {
         env_name[0] = argv[i]; env_value[0] = values[v];
         printf("env %s=%s %u\n", argv[i], values[v], nvk_nak_switch_digest());
         env_name[0] = NULL; cfg_name = argv[i]; cfg_value = values[v];
         printf("cfg %s=%s %u\n", argv[i], values[v], nvk_nak_switch_digest());
         cfg_name = NULL;
      }
   for (int i = 1; i + 1 < argc; i++) {
      env_name[0] = argv[i]; env_value[0] = "1"; env_name[1] = argv[i + 1]; env_value[1] = "1";
      printf("two %s+%s %u\n", argv[i], argv[i + 1], nvk_nak_switch_digest());
      env_name[0] = env_name[1] = NULL;
   }
   return 0;
}
'''


def comment(body, star):
    """A line that is only a comment. One that starts with a star is the inside of a block comment or a product that
    goes on from the line before: it is taken for a comment where that is the careful reading (star), and for code
    where code is (a + line that reads a switch)."""
    return re.match(r"\s*(//|/\*|\*( |/|$))" if star else r"\s*(//|/\*)", body) is not None


def key_function(chain):
    """-> (shifts, unread) over the hunks of nvk_physical_device_compiler_flags in the chain: every `<< N` a + line
    has, and every + line that puts bits in the key in a way this test cannot read."""
    marks = set(KEY_FUNCTION)
    shifts, unread = [], []
    for pf in chain:
        if not pf.is_file():
            continue
        for sec in re.split(r"(?m)^\+\+\+ ", pf.read_text(errors="replace"))[1:]:
            if not sec.startswith("b/" + KEYFN_FILE):
                continue
            for hunk in re.split(r"(?m)^@@ ", sec)[1:]:
                parts, part = [], []
                for line in hunk.splitlines()[1:]:
                    if line.startswith("\\"):
                        continue                # "\ No newline at end of file"
                    line = line or " "          # a blank line of context that lost its space
                    if line[0] not in " +-" or line.startswith("--- "):
                        break                   # the next file's header
                    part.append(line)
                    if line[0] != "-" and line[1:].rstrip() == "}":
                        parts.append(part)      # the end of a function
                        part = []
                parts.append(part)
                for part in parts:
                    if not any(l[1:].strip() in marks for l in part):
                        continue
                    for l in part:
                        body = l[1:].strip()
                        if l[0] != "+" or comment(body, star=False):
                            continue
                        code = re.sub(r"/\*.*?\*/|//.*$|/\*.*$", "", body)
                        here = [int(n) for n in re.findall(r"<<\s*(\d+)", code)]
                        shifts += [(pf.name, n) for n in here]
                        if (re.search(r"<<(?!\s*\d)", code) or re.search(r"\bBITFIELD\w*\s*\(", code)
                                or any(int(c, 0) >= 1 << 16 for c in re.findall(r"\b(0[xX][0-9a-fA-F]+|[1-9]\d*)", code))
                                or (not here and re.search(r"(?<!\|)\|(?!\|)|^return\b", code))):
                            unread.append(f"{pf.name}: {body}")
                        if len(body) >= 10:
                            marks.add(body)
    return shifts, unread


def read(root):
    """-> ([(arm, passed, detail)], the EXEMPT names still read) for the tree at root"""
    out = []

    def arm(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    patch = root / "nvk" / (NAME + ".patch")
    chain, copied, problems = sr.nvk_chain(root)
    arm("build/build_nvk.sh's chain was read", chain and not problems, problems)

    # --- the reads, replayed over the chain
    live = defaultdict(int)            # (name, reader) -> count
    where = defaultdict(list)
    nonliteral = []

    def scan(body, sign, target, at):
        if comment(body, star=sign < 0):
            return
        if sign > 0 and target.endswith(".rs") and RS_USE.search(body):
            nonliteral.append(f"{at}: {body.strip()} (write env::var or env::var_os where the switch is read)")
        for m in list(C_READ.finditer(body)) + list(C_ONCE.finditer(body)) + list(RS_READ.finditer(body)):
            fn, name = m.group(1), m.group(3)
            if name is None:
                arg = m.group(2).strip()
                if arg.startswith("const "):
                    continue                    # the definition of a reader, not a read
                if fn.startswith("debug_get_") and arg == ")":
                    continue                    # debug_get_option_<x>(): what a DEBUG_GET_ONCE_ macro defines
                # the readers' own bodies (a parameter handed on) and upstream's NAK_DEBUG reader
                if sign > 0 and not (target.endswith("nvmtl_cfg.h") and arg == "name") and arg != "debug_var":
                    nonliteral.append(f"{at}: {fn}({arg if arg != ')' else ''}" + (")" if arg else " and the name on another line"))
                continue
            live[(name, "cfg" if fn == "nvmtl_cfg_getenv" else "env")] += sign
            if sign > 0:
                where[name].append(at)

    for pf in chain:
        if not pf.is_file():
            arm(f"{pf.name} exists", False, "named by build/build_nvk.sh")
            continue
        target = None
        for i, line in enumerate(pf.read_text(errors="replace").splitlines(), 1):
            if line.startswith("+++ "):
                target = re.sub(r"^b/", "", line[4:].split("\t")[0].strip())
                continue
            if line.startswith("--- ") or line[:1] not in ("+", "-") or not target or not COMPILE_PATH.search(target):
                continue
            scan(line[1:], 1 if line[0] == "+" else -1, target, f"{pf.relative_to(root)}:{i}")
    for cf in copied:                  # copied into src/nouveau/vulkan after the chain, some of it NIR builders
        for i, line in enumerate(cf.read_text(errors="replace").splitlines(), 1):
            scan(line, 1, str(cf.name), f"{cf.relative_to(root)}:{i}")
    arm(A_LITERAL, not nonliteral, "; ".join(nonliteral))

    # --- the key's own table and readers, from the patch's + lines
    ptext = patch.read_text() if patch.is_file() else ""
    added = "\n".join(l[1:] for l in ptext.splitlines() if l.startswith("+") and not l.startswith("+++"))
    table = re.search(r"static const char \*const nvk_nak_key_switches\[\] = \{(.*?)\};", added, re.S)
    key_env = set(re.findall(r"\"([A-Z0-9_]+)\"", table.group(1))) if table else set()
    digest = re.search(r"(static uint32_t\nnvk_nak_switch_mix\(.*?\n\}\n.*?static uint32_t\nnvk_nak_switch_digest\(void\)\n\{.*?\n\}\n)",
                       added, re.S)
    key_cfg = set(re.findall(r"nvmtl_cfg_getenv\(\"([A-Z0-9_]+)\"\)", digest.group(1))) if digest else set()
    if digest:                         # a name the digest reads with getenv outside the table
        key_env |= set(re.findall(r"(?<![A-Za-z0-9_])getenv\(\"([A-Z0-9_]+)\"\)", digest.group(1)))
    arm("the patch has the key's table and its digest", table and digest, patch)
    compiler = {}
    for (name, reader), n in live.items():
        if n > 0:
            compiler.setdefault(name, set()).add(reader)

    missing = sorted(n for n in compiler if n not in key_env | key_cfg and n not in COVERED and n not in EXEMPT)
    arm(A_MISSING, not missing, "; ".join(f"{n} ({', '.join(where[n][:2])})" for n in missing))
    stale = sorted((key_env | key_cfg) - set(compiler) - set(UPSTREAM))
    arm("the key's table names only switches the compile path still reads", not stale, ", ".join(stale))
    both = sorted((key_env | key_cfg) & (set(COVERED) | set(EXEMPT)))
    arm("no switch is both in the key's table and in COVERED or EXEMPT", not both, ", ".join(both))
    gone = sorted((set(COVERED) | set(EXEMPT)) - set(compiler))
    arm("COVERED and EXEMPT name only switches the compile path still reads", not gone, ", ".join(gone))
    lost = sorted(n for n in UPSTREAM if n not in key_env or min(live.get((n, r), 0) for r in ("env", "cfg")) < 0)
    arm(A_UPSTREAM, not lost, ", ".join(lost) + ": read with getenv in the key's table, unless a patch took upstream's read out")
    wrong = sorted([n for n in key_env if compiler.get(n, {"env"}) != {"env"}] +
                   [n for n in key_cfg if compiler.get(n, {"cfg"}) != {"cfg"}] +
                   [n for n in key_env & key_cfg])
    arm(A_READER, not wrong, ", ".join(f"{n} (the compiler: {', '.join(sorted(compiler.get(n, [])))})" for n in wrong))

    # --- the digest, built from the patch's own text
    if digest and table and CC:
        names = sorted(key_env) + sorted(key_cfg)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "k.c").write_text(HARNESS_HEAD + table.group(0) + "\n" + digest.group(1) + HARNESS_TAIL)
            try:
                b = subprocess.run(CC + ["-std=c11", "-Wall", "-Werror", "-Wno-unused-function", "-O1", "-o", f"{d}/k", f"{d}/k.c"],
                                   capture_output=True, text=True)
                built, said = b.returncode == 0, b.stderr
            except OSError as e:
                built, said = False, f"{CC[0]}: {e}"
            arm("the patch's digest builds on its own", built, said)
            res = subprocess.run([f"{d}/k"] + names, capture_output=True, text=True).stdout.splitlines() if built else []
        if res:
            none = int(res[0].split()[1])
            env = {l.split()[1]: int(l.split()[2]) for l in res[1:] if l.startswith("env ")}
            cfg = {l.split()[1]: int(l.split()[2]) for l in res[1:] if l.startswith("cfg ")}
            two = {l.split()[1]: int(l.split()[2]) for l in res[1:] if l.startswith("two ")}
            arm(A_ZERO, none == 0, none)
            arm("a switch that is set never gives 0, whatever its value (\"0\" and \"\" included)",
                env and all(v != 0 for v in env.values()), [k for k, v in env.items() if v == 0])
            arm("each switch and each value gives a digest of its own", len(set(env.values())) == len(env) == 5 * len(names),
                (len(env), len(set(env.values())), len(names)))
            spread = [len({(v >> s) & 0xff for v in env.values()}) for s in (0, 8, 16, 24)]
            arm(A_WIDE, min(spread) > 16, f"different values of each byte, low to high: {spread}")
            arm("two switches set together give a digest that is neither one's alone",
                two and all(v != 0 and v not in (env[a + "=1"], env[b + "=1"])
                            for k, v in two.items() for a, b in [k.split("+")]), two)
            arm(A_FILE,
                all((cfg[k] != 0) == (k.split("=")[0] in key_cfg) for k in cfg),
                [k for k in cfg if (cfg[k] != 0) != (k.split("=")[0] in key_cfg)])

    # --- where the digest goes
    arm(A_PLACE,
        "static uint32_t\nnvk_nak_switch_digest(void)" in added
        and "-   const uint64_t compiler_flags = nvk_physical_device_compiler_flags(pdev);\n" in ptext
        and "+   const uint64_t compiler_flags = nvk_physical_device_compiler_flags(pdev)\n"
            "+      ^ ((uint64_t)nvk_nak_switch_digest() << 16);\n"
            "    _mesa_blake3_update(&blake3_ctx, &compiler_flags, sizeof(compiler_flags));" in ptext)
    arm(A_DISK, "+   const uint64_t driver_flags = compiler_flags;" in ptext
        and "-   const uint64_t driver_flags = nvk_physical_device_compiler_flags(pdev);" in ptext)
    arm("the patch asserts that nvk_physical_device_compiler_flags leaves bits 16..47 free",
        "assert((nvk_physical_device_compiler_flags(pdev) & 0x0000ffffffff0000ull) == 0);" in added)
    # nvk_physical_device_compiler_flags as the chain leaves it. The patch's own assert is compiled out of the release
    # build, so this is what keeps a later patch from putting a bit where the digest is.
    shifts, unread = key_function(chain)
    odd = [f"{p}: << {n}" for p, n in shifts if n not in KEY_BITS] + unread
    arm(A_BITS, any(n == 15 for _, n in shifts) and not odd,
        "; ".join(odd) + ": write a bit of the key as `<< N` in that function, N outside 16..47, and add N to KEY_BITS"
        if odd else "the chain's own bit 15 was not found: KEY_FUNCTION no longer names the function's lines")
    arm("the patch changes nvk_physical_device.c only, none of the files of the three upstream-fix patches",
        re.findall(r"^diff --git a/(\S+)", ptext, re.M) == [KEYFILE], ptext[:200])
    # as tools/test_nvk_chain.py has it for the upstream patches: patch(1), the BSD one of macOS above all, takes a
    # header line that looks like a diff, or starts with a digit, for part of the patch
    risky = [l for l in ptext.partition("\ndiff --git ")[0].splitlines()
             if re.match(r"[ \tX]*(--- |\+\+\+ |@@|\*\*\* |Index:|Prereq:|diff |[<>] |\.$|\d)", l)]
    arm("no line of the patch's header can be taken for part of a patch", ptext and not risky, risky)
    at = [i for i, p in enumerate(chain) if p == patch]
    touching = [p.relative_to(root / "nvk").as_posix() for p in (chain[at[0] + 1:] if at else [])
                if p.is_file() and re.search(r"(?m)^\+\+\+ b/" + re.escape(KEYFILE) + r"(\t.*)?$", p.read_text(errors="replace"))]
    arm(A_ORDER, len(at) == 1 and not touching,
        f"{', '.join(touching)} changes that file after it: put {NAME} after it in the chain" if touching else "not in the chain once")
    bld = (root / "build/build_nvk.sh").read_text()
    arm("the 2.9 and 2.8 chains do not have it", sum(NAME in l for l in bld.splitlines() if 'CHAIN="' in l) == 1)
    return out, sorted(set(compiler) & set(EXEMPT))


def broken_tree(tmp, edits=(), extra=None, before=None):
    """A copy of the tree's build/build_nvk.sh and nvk/ under tmp, with text edits to its files, one more patch
    appended to the 3.0 chain, or the patch moved in front of another one of that chain. -> its root, or the text of
    an edit that did not apply."""
    root = Path(tmp)
    shutil.rmtree(root, ignore_errors=True)
    (root / "build").mkdir(parents=True)
    bld = (ROOT / "build/build_nvk.sh").read_text()
    for p in sorted((ROOT / "nvk").rglob("*")):
        dst = root / p.relative_to(ROOT)
        if p.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif p.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(p, dst)
    for rel, old, new in edits:
        f = root / rel
        text = f.read_text()
        if text.count(old) != 1:
            return f"{rel}: {old!r} is there {text.count(old)} times"
        f.unlink()
        f.write_text(text.replace(old, new))
    lines = bld.splitlines(keepends=True)
    at = [i for i, l in enumerate(lines) if l.strip().startswith('CHAIN="nvk-macos') and f" {NAME}" in l]
    m = re.match(r'(\s*CHAIN=")([^"]*)(".*)$', lines[at[0]], re.S) if len(at) == 1 else None
    if not m:
        return "build/build_nvk.sh: the 3.0 chain line with the patch was not found"
    words = m.group(2).split()
    if extra:
        (root / "nvk/nvk-control.patch").write_text(extra)
        words.append("nvk-control")
    if before:
        if before not in words:
            return f"build/build_nvk.sh: {before} is not in the 3.0 chain"
        words.remove(NAME)
        words.insert(words.index(before), NAME)
    lines[at[0]] = m.group(1) + " ".join(words) + m.group(3)
    (root / "build/build_nvk.sh").write_text("".join(lines))
    return root


def later_patch(path, lines, context=("   const uint64_t x = 0;", "   return x;", "}")):
    """The text of a patch that adds lines to one file, after the first line of context."""
    return ("nvk-control: a control of tools/test_nvk_nak_switch_key.py.\n\n"
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -80,{len(context)} +80,{len(context) + len(lines)} @@\n"
            f" {context[0]}\n" + "".join("+" + l + "\n" for l in lines) + "".join(f" {c}\n" for c in context[1:]))


def main():
    fails = 0
    arms, exempt = read(ROOT)
    for name, ok, detail in arms:
        print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-400:]})" if detail and not ok else ""))
        fails += not ok
    for n in exempt:
        print(f"     exempt {n}: {EXEMPT[n]}")
    if not CC:
        if fails:
            print("FAILED")
            return 1
        print("SKIP: the arms that read text pass; the digest arms and the controls need a C compiler (cc, clang or $CC)")
        return 2

    P = f"nvk/{NAME}.patch"
    NAK_NIR, SHADER = "src/nouveau/compiler/nak_nir.c", KEYFN_FILE
    controls = [
        ("a switch the compiler reads is dropped from the table", A_MISSING,
         dict(edits=[(P, '+   "NAK_NO_LICM",\n', "")])),
        ("a switch of upstream's SPIR-V reader is dropped from the table", A_UPSTREAM,
         dict(edits=[(P, '+   "MESA_SPIRV_READ_PATH",\n', "")])),
        ("NVMTL_NVK_NO_LCONST16 is read with getenv, which does not see the settings file", A_READER,
         dict(edits=[(P, 'nvmtl_cfg_getenv("NVMTL_NVK_NO_LCONST16")', 'getenv("NVMTL_NVK_NO_LCONST16")')])),
        ("a later patch reads a new switch in the compiler", A_MISSING,
         dict(extra=later_patch(NAK_NIR, ['   if (getenv("NVMTL_NAK_NEW_THING")) return 0;']))),
        ("a later patch reads a keyed switch through the settings file as well, which the key's getenv does not see", A_READER,
         dict(extra=later_patch(NAK_NIR, ['   if (nvmtl_cfg_getenv("NAK_NO_LICM")) return 0;']))),
        ("a later patch reads a switch in NAK through a variable", A_LITERAL,
         dict(extra=later_patch("src/nouveau/compiler/nak/api.rs", ["    let on = std::env::var_os(name).is_some();"]))),
        ("a later patch reads a new switch through one of Mesa's once-only macros", A_MISSING,
         dict(extra=later_patch(NAK_NIR, ['DEBUG_GET_ONCE_BOOL_OPTION(nak_new, "NVMTL_NAK_NEW_THING", false)']))),
        ("a later patch reads a switch with the name on the next line", A_LITERAL,
         dict(extra=later_patch(NAK_NIR, ["   const char *s = getenv(", '      "NVMTL_NAK_NEW_THING");']))),
        ("a later patch takes bit 16 of the key the way upstream writes its bits", A_BITS,
         dict(extra=later_patch(SHADER, ["      | (instance->drirc.misc.new_thing ? 1 << 16 : 0)"],
                                context=("   return (no_cbufs ? 1 << 12 : 0)", "      | (use_edb_buffer_views ? 1 << 13 : 0)",
                                         "      | (instance->drirc.misc.ssbo_align_4b ? 1 << 14 : 0)")))),
        ("a later patch takes bit 20 in a hunk that shows none of the return's lines", A_BITS,
         dict(extra=later_patch(SHADER, ["   const uint64_t new_bit = pdev->new_thing ? 1ull << 20 : 0;"],
                                context=("   const struct nvk_instance *instance = nvk_physical_device_instance(pdev);",
                                         "   bool no_cbufs = pdev->debug_flags & NVK_DEBUG_NO_CBUF;",
                                         "   bool use_edb_buffer_views = nvk_use_edb_buffer_views(pdev);")))),
        ("a later patch takes bit 20 without writing a shift", A_BITS,
         dict(extra=later_patch(SHADER, ["      | (instance->drirc.misc.new_thing ? BITFIELD64_BIT(20) : 0)"],
                                context=("   return (no_cbufs ? 1 << 12 : 0)", "      | (use_edb_buffer_views ? 1 << 13 : 0)",
                                         "      | (instance->drirc.misc.ssbo_align_4b ? 1 << 14 : 0)")))),
        ("a later patch shifts by 16 somewhere else in nvk_shader.c: not the key, and not taken for it", None,
         dict(extra=later_patch(SHADER, ["   const uint32_t hi = (x | 1) << 16;"]))),
        ("the digest is not 0 with nothing set (a settings-file line for a getenv switch is nothing set too)", (A_ZERO, A_FILE),
         dict(edits=[(P, "+   return !any ? 0 : h != 0 ? h : 1;\n", "+   return !any ? h : h != 0 ? h : 1;\n")])),
        ("the digest is 24 bits wide", A_WIDE,
         dict(edits=[(P, "+   return !any ? 0 : h != 0 ? h : 1;\n", "+   h &= 0x00ffffffu;\n+   return !any ? 0 : h != 0 ? h : 1;\n")])),
        ("the digest goes to bit 8, over the bits the key already has", A_PLACE,
         dict(edits=[(P, "nvk_nak_switch_digest() << 16);", "nvk_nak_switch_digest() << 8);")])),
        ("the disk cache keeps the key without the switches", A_DISK,
         dict(edits=[(P, "+   const uint64_t driver_flags = compiler_flags;",
                      "+   const uint64_t driver_flags = nvk_physical_device_compiler_flags(pdev); //")])),
        ("the patch comes before nvk-nvrm-fixes, which changes the same file", A_ORDER,
         dict(before="nvk-nvrm-fixes")),
    ]
    # a control is judged against what the tree itself fails, so that one real failure is reported once
    real = {name for name, ok, _ in arms if not ok}
    with tempfile.TemporaryDirectory() as tmp:
        for what, expect, how in controls:
            tree = broken_tree(Path(tmp) / "tree", **how)
            if isinstance(tree, str):
                print(f"FAIL control ({what}): the text it breaks is no longer in the tree once ({tree}): update the control")
                fails += 1
                continue
            failed = {name for name, ok, _ in read(tree)[0] if not ok}
            expect = {expect} if isinstance(expect, str) else set(expect or ())
            ok = failed == real | expect
            print(("ok   " if ok else "FAIL ") + f"control ({what}): " + ("its arm fails, and no other" if expect else "every arm passes")
                  + ("" if ok else f" (failed arms beside the tree's own: {sorted(failed - real)})"))
            fails += not ok
    print("FAILED" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
