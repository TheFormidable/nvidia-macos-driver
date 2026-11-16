#!/usr/bin/env python3
"""The native Vulkan presentation runtime (tools/nvk-bridge): what it needs from this tree, and that the build, the
package and the installer carry it.

- tools/nvk-bridge/build.sh runs here with stand-ins for Apple's tools (xcrun, codesign, install_name_tool, lipo,
  vtool) first on PATH: the outputs it writes, the manifest's Vulkan version taken from the headers, the frontend's
  SHA-256 in the properties file, the compile flags, and no folder of the build machine in what it writes.
- bridge.mm against the driver: the import record is NVK's struct nvmtl_nvk_share at its version, the entry points it
  looks up are exported by the NVK patch chain with the signatures it calls, the generation tag is the plugin's, the
  service it matches is the accelerator's class; it reads no environment variable; and the renderer's own
  window-system extensions (nvk-metal-wsi.patch) are filtered.
- build.sh builds it after nvk as part of all; package_local.sh puts its four files under
  Library/GPUBundles/nvmtl/minecraft and its two guides at the package's top; install.sh and uninstall.sh back up,
  replace, restore and remove Library/GPUBundles/nvmtl as a whole folder, so the runtime inside it needs no line of its
  own.

Every check of a text has a control: the same check on a changed copy must fail.
On a Mac with Vulkan headers (NM_VULKAN_INCLUDE) it also builds host_test.mm with AddressSanitizer and
UndefinedBehaviorSanitizer and runs it. On a Mac without them it runs everything else and then reports itself skipped
(exit 2, as the other tests that read NM_VULKAN_INCLUDE do), so that a run that should have built host_test.mm and did
not shows as a skip; a NM_VULKAN_INCLUDE that holds no vulkan/vulkan.h fails.
usage: test_nvk_bridge.py"""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NB = ROOT / "tools" / "nvk-bridge"
BRIDGE = (NB / "bridge.mm").read_text()
PATCH = (ROOT / "nvk" / "nvk-macos.patch").read_text()
fails = 0


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


def control(what, problems):
    check("control: " + what, bool(problems), "the check found nothing wrong in a copy that is wrong")


# ---------------------------------------------------------------------------------------------- the build script
FAKE = r'''#!/bin/bash
# stand-in written by tools/test_nvk_bridge.py
me=${0##*/}; printf '%s' "$me" >> "$NBT_LOG"; printf ' %s' "$@" >> "$NBT_LOG"; printf '\n' >> "$NBT_LOG"
case "$me" in
  xcrun) shift; out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done
         [ -n "$out" ] && printf 'frontend built by the stand-in\n' > "$out" ;;
  vtool) echo "    minos ${NBT_MINOS:-15.0}" ;;
esac
exit 0
'''
HEADER = "#define VK_HEADER_VERSION 363\n#define VK_HEADER_VERSION_COMPLETE VK_MAKE_API_VERSION(0, 1, 4, VK_HEADER_VERSION)\n"


def run_build(t, header=HEADER, minos="15.0"):
    fake = t / "bin"; fake.mkdir(exist_ok=True)
    for tool in ("xcrun", "codesign", "install_name_tool", "lipo", "vtool"):
        (fake / tool).write_text(FAKE); (fake / tool).chmod(0o755)
    inc = t / "include" / "vulkan"; inc.mkdir(parents=True, exist_ok=True)
    (inc / "vulkan_core.h").write_text(header)
    nvk = t / "libvulkan_nouveau.dylib"; nvk.write_bytes(b"nvk built by the test\n")
    out = t / "out"; log = t / "calls.log"
    if out.exists():
        shutil.rmtree(out)
    if log.exists():
        log.unlink()
    env = {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(t), "TMPDIR": str(t), "LANG": "C", "NBT_LOG": str(log),
           "NBT_MINOS": minos}
    r = subprocess.run(["bash", str(NB / "build.sh"), str(nvk), str(t / "include"), str(out)], capture_output=True,
                       text=True, env=env, timeout=60)
    return r.returncode, r.stdout + r.stderr, out, (log.read_text() if log.exists() else "")


def build_problems(rc, text, out, calls, t):
    p = []
    if rc != 0:
        return [f"exit {rc}: {text[-400:]}"]
    want = ["lib/libnvk_minecraft.dylib", "lib/libnvk_renderer.dylib", "nvk_bridge_icd.json", "nvk-bridge-runtime.properties"]
    have = sorted(str(f.relative_to(out)) for f in out.rglob("*") if f.is_file())
    if have != sorted(want):
        p.append(f"outputs {have}, not {sorted(want)}")
        return p
    if (out / "lib/libnvk_renderer.dylib").read_bytes() != b"nvk built by the test\n":
        p.append("the renderer is not a copy of the NVK library")
    try:
        icd = json.loads((out / "nvk_bridge_icd.json").read_text())
    except ValueError as e:
        return p + [f"nvk_bridge_icd.json is not JSON: {e}"]
    if icd != {"file_format_version": "1.0.0", "ICD": {"library_path": "./lib/libnvk_minecraft.dylib", "api_version": "1.4.363"}}:
        p.append(f"nvk_bridge_icd.json is {icd}")
    props = dict(l.split("=", 1) for l in (out / "nvk-bridge-runtime.properties").read_text().splitlines())
    sha = hashlib.sha256((out / "lib/libnvk_minecraft.dylib").read_bytes()).hexdigest()
    if props.get("library.sha256") != sha or props.get("runtime.id") != "nvk-macos-gpu-copy-" + sha[:12]:
        p.append(f"the properties do not carry the frontend's SHA-256: {props}")
    if props.get("library.path") != "lib/libnvk_minecraft.dylib" or props.get("presentation") != "gpu-copy":
        p.append(f"properties {props}")
    for f in ("nvk_bridge_icd.json", "nvk-bridge-runtime.properties"):
        if str(t) in (out / f).read_text():
            p.append(f"{f} names a folder of the build machine")
    cc = [l for l in calls.splitlines() if l.startswith("xcrun clang++ ")]
    if len(cc) != 1:
        p.append(f"{len(cc)} compiles of the frontend")
    else:
        for flag in ("-arch x86_64", "-mmacosx-version-min=15.0", "-dynamiclib", "-Werror", "-fobjc-arc",
                     "-Wl,-install_name,@loader_path/libnvk_minecraft.dylib", str(NB / "bridge.mm")):
            if flag not in cc[0]:
                p.append(f"the compile lacks {flag}")
    if f"install_name_tool -id @loader_path/libnvk_renderer.dylib {out}/lib/libnvk_renderer.dylib" not in calls:
        p.append("the renderer does not get its own install name")
    for b in ("libnvk_minecraft.dylib", "libnvk_renderer.dylib"):
        if f"codesign -f -s - {out}/lib/{b}" not in calls or f"codesign --verify --strict {out}/lib/{b}" not in calls:
            p.append(f"{b} is not signed and verified")
    return p


t = Path(tempfile.mkdtemp(prefix="nvkbridge-"))
try:
    rc, text, out, calls = run_build(t)
    p = build_problems(rc, text, out, calls, t)
    check("tools/nvk-bridge/build.sh writes the four files, signs both libraries, takes the Vulkan version from NVK's "
          "headers and the frontend's SHA-256 into its properties, and names no folder of the build machine", not p, p)
    rc2, text2, *_ = run_build(t, header="#define VK_HEADER_VERSION_COMPLETE VK_MAKE_VERSION(1, 4, 363)\n")
    check("headers without the version lines it reads stop the build", rc2 != 0 and "STOP" in text2, text2[-300:])
    rc3, text3, *_ = run_build(t, minos="26.0")
    check("a frontend built for another macOS than 15.0 stops the build", rc3 != 0 and "minos" in text3, text3[-300:])
    rc4, text4, out4, calls4 = run_build(t, header=HEADER.replace("363", "999"))
    control("a manifest whose version differs from 1.4.363", build_problems(rc4, text4, out4, calls4, t))
    rc5, text5, out5, calls5 = run_build(t)
    (out5 / "nvk-bridge-runtime.properties").write_text((out5 / "nvk-bridge-runtime.properties").read_text()
                                                       .replace("library.sha256=", "library.sha256=0"))
    control("properties that do not carry the frontend's SHA-256", build_problems(rc5, text5, out5, calls5, t))
finally:
    shutil.rmtree(t, ignore_errors=True)


# ------------------------------------------------------------------------------------------- bridge.mm and the driver
def interface_problems(bridge, patch, plugin, plist):
    p = []
    share = re.search(r"\+#define NVMTL_NVK_SHARE_VERSION (\d+)\s*\n\+struct nvmtl_nvk_share \{(.*?)\};", patch, re.S)
    if not share:
        return ["nvk-macos.patch has no struct nvmtl_nvk_share after NVMTL_NVK_SHARE_VERSION"]
    fields = re.findall(r"\+\s*(uint\d+_t)\s+(\w+);", share.group(2))
    if fields != [("uint32_t", "version"), ("uint32_t", "surfaceID"), ("uint32_t", "plane"), ("uint32_t", "pad"),
                  ("uint64_t", "size_B")]:
        p.append(f"struct nvmtl_nvk_share is {fields}, not the record bridge.mm writes")
    if "struct Share { uint32_t version, surface, plane, pad; uint64_t bytes; };" not in bridge:
        p.append("bridge.mm's Share is not {version, surface, plane, pad, bytes}")
    if f"Share share{{{share.group(1)},IOSurfaceGetID(" not in bridge:
        p.append(f"bridge.mm does not write version {share.group(1)} into its import record")
    if not re.search(r'visibility\("default"\)\)\) void\s+/\*[^*]*\*/\s*\n\+nvmtl_nvk_stage_import\(const struct nvmtl_nvk_share \*in\)', patch):
        p.append("nvk-macos.patch does not export void nvmtl_nvk_stage_import(const struct nvmtl_nvk_share *)")
    if not re.search(r'visibility\("default"\)\)\) uint32_t\s+/\*[^*]*\*/\s*\n\+nvmtl_nvk_surface_dirty\(VkDevice device, uint32_t surfaceID, uint32_t plane\)', patch):
        p.append("nvk-macos.patch does not export uint32_t nvmtl_nvk_surface_dirty(VkDevice, uint32_t, uint32_t)")
    if "using SurfaceDirty = uint32_t (*)(VkDevice, uint32_t, uint32_t);" not in bridge or \
       "using StageImport = void (*)(const Share *);" not in bridge:
        p.append("bridge.mm calls the two entry points with other signatures")
    for sym in ("vk_icdGetInstanceProcAddr", "nvmtl_nvk_stage_import", "nvmtl_nvk_surface_dirty"):
        if f'dlsym(backend, "{sym}")' not in bridge:
            p.append(f"bridge.mm does not look up {sym}")
    if "surface_dirty(d->raw,IOSurfaceGetID(frame->surface),0)" not in bridge:
        p.append("bridge.mm does not mark plane 0 without flags (the blocking mark)")
    if '"NVMTLVramAt:p%lu"' not in plugin or "0x564d5634" not in plugin:
        p.append("the plugin's generation tag is not NVMTLVramAt:p<plane> with magic 0x564d5634")
    if 'CFSTR("NVMTLVramAt:p0")' not in bridge or "0x564d5634u" not in bridge:
        p.append("bridge.mm does not write the plugin's tag")
    if bridge.index("surface_dirty(d->raw") > bridge.index("publish_tag(frame->surface);"):
        p.append("bridge.mm writes the tag before the mark (the tag must carry the seed the mark left)")
    if "<key>IOClass</key><string>NVAccel</string>" not in plist or 'IOServiceMatching("NVAccel")' not in bridge:
        p.append("bridge.mm does not match the accelerator's class NVAccel")
    return p


plugin = (ROOT / "plugin" / "NVMTLDevice.m").read_text()
plist = (ROOT / "kexts" / "NVRM" / "accel" / "Info.plist").read_text()
p = interface_problems(BRIDGE, PATCH, plugin, plist)
check("bridge.mm writes NVK's import record v3, calls NVK's exported stage_import and surface_dirty with their "
      "signatures, marks before it tags with the plugin's tag, and matches the NVAccel service", not p, p)
control("a record with another version",
        interface_problems(BRIDGE.replace("Share share{3,", "Share share{2,"), PATCH, plugin, plist))
control("an NVK record with one more field",
        interface_problems(BRIDGE, PATCH.replace("+   uint32_t pad;", "+   uint32_t pad;\n+   uint32_t extra;", 1), plugin, plist))
control("a surface_dirty with another signature", interface_problems(
    BRIDGE, PATCH.replace("nvmtl_nvk_surface_dirty(VkDevice device, uint32_t surfaceID, uint32_t plane)",
                          "nvmtl_nvk_surface_dirty(VkDevice device, uint32_t surfaceID)", 1), plugin, plist))

def reads_problems(bridge):
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", bridge, flags=re.S)
    reads = re.findall(r"\b(getenv|secure_getenv|environ|NSProcessInfo|fopen|O_CREAT|O_WRONLY|O_RDWR)\b", code)
    if len(re.findall(r"(?<!\w)open\(", code)) != 1 or 'open("/dev/null",O_RDONLY|O_CLOEXEC)' not in code:
        reads.append("an open( other than the one of /dev/null, read-only")
    return reads


p = reads_problems(BRIDGE)
check("bridge.mm reads no environment variable and opens no file to write (it opens only /dev/null, read-only)", not p, p)
control("a bridge.mm that reads an environment variable",
        reads_problems(BRIDGE.replace("Share share{3,", 'getenv("X");Share share{3,', 1)))
control("a bridge.mm that opens a file to write", reads_problems(
    BRIDGE.replace('open("/dev/null",O_RDONLY|O_CLOEXEC)', 'open("/tmp/x",O_WRONLY|O_CLOEXEC)', 1)))


def wsi_problems(bridge):
    p = []
    m = re.search(r"bool unsupported_device_extension\(const char \*name\) \{(.*?)\n\}", bridge, re.S)
    listed = set(re.findall(r'"(VK_\w+)"', m.group(1))) if m else set()
    for e in ("VK_KHR_present_id", "VK_KHR_present_id2", "VK_KHR_present_wait", "VK_KHR_present_wait2",
              "VK_KHR_swapchain_maintenance1", "VK_EXT_swapchain_maintenance1", "VK_KHR_swapchain_mutable_format",
              "VK_KHR_incremental_present", "VK_EXT_present_timing", "VK_GOOGLE_display_timing", "VK_EXT_hdr_metadata"):
        if e not in listed:
            p.append(f"{e}, which NVK's window-system layer offers, is not filtered")
    if "unsupported_device_extension(e.extensionName)" not in bridge or "if (unsupported_device_extension(name)) return VK_ERROR_EXTENSION_NOT_PRESENT;" not in bridge:
        p.append("the device extensions are not both hidden and refused")
    if "if (window_system_proc(name)) return nullptr;\n   return backend_gipa(instance,name);" not in bridge or \
       "if (window_system_proc(name)) return d->swapchain ? lookup(name) : nullptr;" not in bridge:
        p.append("a surface, swapchain or present entry point can reach the renderer")
    return p


def metal_wsi(wsi):
    return ".EXT_metal_surface = true," in wsi and "VK_USE_PLATFORM_METAL_EXT" in wsi


wsi = (ROOT / "nvk" / "nvk-metal-wsi.patch").read_text()
check("NVK's chain gives NVK its own Metal window-system layer (nvk-metal-wsi.patch), the reason for the filter",
      metal_wsi(wsi))
check("control: a patch without the Metal surface extension is caught",
      not metal_wsi(wsi.replace(".EXT_metal_surface = true,", "")), "the check found nothing wrong in a copy that is wrong")
p = wsi_problems(BRIDGE)
check("bridge.mm neither offers nor accepts the renderer's own surface and swapchain extensions, and no surface, "
      "swapchain or present entry point reaches the renderer", not p, p)
control("a filter that lets present wait through", wsi_problems(BRIDGE.replace('"VK_KHR_present_wait", ', "", 1)))
control("a device lookup that forwards unknown swapchain functions to the renderer", wsi_problems(
    BRIDGE.replace("if (window_system_proc(name)) return d->swapchain ? lookup(name) : nullptr;", "", 1)))


# ------------------------------------------------------------------------------- build.sh, the package, the installer
def build_sh_problems(text):
    p = []
    allc = re.search(r'^ALL_COMPONENTS="([^"]*)"', text, re.M).group(1).split()
    if "nvkbridge" not in allc or allc.index("nvkbridge") < allc.index("nvk"):
        p.append(f"ALL_COMPONENTS {allc} does not build nvkbridge after nvk")
    info = re.search(r'^    nvkbridge\) (.*?);;', text, re.M | re.S)
    if not info or 'C_DEPS="nvk"' not in info.group(1) or 'C_SRC="tools/nvk-bridge"' not in info.group(1) or \
       'C_OUT="$O/nvkbridge/lib/libnvk_minecraft.dylib"' not in info.group(1):
        p.append("comp_info nvkbridge does not depend on nvk, hash tools/nvk-bridge and name the frontend")
    if 'bash "$HERE/tools/nvk-bridge/build.sh" "$O/nvk/libvulkan_nouveau.dylib" "$O/nvk-build/mesa/include" "$O/nvkbridge"' not in text:
        p.append("the step does not give the script this build's NVK and its Mesa tree's headers")
    userland = re.search(r"userland\) out=\"\$out ([^\"]*)\"", text).group(1).split()
    if "nvkbridge" not in userland:
        p.append("the userland group lacks nvkbridge")
    return p


bsh = (ROOT / "build.sh").read_text()
p = build_sh_problems(bsh)
check("build.sh builds nvkbridge after nvk in all and userland, from tools/nvk-bridge and this build's NVK", not p, p)
control("a build.sh that leaves the component out of all", build_sh_problems(bsh.replace(" nvkbridge vulkan", " vulkan", 1)))

FILES = {"lib/libnvk_minecraft.dylib": "755", "lib/libnvk_renderer.dylib": "755", "nvk_bridge_icd.json": "",
         "nvk-bridge-runtime.properties": ""}


def package_problems(text):
    p = []
    if "NB=Library/GPUBundles/nvmtl/minecraft" not in text:
        return ["package_local.sh does not stage into Library/GPUBundles/nvmtl/minecraft"]
    staged = {}
    for m in re.finditer(r'for f in ([^;]*); do put_file "\$OUT/nvkbridge/\$f" "\$NB/\$f"( 755)?; done', text):
        for f in m.group(1).split():
            staged[f] = (m.group(2) or "").strip()
    if staged != FILES:
        p.append(f"package_local.sh stages {staged}, not {FILES}")
    if 'drop_lines "$NB"; rm -rf "${R:?}/$NB"' not in text:
        p.append("a runtime folder of the base package is not cleared first")
    for src, dst in (("INSTALL.md", "MINECRAFT-NVK.md"), ("APPLICATIONS.md", "NVK-APPLICATIONS.md")):
        if f'put_file "$HERE/tools/nvk-bridge/{src}" {dst}\n' not in text:
            p.append(f"the package does not carry {src} as {dst}")
    return p


pkg = (ROOT / "build" / "package_local.sh").read_text()
p = package_problems(pkg)
check("package_local.sh puts the four files (the two libraries 755) under Library/GPUBundles/nvmtl/minecraft, and "
      "INSTALL.md and APPLICATIONS.md at the package's top", not p, p)
control("a package that leaves out the renderer", package_problems(pkg.replace(" lib/libnvk_renderer.dylib;", ";", 1)))
control("a package without the Minecraft guide",
        package_problems(pkg.replace('put_file "$HERE/tools/nvk-bridge/INSTALL.md" MINECRAFT-NVK.md\n', "", 1)))
def readme_problems(readme):
    listed = sorted(re.findall(r"^\| `([^`]+)` \|", readme.split("## What is installed")[1].split("##")[0], re.M))
    return [] if listed == sorted(FILES) else [f"README.md lists {listed}, not {sorted(FILES)}"]


readme = (NB / "README.md").read_text()
p = readme_problems(readme)
check("README.md lists exactly the files the package installs", not p, p)
control("a README.md without the renderer's row",
        readme_problems(re.sub(r"^\| `lib/libnvk_renderer\.dylib` \|[^\n]*\n", "", readme, count=1, flags=re.M)))


def installer_problems(install, uninstall):
    p = []
    loops = {"backup": r'for b in [^;]* nvmtl [^;]*; do \[ ! -e "\$GB/\$b" \] \|\| backup_copy "\$b" ditto "\$GB/\$b" "\$BK/\$b"; done',
             "rollback": r'for b in [^;]* nvmtl [^;]*; do\n    rm -rf "\$GB/\$b" \|\| failed=1\n    \[ ! -e "\$BK/\$b" \] \|\| ditto "\$BK/\$b" "\$GB/\$b"',
             "replace": r'for b in NVMTLDriver.bundle nvmtl( [^;]*)?; do rm -rf "\$GB/\$b" && ditto "\$HERE/Library/GPUBundles/\$b" "\$GB/\$b"',
             "owner": r'chown -R root:wheel [^\n]*"\$GB/nvmtl"', "modes": r'chmod -R go-w,a\+rX [^\n]*"\$GB/nvmtl"',
             "step 6b fallback": r'for b in [^;]* nvmtl [^;]*; do rm -rf "\$GB/\\\$b"; \[ ! -e "\\\$BK/\\\$b" \] \|\| ditto "\\\$BK/\\\$b" "\$GB/\\\$b"; done'}
    for what, rx in loops.items():
        if not re.search(rx, install):
            p.append(f"install.sh: the {what} does not take Library/GPUBundles/nvmtl as a whole folder")
    for what, rx in {"backup": r'for b in [^;]* nvmtl [^;]*; do \[ ! -e "\$GB/\$b" \] \|\| ditto "\$GB/\$b" "\$RB/\$b"',
                     "removal": r'for b in [^;]* nvmtl [^;]*; do rm -rf "\$GB/\$b"',
                     "restore": r'for b in [^;]* nvmtl [^;]*; do \[ ! -e "\$RB/\$b" \] \|\| ditto "\$RB/\$b" "\$GB/\$b"'}.items():
        if not re.search(rx, uninstall):
            p.append(f"uninstall.sh: the {what} does not take Library/GPUBundles/nvmtl as a whole folder")
    return p


ins = (ROOT / "package" / "install.sh").read_text(); unins = (ROOT / "package" / "uninstall.sh").read_text()
p = installer_problems(ins, unins)
check("install.sh backs up, replaces (root:wheel, go-w), rolls back and in its step 6b fallback restores "
      "Library/GPUBundles/nvmtl whole, and uninstall.sh backs it up, removes and restores it whole", not p, p)
control("an installer whose fallback leaves nvmtl out", installer_problems(
    ins.replace('for b in NVMTLDriver.bundle NVIDIAShared.bundle nvmtl nvmtl-allow.txt $ZINK_GB; do rm -rf "$GB/\\$b"',
                'for b in NVMTLDriver.bundle NVIDIAShared.bundle nvmtl-allow.txt $ZINK_GB; do rm -rf "$GB/\\$b"', 1), unins))


# ------------------------------------------------------------------------------------------ host_test.mm on a Mac
# The Vulkan headers as the other tests that compile take them: NM_VULKAN_INCLUDE (./build.sh test and the workflow ci
# set it), else ~/Vulkan-Headers/include. Built for the machine's own architecture: AddressSanitizer does not run
# under Rosetta.
inc = os.environ.get("NM_VULKAN_INCLUDE", "") or str(Path.home() / "Vulkan-Headers/include")
if sys.platform != "darwin":
    print("note: host_test.mm needs a Mac (Foundation, Metal, IOSurface, IOKit); not built here")
elif not (Path(inc) / "vulkan/vulkan.h").is_file():
    if os.environ.get("NM_VULKAN_INCLUDE"):
        check("NM_VULKAN_INCLUDE holds vulkan/vulkan.h", False, inc)
    elif not fails:
        print("SKIP: no Vulkan headers: set NM_VULKAN_INCLUDE to a folder holding vulkan/vulkan.h (./build.sh test does, "
              "after ./build.sh fetch), or put Vulkan-Headers in ~/Vulkan-Headers; the other checks passed, host_test.mm "
              "is not built")
        sys.exit(2)
else:
    t = Path(tempfile.mkdtemp(prefix="nvkbridge-host-"))
    try:
        exe = t / "host_test"
        r = subprocess.run(["/usr/bin/xcrun", "clang++", "-mmacosx-version-min=15.0", "-std=c++17", "-fobjc-arc", "-O1",
                            "-g", "-fsanitize=address,undefined", "-Wall", "-Wextra", "-Wno-missing-field-initializers",
                            "-Werror", f"-I{inc}", str(NB / "host_test.mm"), "-framework", "Foundation", "-framework",
                            "QuartzCore", "-framework", "Metal", "-framework", "IOSurface", "-framework", "IOKit",
                            "-o", str(exe)], capture_output=True, text=True, timeout=300)
        check("host_test.mm builds with AddressSanitizer and UndefinedBehaviorSanitizer", r.returncode == 0,
              (r.stdout + r.stderr)[-800:])
        if r.returncode == 0:
            r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
            check("host_test.mm passes (the frontend against a fake renderer)",
                  r.returncode == 0 and "HOST CHECKS PASSED" in r.stdout, (r.stdout + r.stderr)[-800:])
    finally:
        shutil.rmtree(t, ignore_errors=True)

print("all passed" if not fails else f"{fails} failed")
sys.exit(1 if fails else 0)
