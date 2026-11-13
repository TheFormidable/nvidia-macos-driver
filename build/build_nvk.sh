#!/bin/bash
# Build NVK (Mesa Vulkan driver for NVIDIA + the NAK compiler) for x86_64 macOS from pristine Mesa plus the patches in nvk/.
#
# usage: build_nvk.sh <mesa git dir> <open-gpu-kernel-modules 610.57.04 tree> <out dir> [3.0|2.9|2.8]
#
# The release argument selects the patch set (default 3.0). Each is the set that release's library was built from, so
# an older release can be built again to compare with:
#   All end with nvk/dlss/patch_dlss_cache.py.
#   3.0  the 2.9 chain below, then upstream Mesa commits newer than nvk/MESA_BASE, unchanged (each patch lists its
#        commits in its header): nvk-mesa-26.3.0 (what the 26.3.0 release has over rc3 in the files NVK is built from;
#        it holds the RTX 50 fix for a register read before its uniform Ldcg load finished), nvk-nak-fixes-main (four
#        NAK fixes that are only on Mesa main: barrier handling, RTX 50 matrix-instruction latencies),
#        nvk-nak-sched-main (four code generation changes that are only on Mesa main: instruction order before
#        register allocation, fall-through branches, flattening of small ifs)
#        then nvk-evwait (waits that have run 1 ms sleep on the GPU's completion event instead of polling, in every process;
#        nvkmd/nvrm/nvrm_evwait.h in it is a copy of kexts/NVRM/nvrm_evwait.h; NVRM_EVENT_WAIT=0 in nvrm610.conf polls)
#        then nvk-fence (channels registered with the kernel's per-surface fence, nvmtl_nvk_fence_v1 for the plugin;
#        nvkmd/nvrm/nvrm_fence_abi.h in it is a copy of kexts/NVRM/nvrm_fence_abi.h; boot-arg nvrmfence=0 turns it off)
#        then nvk-channel-recover (a faulted or stalled channel is rebuilt instead of losing the device; nvkmd/nvrm/
#        nvrm_chan_note_abi.h in it is a copy of kexts/NVRM/nvrm_chan_note_abi.h; NVK_CHANNEL_RECOVER=0 in nvrm610.conf
#        turns it off)
#   2.9  nvk-macos, nvk-fixes-after-def16, nvk-620-kernel, nvk-recycle, nvk-slab, nvk-slabdef, nvk-hubfix,
#        nvk-perf-boost-burst, nvk-rated-tdp, nvk-gps-maxpower, nvk-metal-wsi (VK_EXT_metal_surface: windows), nvk-card-select (NVK_ACCEL_ID),
#        hwrt/ttu-nak + hwrt/ttu-rq (ray queries on the RT cores, compiled in, off unless NVK_HWRT_QUERY=1; docs/HWRT.md),
#        curves/curves and hwrt/wide (the curve and wide-BVH ray-traversal sources, with the headers copied in after the chain, below)
#        ... nvk-warnings, nvk-unbound-stage (a read of an empty binding by an earlier stage is reported; the shipped outcome is kept,
#        NVK_UNBOUND_STAGE_STRICT=1 refuses it)
#        nvk-diag-nofollow (the opt-in diagnostic log is created without following a link, for its owner alone)
#        It is built only from the files release 2.9 was built from: a file of nvk/ that 2.9's build reads and that a
#        later change edited stops the build, by name (R29_FILES below), instead of giving a library that is not 2.9's.
#   2.8  the set the 2.8 / 2.8.1 package was built from, on an older Mesa (17ca6174): only nvk-macos,
#        nvk/release-2.8/nvk-fixes-after-def16.patch (the copy-engine retry still logged to stderr), nvk-620-kernel,
#        nvk-recycle, nvk-slab and nvk-slabdef. None of the later additions above (hubfix onward, the hwrt/curves
#        sources, the upstream-Mesa fixes, and the event-wait / fence / channel-recover chain) are in it.
# The NAK changes (nvk/speed267) are already part of nvk-macos.patch; the scripts there are the history of how they
# were made and are not run here.
#
# Environment (build host specifics):
#   NVK_PATH        directories put first on PATH: python with mako/pyyaml, glslangValidator, llvm-spirv, llvm-config
#                   (LLVM 22.1.x), cargo with the x86_64-apple-darwin target, bindgen, cbindgen, meson, ninja
#   PKG_CONFIG_PATH must reach LLVMSPIRVLib.pc (SPIRV-LLVM-Translator matching the LLVM) for the native mesa_clc build
#   NVK_SYSROOT     x86_64 prefix holding lib/pkgconfig for zlib (and any other library the build needs)
#   NVK_JOBS        parallel jobs (default 6)
#   NVK_EXTRA_PATCHES  space-separated names of extra patches in nvk/ applied last
#   NVK_SKIP_PATCHES   space-separated names of upstream-fix patches to leave out of the 3.0 chain: nvk-mesa-26.3.0,
#                   nvk-nak-fixes-main, nvk-nak-sched-main (2.9 and 2.8 have none of them: a name stops their build).
#                   For finding which of them a rendering fault comes from
#                   (start with nvk-nak-sched-main: it changes the code of every shader and is not a fix).
#                   The summary line of a build made with either variable (also kept in <out dir>/BUILD.txt) ends with
#                   "WITHOUT <names>" or "PLUS <names>"; unset both for a library that is meant to be used.
#   NVK_REF_EXPORTS optional file with the exported symbols to compare against (one name per line, sorted)
#
# The Mesa git must contain the commit in nvk/MESA_BASE (3.0 and 2.9: 90a959eb, mesa-26.3.0-rc3) and, for 2.8, 17ca6174. The tree is exported with git archive, so no .git is
# present and the binary carries no git sha. Rebuilding is deterministic: the same inputs give the same LC_UUID.
set -euo pipefail

MESA_GIT=${1:?mesa git dir}; OGKM=${2:?open-gpu-kernel-modules 610.57.04 tree}; OUT=${3:?output dir}; REL=${4:-3.0}
HERE=$(cd "$(dirname "$0")/.." && pwd); NVK=$HERE/nvk
BASE=$(cat "$NVK/MESA_BASE")
JOBS=${NVK_JOBS:-6}
UPSTREAM_FIXES="nvk-mesa-26.3.0 nvk-nak-fixes-main nvk-nak-sched-main"   # the chain's patches that are upstream Mesa commits, unchanged
[ -n "${NVK_PATH:-}" ] && export PATH="$NVK_PATH:$PATH"

case $REL in
  3.0) FIXES=$NVK/nvk-fixes-after-def16.patch
       CHAIN="nvk-macos @FIXES nvk-620-kernel nvk-recycle nvk-slab nvk-slabdef nvk-hubfix nvk-perf-boost-burst nvk-rated-tdp nvk-gps-maxpower nvk-metal-wsi nvk-card-select hwrt/ttu-nak hwrt/ttu-rq curves/curves hwrt/wide nvk-warnings nvk-unbound-stage nvk-diag-nofollow nvk-mesa-26.3.0 nvk-nak-fixes-main nvk-nak-sched-main nvk-evwait nvk-fence nvk-channel-recover" ;;
  2.9) FIXES=$NVK/nvk-fixes-after-def16.patch
       BASE=90a959eb   # the 2.9 package was built on this Mesa (mesa-26.3.0-rc3), whatever nvk/MESA_BASE names later
       CHAIN="nvk-macos @FIXES nvk-620-kernel nvk-recycle nvk-slab nvk-slabdef nvk-hubfix nvk-perf-boost-burst nvk-rated-tdp nvk-gps-maxpower nvk-metal-wsi nvk-card-select hwrt/ttu-nak hwrt/ttu-rq curves/curves hwrt/wide nvk-warnings nvk-unbound-stage nvk-diag-nofollow" ;;
  2.8) FIXES=$NVK/release-2.8/nvk-fixes-after-def16.patch
       BASE=17ca6174   # the 2.8 / 2.8.1 package was built on this Mesa; 2.9 moved to nvk/MESA_BASE (26.3)
       CHAIN="nvk-macos @FIXES nvk-620-kernel nvk-recycle nvk-slab nvk-slabdef" ;;
  *) echo "STOP: release must be 3.0, 2.9 or 2.8"; exit 2 ;;
esac

# NVK_EXTRA_PATCHES appends patch files from nvk/ to the chain. 2.9 had one such file, nvk-upstream-nak (opt-in, and it
# did not apply on the 26.3.0-rc3 base). The file is gone: its commits are the three upstream-fix patches of the 3.0
# chain. Its name stops every release, with what to do instead: a build that went on without it would not be what was
# asked for.
ADDED=
for p in ${NVK_EXTRA_PATCHES:-}; do
  if [ "$p" = nvk-upstream-nak ]; then
    if [ "$REL" = 3.0 ]; then
      echo "STOP: NVK_EXTRA_PATCHES names nvk-upstream-nak, a file of 2.9 that is gone: its upstream fixes are already in the 3.0 chain ($UPSTREAM_FIXES). Take the name out of NVK_EXTRA_PATCHES"
    else
      echo "STOP: NVK_EXTRA_PATCHES names nvk-upstream-nak, which is no longer in nvk/: a $REL library built with it cannot be built from this tree (the tree of tag v2.9 has the file). Without the name this builds the $REL chain alone"
    fi
    exit 2
  fi
  CHAIN="$CHAIN $p"; ADDED="$ADDED $p"
done
# NVK_SKIP_PATCHES leaves upstream-fix patches out of the 3.0 chain. Only those can be named: they change compiler files
# that no later patch of the chain touches, and none of the three needs another, so the rest of the chain applies
# whichever are left out. Any other name stops the build: a misspelt name that was silently ignored would give a build
# that still has the patch its owner believes is out. So does a name that is not in the chain asked for (2.9 and 2.8
# have none of the three): the library would be that release's whole chain, not one with a patch left out.
SKIP=" "
for p in ${NVK_SKIP_PATCHES:-}; do
  case " $UPSTREAM_FIXES " in
    *" $p "*) SKIP="$SKIP$p " ;;
    *) echo "STOP: NVK_SKIP_PATCHES can only name $UPSTREAM_FIXES (got: $p)"; exit 2 ;;
  esac
  case " $CHAIN " in
    *" $p "*) ;;
    *) echo "STOP: NVK_SKIP_PATCHES names $p, which the $REL chain does not have (only the 3.0 chain has $UPSTREAM_FIXES). Unset NVK_SKIP_PATCHES to build $REL"; exit 2 ;;
  esac
done
KEPT=; LEFT_OUT=
for p in $CHAIN; do
  case "$SKIP" in
    *" $p "*) echo "skipped $p.patch (NVK_SKIP_PATCHES)"; LEFT_OUT="$LEFT_OUT $p" ;;
    *) KEPT="$KEPT $p" ;;
  esac
done
CHAIN=${KEPT# }
echo "patch chain ($REL): $CHAIN"
# A chain that is not the default one is named again in the summary line at the end, the one kept in BUILD.txt.
OTHER_CHAIN="${LEFT_OUT:+  WITHOUT$LEFT_OUT (NVK_SKIP_PATCHES)}${ADDED:+  PLUS$ADDED (NVK_EXTRA_PATCHES)}"

# 2.9 is built only from the files release 2.9 was built from (tag v2.9): the SHA-256 of each file of nvk/ that its build
# reads, as 2.9 shipped it. A later change to one of them would otherwise give a library that is not 2.9's under 2.9's
# name. (Its Mesa commit is fixed in the table above.)
R29_FILES="
cc397cb87acec85a961a95a8e938b04ea0167b2ffcf1c5ff618e8af97c00af17  ogkm-nvos-620.patch
e8ed684fd6ea369c14cf5379324b4fea823418c8fec6a6daf30429a88ded524c  ogkm-warnings.patch
d8c6b112c1f6b86a89035fbc484da128aa86191fadd21a81f63badb25035984b  nvk-macos.patch
f189d9d7c8df6739acbec054c1d65690a28691c97ec2ff8dcadc7f4941a27f2b  nvk-fixes-after-def16.patch
988af63e96553173ee2e0fc002503759df37c5e35d2d9df6505ed200c1e3168f  nvk-620-kernel.patch
f49e522defe02aa11053c251ca83e3da026e04013241f0f0d832feda152b7295  nvk-recycle.patch
9efa2d23f323435fe6d84c1ea76ec397793f8f591d557c7e49937db95d31f8dd  nvk-slab.patch
c537b8ffc0f51ce83f453b7377df2b3f1ecbef41aebd43fc962cf69074c20bd3  nvk-slabdef.patch
39618bc806c0a5dd058b502a00d44c16049597192dadc3a233df010cc413fd92  nvk-hubfix.patch
301229c0b0e598591f693c5f37b1f22b1cd5c17c8b877311a1b4a828e4283d30  nvk-perf-boost-burst.patch
ed3edf2274faab7046d5eecb00db76d1c380dd8c9d18437269103ae455b42e2a  nvk-rated-tdp.patch
1e6c82f9513f02b03dd68f7205d3e5b0fba7779972abd4c9b98046055f789cd2  nvk-gps-maxpower.patch
2b0c353c4497b6d607de24fab70e8e8a10e51460b49544ce047b932ada3636ea  nvk-metal-wsi.patch
1539949dd35a238a734aece1f558978883f135731fba89f6456f2a92850655fa  nvk-card-select.patch
80bb66dc9b164b9fd8c35e707040420e37f5ca3d87afd1a92e1bee92cf33aadc  hwrt/ttu-nak.patch
dae6eaae7524993e8cefd7f7a70528dd293bb84d3ffaaa313d2b4ef8b5c12684  hwrt/ttu-rq.patch
00b09d67e5b80c3a9f75da3d82a6514206bc8ad84c43f1b706314882b348c71d  curves/curves.patch
a6f0c761da23b937c61228058ee66bd19154c602af39c75829917be41583cc98  hwrt/wide.patch
02a5b9017c836371fc0c839d4822f7f3fc34f8e9858fa4845057254dc51c3d62  nvk-warnings.patch
dbc62b2fe104d68ac399a176e4b91d6486b648328de2aa078bb80b6d2fdfe2f5  nvk-unbound-stage.patch
aa1af3d9bdb081525d8ed066df7851c3054cb3457d99ef3235508cadb481ab2f  nvk-diag-nofollow.patch
d56a2a91a616bcddcbd7c96f94780fd1d67a1cde6ee28f4be4f771b4a5db0345  hwrt/nvk_hwrt.h
4b78db905569e072ee64ef4e5020db2f5fac4ff1c2fcb00088060400b08bd626  hwrt/nvk_hwrt_layout.h
d738be6746112791a7ed4c4d7084c476a5c5923c091251e36de735f33632b826  curves/nvk_curve.h
fda9de8505368f286d365a8fe34e1219c7cb63b9b8b51b04e26cb1fb5427ef04  hwrt/encode_wide.comp
7483ac16d85a235783f0e4351a591b0dd0c0ec98010cb1099179f3a3d490e0e6  hwrt/encode_wide_body.h
0421e7c2f96e071908b5a9788a1f72cb1cf35eac80ff983a3e3c84e6dfad33b6  curves/curve_math.h
3e55df3278b7c41b0da550c0ebd5661a8574cefeb999f00884606f5a535cae84  curves/curve_nir.h
c2b1d5b3b45062c7b374768a32e15181d49ae839360a79c20cfe32e5f2498159  dlss/patch_dlss_cache.py
"
if [ "$REL" = 2.9 ]; then
  while read -r sum f; do
    [ -n "$f" ] || continue
    have=$(shasum -a 256 "$NVK/$f" 2>/dev/null | cut -d' ' -f1) || have=
    [ "$have" = "$sum" ] || { echo "STOP: nvk/$f is not the file release 2.9 was built from (it was changed or removed since), so this tree cannot build 2.9's NVK. Build it from release 2.9's tree (tag v2.9)"; exit 2; }
  done <<EOF
$R29_FILES
EOF
fi

git -C "$MESA_GIT" cat-file -e "$BASE^{commit}" || { echo "STOP: $MESA_GIT has no commit $BASE"; exit 2; }
mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
S=$OUT/mesa; rm -rf "$S" "$OUT/ogkm"; mkdir -p "$S"
git -C "$MESA_GIT" archive "$BASE" | tar -x -C "$S"

# Headers: NVK compiles against the 610 open kernel module headers with the 620 nvos.h change applied.
cp -R "$OGKM" "$OUT/ogkm"
patch -p1 -s --no-backup-if-mismatch -d "$OUT/ogkm" < "$NVK/ogkm-nvos-620.patch"
[ "$REL" = 2.8 ] || patch -p1 -s --no-backup-if-mismatch -d "$OUT/ogkm" < "$NVK/ogkm-warnings.patch"   # DRF_MASK, also defined by Mesa's drf.h

for p in $CHAIN; do
  f=$NVK/$p.patch; [ "$p" = "@FIXES" ] && f=$FIXES
  patch -p1 -s --no-backup-if-mismatch -d "$S" < "$f" || { echo "STOP: $f does not apply"; exit 3; }
  echo "applied $(basename "$f")"
done
# the shared layout headers of the hardware ray-traversal part live in nvk/hwrt (the host tests include them too)
cp "$NVK/hwrt/nvk_hwrt.h" "$NVK/hwrt/nvk_hwrt_layout.h" "$NVK/curves/nvk_curve.h" "$S/src/nouveau/vulkan/"
cp "$NVK/hwrt/encode_wide.comp" "$NVK/hwrt/encode_wide_body.h" "$S/src/nouveau/vulkan/bvh/"
cp "$NVK/curves/curve_math.h" "$S/src/nouveau/vulkan/nvk_curve_math.h"
sed 's/#include "curve_math.h"/#include "nvk_curve_math.h"/' "$NVK/curves/curve_nir.h" > "$S/src/nouveau/vulkan/nvk_curve_nir.h"
# the nvrm directory comes from nvk-macos.patch
ln -s "$OUT/ogkm" "$S/src/nouveau/vulkan/nvkmd/nvrm/open-gpu-kernel-modules"
python3 "$NVK/dlss/patch_dlss_cache.py" "$S"
find "$S" -name '*.orig' -o -name '*.rej' | grep . && { echo "STOP: patch left .orig/.rej files"; exit 3; } || true

# mesa_clc and vtn_bindgen2 precompile NVK's internal CL shaders to serialized NIR. They MUST be built from this same
# tree: tools from another Mesa revision give no build error and deserialize as garbage at run time.
H=$OUT/host
if [ ! -x "$H/bin/mesa_clc" ] || [ ! -x "$H/bin/vtn_bindgen2" ]; then
  meson setup "$OUT/clc-native" "$S" -Dbuildtype=release -Dprefix="$H" -Dllvm=enabled -Dmesa-clc=enabled -Dinstall-mesa-clc=true \
    -Dvulkan-drivers= -Dgallium-drivers= -Dplatforms= -Degl=disabled -Dgbm=disabled -Dgles1=disabled -Dgles2=disabled -Dglx=disabled \
    -Dopengl=false > "$OUT/clc-native.setup.log" 2>&1
  ninja -C "$OUT/clc-native" -j"$JOBS" src/compiler/clc/mesa_clc src/compiler/spirv/vtn_bindgen2 > "$OUT/clc-native.log" 2>&1
  mkdir -p "$H/bin"; cp "$OUT/clc-native/src/compiler/clc/mesa_clc" "$OUT/clc-native/src/compiler/spirv/vtn_bindgen2" "$H/bin/"
fi
export PATH="$H/bin:$PATH"

SYS=${NVK_SYSROOT:-}
X=$OUT/x86_64-nvk.ini
cat > "$X" <<EOT
[binaries]
c = ['clang', '-arch', 'x86_64', '-mmacosx-version-min=15.0']
cpp = ['clang++', '-arch', 'x86_64', '-mmacosx-version-min=15.0']
objc = ['clang', '-arch', 'x86_64', '-mmacosx-version-min=15.0']
rust = ['rustc', '--target', 'x86_64-apple-darwin']
ar = 'ar'
strip = 'strip'
pkg-config = 'pkg-config'
[host_machine]
system = 'darwin'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
[properties]
pkg_config_libdir = '$SYS/lib/pkgconfig'
bindgen_clang_arguments = ['--target=x86_64-apple-darwin', '-mmacosx-version-min=15.0']
EOT

# -oso_prefix makes the object paths in the unstripped output (which the linker hashes into LC_UUID) relative to the
# build directory, so the UUID does not depend on where the build ran.
# The build directory sits inside the source tree: Rust and some C debug paths are recorded relative to it
# ("../src/..."), so a build directory elsewhere gives a different binary (and LC_UUID).
B=$S/build
meson setup "$B" "$S" --cross-file="$X" -Dbuildtype=release -Db_ndebug=true -Dvulkan-drivers=nouveau -Dgallium-drivers= -Dplatforms=macos \
  -Degl=disabled -Dglx=disabled -Dglvnd=disabled -Dllvm=disabled -Dmesa-clc=system -Dshared-glapi=disabled \
  -Dvulkan-layers= -Dtools= -Dbuild-tests=false -Dexpat=disabled -Dzstd=disabled -Dlibunwind=disabled \
  -Dlmsensors=disabled -Dvalgrind=disabled -Dxlib-lease=disabled -Dgallium-rusticl=false -Dspirv-tools=disabled \
  "-Dc_args=-ffile-prefix-map=$S=/src -ffile-prefix-map=$HOME=/build" "-Dcpp_args=-ffile-prefix-map=$S=/src -ffile-prefix-map=$HOME=/build" \
  "-Drust_args=--remap-path-prefix=$S=/src --remap-path-prefix=$HOME=/build" \
  "-Dc_link_args=-Wl,-oso_prefix,$B -Wl,-no_warn_duplicate_libraries" "-Dcpp_link_args=-Wl,-oso_prefix,$B -Wl,-no_warn_duplicate_libraries" > "$OUT/setup.log" 2>&1
ninja -C "$B" -j"$JOBS" src/nouveau/vulkan/libvulkan_nouveau.dylib > "$OUT/build.log" 2>&1
# Warnings may only go down: the committed count in nvk/WARNINGS is the ceiling (2.9 brought it to 0).
WARN=$(grep -c 'warning:' "$OUT/build.log" || true); WMAX=$(cat "$NVK/WARNINGS" 2>/dev/null || echo 0)
[ "$WARN" -le "$WMAX" ] || { echo "STOP: $WARN compiler/linker warnings, more than the $WMAX in nvk/WARNINGS"; grep 'warning:' "$OUT/build.log" | head -20; exit 4; }
echo "warnings $WARN (ceiling $WMAX)"

# Licence texts of the Rust crates Mesa fetched for NAK (meson setup downloads them into subprojects/packagecache).
# licenses/nvk-rust-crates.txt in the package must equal this; a different crate set stops the build until it is updated.
python3 -I "$HERE/build/gen_nvk_rust_crates.py" "$S/subprojects/packagecache" "$OUT/nvk-rust-crates.txt"
cmp -s "$OUT/nvk-rust-crates.txt" "$HERE/licenses/nvk-rust-crates.txt" || { echo "STOP: licenses/nvk-rust-crates.txt is out of date; copy $OUT/nvk-rust-crates.txt over it"; exit 6; }

D=$B/src/nouveau/vulkan/libvulkan_nouveau.dylib
U=$(nm -m "$D" | grep '(undefined)' | grep -v weak | grep 'dynamically looked up' || true)
[ -z "$U" ] || { echo "STOP: unresolved strong references (dlopen fails in WindowServer):"; echo "$U"; exit 5; }
cp "$D" "$OUT/libvulkan_nouveau.dylib"; strip -S "$OUT/libvulkan_nouveau.dylib"
nm -gU "$OUT/libvulkan_nouveau.dylib" | awk '{print $3}' | sort > "$OUT/exports.txt"
# The listing is read to its end. A reader that leaves at the first match can kill otool while it is still writing, and
# under pipefail and errexit that stops the script here, after the build, with status 141 and no message.
UUID=$(otool -l "$OUT/libvulkan_nouveau.dylib" | awk '/uuid/ && !n++ {print $2}')
# BUILD.txt is all the release build (build_release_inputs.sh) shows and records of this script; the rest of what is
# printed here goes to a log there. A library built with a patch left out or added (a variable still set from a fault
# hunt is enough) must not look like the default one in that record, so the line ends with what differs.
echo "release $REL  sha256 $(shasum -a 256 "$OUT/libvulkan_nouveau.dylib" | cut -c1-8)  LC_UUID $UUID  exports $(wc -l < "$OUT/exports.txt")$OTHER_CHAIN" | tee "$OUT/BUILD.txt"
if [ -n "${NVK_REF_EXPORTS:-}" ]; then
  diff "$NVK_REF_EXPORTS" "$OUT/exports.txt" > "$OUT/exports.diff" && echo "exports: identical to reference" || { echo "exports differ from reference:"; cat "$OUT/exports.diff"; }
fi
