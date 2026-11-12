#!/usr/bin/env python3
"""Every switch the shipped code reads is in tools/switches.tsv, and the registry keeps its rules.

A switch is an environment variable, a boot argument, a sysctl, a flag file, a branch on a process name or a build
variable that changes what is compiled. Before the registry nothing listed them (481 environment names, 86 boot
arguments, 158 sysctls at 2.9), and a fix could ship behind a new one that nobody would ever set. This fails when the
source reads a name the registry does not have, when a row is read nowhere, when a new switch is an opt-in or a
writable sysctl, when an escape is past its review version, when a ceiling rises, and when docs/SWITCHES.md or
kexts/NVRM/nvrm_switch_table.h is not what the registry generates. Each failure says what to do instead.

Three parts: the rules on synthetic input (switch_registry.py --selftest), the real tree (switch_registry.py check),
and a small made-up tree that is edited the way a contributor would edit the real one. The made-up tree is where the
controls are: every kind of new switch is added to it, hidden in every way the scanner is meant to see through, and
the check has to fail each time with the name and the remedy.
Usage: test_switch_registry.py [driver checkout]"""
import contextlib
import io
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import switch_registry as sr  # noqa: E402

ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE.parent
FAILS = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def quiet(fn, *a):
    """-> (return value, what it printed)"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a)
    return rc, buf.getvalue()


rc, out = quiet(sr.selftest)
arm("the rules fire on synthetic input (switch_registry.py --selftest)", rc == 0, out[-600:])
rc, out = quiet(sr.cmd_check, ROOT)
print(out.rstrip()[-3000:])
arm("the registry and the source agree, and the generated files are current (switch_registry.py check)", rc == 0)
reg = sr.load(ROOT)
arm("the four boot arguments of the README install line are tier install, and nothing else is",
    {n for (k, n), r in reg.rows.items() if r["tier"] == "install" and k == "bootarg"} == {"nvfb", "nvaccel", "nvfbheads", "-nvkmsnosmooth"})
arm("no row added after 2.9 is a development switch, a hardware switch or a writable sysctl",
    not [k for k, r in reg.rows.items() if sr.is_new(r) and (r["tier"] not in sr.NEW_TIERS or r["default"].startswith("writable"))])


def tree(t):
    """A tree with one switch of each kind and nothing wrong with it."""
    for d in ("plugin", "kexts/X", "build", "nvk", "translator/src", "tools"):
        (t / d).mkdir(parents=True)
    (t / "build/build_nvk.sh").write_text('REL=${4:-9.9}\ncase $REL in\n  9.9) FIXES=$NVK/fixes.patch\n       CHAIN="one @FIXES" ;;\n'
                                           '  9.8) FIXES=$NVK/old.patch\n       CHAIN="old" ;;\nesac\nCHAIN="$CHAIN ${NVK_EXTRA_PATCHES:-two}"\n'
                                           'for s in ${NVK_SKIP_PATCHES:-}; do CHAIN="$(printf "%s " $CHAIN | grep -v -- "$s")"; done\n'
                                           'cp "$NVK/extra.h" "$S/src/"\n# ${NVK_IN_A_COMMENT}\n')
    (t / "nvk/fixes.patch").write_text('+++ b/y.c\n+   os_get_option("NVK_FIXES");\n')
    (t / "nvk/two.patch").write_text('+++ b/z.c\n+   getenv("NVK_TWO");\n')
    (t / "nvk/old.patch").write_text('+++ b/o.c\n+   getenv("NVK_OLD_RELEASE");\n')
    (t / "nvk/one.patch").write_text('--- a/x.c\n+++ b/x.c\n+   if (getenv("NVK_ONE")) return;\n\n+   /* getenv("NVK_NOT") */\n-   getenv("NVK_GONE");\n')   # with an empty line, as real patches have
    (t / "nvk/extra.h").write_text('static inline int e(void) { return getenv("NVK_COPIED") != 0; }\n')
    (t / "plugin/a.m").write_text('static int f(void) { return nvmtl_env_uint(@"NVMTL_WRAPPED") + (getenv("NVMTL_PLAIN") != 0); }\n'
                                  '// getenv("NVMTL_COMMENT")\nstatic void g(void) { setenv("NVMTL_SETONLY", "1", 0); }\n'
                                  'static int ours(const char *k) { return !strncmp(k, "NVMTL_", 6) || !strncmp(k, "NVK_", 4); }\n')
    (t / "translator/src/a.rs").write_text('fn f() -> bool { std::env::var("METAL2VULKAN_ONE").is_ok() }\n')
    (t / "kexts/X/x.cpp").write_text(
        'SYSCTL_INT(_debug, OID_AUTO, nvx_knob, CTLFLAG_RW | CTLFLAG_LOCKED, &g, 0, "a knob");\n'
        'SYSCTL_PROC(_debug, OID_AUTO, nvx_blob, CTLTYPE_OPAQUE | CTLFLAG_WR, 0, 0, h, "-", "a blob");\n'
        'SYSCTL_QUAD(_debug, OID_AUTO, nvx_count, CTLFLAG_RD, &c, "a " "counter");\n'
        '#define NVX_CNT(nm, var, desc) SYSCTL_QUAD(_debug, OID_AUTO, nm, CTLFLAG_RD | CTLFLAG_LOCKED, (SInt64 *)&var, desc)\n'
        'NVX_CNT(nvx_macro_count, gA, "a counter declared through a macro");\n'
        'static bool a(void) { int v; return PE_parse_boot_argn("nvxarg", &v, sizeof v); }\n'
        'static bool flag(const char *name, int *cache) { int v = 0; return PE_parse_boot_argn(name, &v, sizeof v) && v; }\n'
        'static bool b(void) { static int c = -1; LOG("flag is a word in a log line"); return flag("nvxwrapped", &c); }\n')
    return t


def check(t):
    rc, out = quiet(sr.cmd_check, t)
    return rc, out


def edit(t, rel, text):
    p = t / rel
    old = p.read_text()
    p.write_text(old + text)
    return lambda: p.write_text(old)


with tempfile.TemporaryDirectory(prefix="nmsw-") as tmp:
    t = tree(pathlib.Path(tmp))
    found, problems = sr.scan(t)
    env, sysc = found["env"], found["sysctl"]
    arm("the made-up tree has nothing the scanner objects to", problems == [], str(problems))
    arm("a plain getenv, a name passed through a wrapper, a Rust env::var and a name an NVK patch adds are all found",
        {"NVMTL_PLAIN", "NVMTL_WRAPPED", "METAL2VULKAN_ONE", "NVK_ONE"} <= set(env), str(sorted(env)))
    arm("the NVK scan follows the default release's chain, its fixes patch, the default extra patches and the headers copied into the tree, "
        "and no other release's chain", {"NVK_FIXES", "NVK_TWO", "NVK_COPIED"} <= set(env) and "NVK_OLD_RELEASE" not in env, str(sorted(env)))
    arm("a name in a comment and a name a patch only removes are not switches",
        not ({"NVMTL_COMMENT", "NVK_NOT", "NVK_GONE"} & set(env)), str(sorted(env)))
    arm("a variable the driver only sets is marked so (it is not a user switch)", env["NVMTL_SETONLY"]["set_only"] and not env["NVMTL_PLAIN"]["set_only"])
    arm("read-write and write-only sysctls are both writable, a counter is not",
        sysc["debug.nvx_knob"]["writable"] and sysc["debug.nvx_blob"]["writable"] and not sysc["debug.nvx_count"]["writable"])
    arm("a sysctl declared through a macro is found with its name, its flags and its description",
        "debug.nvx_macro_count" in sysc and not sysc["debug.nvx_macro_count"]["writable"]
        and sysc["debug.nvx_macro_count"]["desc"] == "a counter declared through a macro" and sysc["debug.nvx_count"]["desc"] == "a counter", str(sorted(sysc)))
    arm("a boot argument is found by its literal name, also through a function that passes its parameter on",
        {"nvxarg", "nvxwrapped"} == set(found["bootarg"]), str(sorted(found["bootarg"])))
    arm("the variables of a build script that carry a driver prefix are found; one in a comment is not",
        set(found["build"]) == {"NVK_EXTRA_PATCHES", "NVK_SKIP_PATCHES"}, str(sorted(found["build"])))

    # the registry of the made-up tree closes, and then every kind of new switch is caught with the remedy named
    (t / sr.REG).write_text(sr.bootstrap(t))
    quiet(sr.cmd_gen, t)
    rc, out = check(t)
    arm("a first registry of the made-up tree passes its own check", rc == 0, out[-800:])
    for name, rel, text, want in (
        ("a new getenv in the plugin", "plugin/a.m", 'static int n(void) { return getenv("NVMTL_ZZ_NEW") != 0; }\n', ("env NVMTL_ZZ_NEW is read at plugin/a.m:5", "tier escape", "opt-in")),
        ("a new name read through a wrapper nobody told the scanner about", "plugin/a.m", 'static int n(void) { return my_reader(@"NVMTL_ZZ_VIA"); }\n', ("env NVMTL_ZZ_VIA", "ships ON")),
        ("a new name in an NVK patch of the chain", "nvk/one.patch", '+   if (nvmtl_cfg_getenv("NVK_ZZ_NEW")) return;\n', ("env NVK_ZZ_NEW is read at nvk/one.patch", "tier escape")),
        ("a name put together at run time", "plugin/a.m", 'static int n(const char *s) { char b[64]; snprintf(b, sizeof b, "NVMTL_%s", s); return getenv(b) != 0; }\n',
         ("put together at run time", "one string literal")),
        ("a new boot argument", "kexts/X/x.cpp", 'static bool n(void) { int v; return PE_parse_boot_argn("nvzznew", &v, sizeof v); }\n', ("bootarg nvzznew is read at kexts/X/x.cpp:9", "tier escape")),
        ("a new boot argument read through the existing wrapper", "kexts/X/x.cpp", 'static bool n(void) { static int c = -1; return flag("nvzzflag", &c); }\n', ("bootarg nvzzflag", "tier escape")),
        ("a boot argument whose name is not a literal", "kexts/X/x.cpp", 'static bool n(void) { int v; const char *k = pick(); return PE_parse_boot_argn(k, &v, sizeof v); }\n',
         ("not a literal (k)", "string literal")),
        ("a wrapper called with a name that is not a literal", "kexts/X/x.cpp", 'static bool n(void) { static int c = -1; return flag(pick(), &c); }\n', ("flag() reads a boot argument", "string literal")),
        ("a wrapper handed on as a function pointer", "kexts/X/x.cpp", 'static bool n(void) { return each_name(flag); }\n', ("used here without a call",)),
        ("the whole boot-args string", "kexts/X/x.cpp", 'static bool n(void) { return strstr(PE_boot_args(), "nvzz") != 0; }\n', ("PE_boot_args reads the boot arguments", "macOS 15")),
        ("a new writable sysctl", "kexts/X/x.cpp", 'SYSCTL_INT(_debug, OID_AUTO, nvx_new_knob, CTLFLAG_RW | CTLFLAG_LOCKED, &g, 0, "a new knob");\n',
         ("sysctl debug.nvx_new_knob is declared writable", "a new writable sysctl is refused", "CTLFLAG_RD", "boot argument")),
        ("a new writable sysctl declared through a macro", "kexts/X/x.cpp",
         '#define NVX_KNOB(nm, var) SYSCTL_INT(_debug, OID_AUTO, nm, CTLFLAG_RW, &var, 0, "k")\nNVX_KNOB(nvx_macro_knob, gB);\n',
         ("sysctl debug.nvx_macro_knob is declared writable",)),
        ("a sysctl whose access flags hide behind a name", "kexts/X/x.cpp", 'SYSCTL_INT(_debug, OID_AUTO, nvx_hidden, NVX_FLAGS, &g, 0, "hidden");\n',
         ("access flags are not spelled out (NVX_FLAGS)",)),
        ("a sysctl any user may write", "kexts/X/x.cpp", 'SYSCTL_INT(_debug, OID_AUTO, nvx_any, CTLFLAG_RW | CTLFLAG_ANYBODY, &g, 0, "any");\n', ("CTLFLAG_ANYBODY",)),
        ("a new read-only counter", "kexts/X/x.cpp", 'SYSCTL_QUAD(_debug, OID_AUTO, nvx_new_count, CTLFLAG_RD, &c, "a new counter");\n',
         ("sysctl debug.nvx_new_count is declared at kexts/X/x.cpp:9", "switch_registry.py sync")),
        ("a new variable in a build script", "build/build_nvk.sh", 'for s in ${NVK_ZZ_PATCHES:-}; do :; done\n', ("build NVK_ZZ_PATCHES is read at build/build_nvk.sh",)),
        ("an edit that moves a switch into another file", "translator/src/a.rs", 'fn g() -> bool { std::env::var("NVMTL_PLAIN").is_ok() }\n', ("docs/SWITCHES.md is not what",)),
    ):
        undo = edit(t, rel, text)
        rc, out = check(t)
        arm(f"control: {name} fails the check, and the message names it and the remedy", rc == 1 and all(w in out for w in want),
            f"wanted {want}; got: {out[-700:]}")
        undo()
    # the spellings a name can hide behind besides the ones above: each is either listed (and then refused as a new
    # switch) or refused as unreadable, never passed over
    for name, edits, want in (
        ("a foreign name read through a local variable given a literal", [("plugin/a.m", 'static int n(void) { const char *k = "ZZ_LOCAL"; return getenv(k) != 0; }\n')],
         ("env ZZ_LOCAL is read at plugin/a.m:5",)),
        ("getenv of a name computed at run time", [("plugin/a.m", 'static int n(void) { return getenv(pick()) != 0; }\n')], ("not a string literal (pick())",)),
        ("a foreign name passed to a reader of its own parameter", [("plugin/a.m", 'static int rd(const char *k) { return getenv(k) != 0; }\nstatic int n(void) { return rd("ZZ_VIA_PARAM"); }\n')],
         ("env ZZ_VIA_PARAM is read at plugin/a.m:6",)),
        ("getenv through a macro", [("plugin/a.m", '#define ZZ_GET(n) getenv(n)\nstatic int n(void) { return ZZ_GET("ZZ_MACRO") != 0; }\n')], ("not a string literal (n)",)),
        ("a driver name glued in a macro", [("plugin/a.m", '#define ZZ_GET(n) getenv("NVMTL_" n)\n')], ("put together at run time",)),
        ("the process environment read as a dictionary", [("plugin/a.m", 'static id n(void) { return [[NSProcessInfo processInfo] environment][@"ZZ_OBJC"]; }\n')],
         ("env ZZ_OBJC is read at plugin/a.m:5",)),
        ("a boot flag compared next to kern.bootargs in user space", [("plugin/a.m", 'static int n(void) { char b[512]; size_t l = sizeof b; sysctlbyname("kern.bootargs", b, &l, 0, 0); return strstr(b, "-nvzzflag") != 0; }\n')],
         ("bootarg -nvzzflag is read at plugin/a.m:5",)),
        ("a Rust walk of the whole environment", [("translator/src/a.rs", 'fn g() { for (k, v) in std::env::vars() { let _ = (k, v); } }\n')], ("walks the whole environment",)),
        ("a Rust read by a name that is not a literal", [("translator/src/a.rs", 'fn g(n: &str) -> bool { std::env::var(n).is_ok() }\n')], ("env::var with a name that is not a string literal",)),
        ("a Rust name put together with format!", [("translator/src/a.rs", 'fn g(s: &str) -> bool { std::env::var(format!("METAL2VULKAN_{}", s)).is_ok() }\n')], ("put together at run time",)),
        ("a tool path override for a new tool", [("translator/src/a.rs", 'pub fn tool_path_override(cmd: &str) -> Option<String> { std::env::var(format!("METAL2VULKAN_{}", cmd)).ok() }\n'
                                                   'fn h() { let _ = run("zz-tool", &[]); }\n')], ("env METAL2VULKAN_ZZ_TOOL is read at translator/src/a.rs:3",)),
        ("the boot line read whole in a kext", [("kexts/X/x.cpp", 'static void n(void) { char b[256]; size_t l = sizeof b; sysctlbyname("kern.bootargs", b, &l, 0, 0); }\n')],
         ("the boot line read as one string",)),
        ("a boot argument whose name begins with '_'", [("kexts/X/x.cpp", 'static bool n(void) { int v; return PE_parse_boot_argn("_nvzz", &v, sizeof v); }\n')], ("begins with '_'",)),
        ("a sysctl registered through a pointer", [("kexts/X/x.cpp", 'static void n(struct sysctl_oid *o) { sysctl_register_oid(o); }\n')], ("a sysctl is registered through o",)),
        ("a sysctl declared through SYSCTL_##kind", [("kexts/X/x.cpp", '#define NVX_ANY(kind, nm) SYSCTL_##kind(_debug, OID_AUTO, nm, CTLFLAG_RW, &g, 0, "x")\nNVX_ANY(INT, nvx_kind_knob);\n'
                                                       'static void r(void) { sysctl_register_oid(&sysctl__debug_nvx_kind_knob); }\n')],
         ("sysctl__debug_nvx_kind_knob is used here and the registry read no declaration",)),
        ("a sysctl whose flags are numbers", [("kexts/X/x.cpp", 'SYSCTL_INT(_debug, OID_AUTO, nvx_num, 0x40000000 | 0x80000000, &g, 0, "n");\n')], ("not spelled out (0x40000000",)),
        ("a sysctl whose flags name no access", [("kexts/X/x.cpp", 'SYSCTL_INT(_debug, OID_AUTO, nvx_noacc, CTLFLAG_LOCKED, &g, 0, "n");\n')], ("name neither CTLFLAG_RD",)),
        ("a sysctl macro defined through another macro", [("kexts/X/x.cpp", '#define NVX_A(nm, v) SYSCTL_INT(_debug, OID_AUTO, nm, CTLFLAG_RW, &v, 0, "a")\n#define NVX_B(nm) NVX_A(nm, gB)\nNVX_B(nvx_twolevel);\n')],
         ("the registry reads one level of macro",)),
        ("a writable sysctl declared through a macro of a header", [("kexts/X/m.h", '#define NVX_HK(nm, v) SYSCTL_INT(_debug, OID_AUTO, nm, CTLFLAG_RW, &v, 0, "h")\n'),
                                                                     ("kexts/X/x.cpp", 'NVX_HK(nvx_hdr_knob, gC);\n')], ("sysctl debug.nvx_hdr_knob is declared writable",)),
        ("one sysctl macro name defined two ways", [("kexts/X/m.h", '#define NVX_HK(nm, v) SYSCTL_INT(_debug, OID_AUTO, nm, CTLFLAG_RD, &v, 0, "h")\n'),
                                                    ("kexts/X/n.h", '#define NVX_HK(nm, v) SYSCTL_INT(_debug, OID_AUTO, nm, CTLFLAG_RW, &v, 0, "h")\n'),
                                                    ("kexts/X/x.cpp", 'NVX_HK(nvx_hdr_knob, gC);\n')], ("two files define it differently",)),
        ("a new variable in a kext's own build script", [("kexts/X/build.sh", 'cc ${NVRM_ZZ_OPT:+-DZZ} x.cpp\n')], ("build NVRM_ZZ_OPT is read at kexts/X/build.sh:1",)),
        ("a renamed switch", [("plugin/a.m", None)], ("env NVMTL_PLAIN2 is read at plugin/a.m:1", "env NVMTL_PLAIN is read nowhere")),
        ("a deleted boot argument", [("kexts/X/x.cpp", None)], ("bootarg nvxarg is read nowhere",)),
    ):
        undos = []
        for rel, text in edits:
            p = t / rel
            if text is None:                                        # the two edits that change a line instead of adding one
                old = p.read_text()
                p.write_text(old.replace('getenv("NVMTL_PLAIN")', 'getenv("NVMTL_PLAIN2")') if rel.endswith(".m")
                             else old.replace('static bool a(void) { int v; return PE_parse_boot_argn("nvxarg", &v, sizeof v); }\n', ''))
                undos.append(lambda p=p, old=old: p.write_text(old))
            elif p.exists():
                undos.append(edit(t, rel, text))
            else:
                p.write_text(text); undos.append(p.unlink)
        rc, out = check(t)
        arm(f"control: {name} fails the check, and the message names it", rc == 1 and all(w in out for w in want), f"wanted {want}; got: {out[-700:]}")
        for u in reversed(undos):
            u()
    (t / "kexts/X/new.c").write_text('SYSCTL_PROC(_debug, OID_AUTO, nvx_new_blob, CTLTYPE_OPAQUE | CTLFLAG_WR, 0, 0, h, "-", "blob");\n')
    rc, out = check(t)
    arm("control: a write-only sysctl in a new file under kexts/ fails the check (the files are found by walking the tree, not from a list)",
        rc == 1 and "sysctl debug.nvx_new_blob is declared writable at kexts/X/new.c:1" in out, out[-600:])
    (t / "kexts/X/new.c").unlink()
    rc, out = check(t)
    arm("with each edit taken back the made-up tree passes again", rc == 0, out[-600:])
    undo = edit(t, "plugin/a.m", 'static int same(const char *k) { return !strncmp(k, "NVRM_", 5); }\n')
    rc, out = check(t)
    arm("a prefix used to recognise names (strncmp) is not a name put together at run time", rc == 0, out[-600:])
    undo()

    # a row written ahead of its code, and the one command that closes the registry after the code is merged
    row = "bootarg\tnvxlater\tescape\ton (nvxlater=0 restores the old path)\t3.0\t3.2\tA change that is merged later and is on by default.\n"
    (t / sr.REG).write_text((t / sr.REG).read_text() + row)
    quiet(sr.cmd_gen, t)
    rc, out = check(t)
    arm("control: a row with no code fails the check", rc == 1 and "bootarg nvxlater is read nowhere" in out, out[-600:])
    reg = sr.load(t); reg.pending.add(("bootarg", "nvxlater")); (t / sr.REG).write_text(reg.text())
    rc, out = check(t)
    arm("the same row marked #pending passes: it was written before its code", rc == 0, out[-600:])
    soon = "bootarg\tnvxsoon\tescape\ton (nvxsoon=0 restores the old path)\t3.0\t3.2\tA change that is merged first.\n"
    reg = sr.load(t); reg_text = (t / sr.REG).read_text()
    (t / sr.REG).write_text(reg_text + soon); reg = sr.load(t); reg.pending.add(("bootarg", "nvxsoon")); (t / sr.REG).write_text(reg.text())
    undo = edit(t, "kexts/X/x.cpp", 'static bool soon(void) { int v = 1; PE_parse_boot_argn("nvxsoon", &v, sizeof v); return v != 0; }\n')
    rc, out = quiet(sr.cmd_sync, t, False)
    reg = sr.load(t)
    arm("sync after a merge takes the mark off a row whose code arrived and leaves the marks of rows still waiting",
        rc == 0 and ("bootarg", "nvxsoon") not in reg.pending and ("bootarg", "nvxlater") in reg.pending
        and "removed the #pending mark of bootarg nvxsoon" in out and check(t)[0] == 0, out[-600:])
    undo(); (t / sr.REG).write_text(reg_text); quiet(sr.cmd_gen, t)
    rc, out = quiet(sr.cmd_sync, t, True)
    arm("finalize removes the mark, so the row with no code is an error again, and says so",
        rc == 1 and "#pending" not in (t / sr.REG).read_text() and "bootarg nvxlater is read nowhere" in out and "removed 1 #pending mark" in out, out[-900:])
    edit(t, "kexts/X/x.cpp", 'static bool later(void) { int v = 1; PE_parse_boot_argn("nvxlater", &v, sizeof v); return v != 0; }\n'
                             'SYSCTL_QUAD(_debug, OID_AUTO, nvx_later_count, CTLFLAG_RD, &c, "counter of the later change");\n')
    rc, out = check(t)
    arm("the code arrives with a counter of its own: the check asks for the one command", rc == 1 and "switch_registry.py sync" in out, out[-600:])
    rc, out = quiet(sr.cmd_sync, t, True)
    reg = sr.load(t)
    arm("finalize after the merge: the counter gets its row, the generated files are current, the check passes",
        rc == 0 and reg.rows.get(("sysctl", "debug.nvx_later_count"), {}).get("purpose") == "counter of the later change"
        and reg.rows[("sysctl", "debug.nvx_later_count")]["since"] == sr.NEXT and '"nvxlater", NVRM_SW_ESCAPE' in (t / sr.GEN_H).read_text(), out[-900:])
    rc2, out2 = quiet(sr.cmd_sync, t, True)
    arm("finalize a second time changes nothing", rc2 == 0 and f"{sr.REG}: 0 change(s)" in out2, out2[-400:])
    (t / "CHANGELOG.md").write_text("# Changelog\n\n## 3.2\n")
    rc, out = check(t)
    arm("control: when the tree reaches the review version of an escape the check fails until it is promoted or renewed",
        rc == 1 and "nvxlater was due for review at 3.2 and the tree is 3.2" in out and "promote it" in out, out[-600:])

# a row dated 2.9 names a switch the 2.9 release read (needs the release in the clone; a clone without tags says so and
# passes). The count of such rows cannot rise, but a count is kept by deleting one old row for every new one filed as
# old; the release's own tree cannot be argued with.
base = sr.release_switches(ROOT)

# a ceiling may not rise from one commit to the next (needs history; a shallow clone says so and passes). One exception:
# the count of rows dated 2.9 rises when the scanner learns to read a spelling it missed, and then each added row is a
# switch the release itself read. That is shown only by the release's tree, so without tag v2.9 the rise is refused.
old = subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD~1:{sr.REG}"], capture_output=True, text=True)
if old.returncode == 0:
    rises = sr.ceiling_rises(old.stdout, (ROOT / sr.REG).read_text())
    proven = base is not None and not sr.misdated(sr.load(ROOT), base)
    excused = [x for x in rises if x.startswith("ceiling base all ") and proven]
    for x in excused:
        print(f"note: {x}; every row dated {sr.BASE} is a switch the {sr.BASE} release read, so the scanner found more of that release")
    rises = [x for x in rises if x not in excused]
    arm("no ceiling is higher than in the previous commit", not rises, "; ".join(rises))
else:
    print("skip the ceiling comparison: the previous commit has no registry (the commit that adds it, or a shallow clone)")

if base is None:
    print(f"skip the comparison with the {sr.BASE} release: tag v{sr.BASE} is not in this clone")
else:
    reg = sr.load(ROOT)
    wrong = sr.misdated(reg, base)
    arm(f"every row dated {sr.BASE} is a switch the {sr.BASE} release read", len(base) > 500 and not wrong, "; ".join(wrong[:5]))
    reg.rows[("env", "NVMTL_FILED_AS_OLD")] = dict(tier="dev", default="off", since=sr.BASE, review="-", purpose="x", line=1)
    reg.rows[("sysctl", "debug.nvrm_filed_as_old")] = dict(tier="setting", default="writable by root", since=sr.BASE, review="-", purpose="x", line=2)
    wrong = sr.misdated(reg, base)
    arm("control: a new opt-in and a new writable sysctl filed as old are each reported, with what to do",
        len(wrong) == 2 and "env NVMTL_FILED_AS_OLD is dated 2.9" in wrong[0] and "sysctl debug.nvrm_filed_as_old" in wrong[1]
        and f"Set its since to {sr.NEXT}" in wrong[0], "; ".join(wrong))
print(f"{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
