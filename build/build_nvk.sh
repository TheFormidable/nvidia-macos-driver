#!/bin/bash
# Build NVK (Mesa Vulkan driver for NVIDIA + the NAK compiler) for x86_64 macOS from pristine Mesa plus the patches in nvk/.
#
# usage: build_nvk.sh <mesa git dir> <open-gpu-kernel-modules 610.57.04 tree> <out dir> [2.9|2.8]
#
# The release argument selects the patch set (default 2.9):
#   Both end with nvk/dlss/patch_dlss_cache.py.
#   2.9  nvk-macos, nvk-fixes-after-def16, nvk-620-kernel, nvk-recycle, nvk-slab, nvk-slabdef, nvk-hubfix,
#        nvk-perf-boost-burst, nvk-rated-tdp, nvk-gps-maxpower, nvk-metal-wsi (VK_EXT_metal_surface: windows), nvk-card-select (NVK_ACCEL_ID),
#        hwrt/ttu-nak + hwrt/ttu-rq (ray queries on the RT cores, compiled in, off unless NVK_HWRT_QUERY=1; docs/HWRT.md)
#        ... nvk-warnings, nvk-unbound-stage (a read of an empty binding by an earlier stage is reported; the shipped outcome is kept,
#        NVK_UNBOUND_STAGE_STRICT=1 refuses it)
#        nvk-diag-nofollow (the opt-in diagnostic log is created without following a link, for its owner alone)
#        then upstream Mesa commits newer than nvk/MESA_BASE, unchanged (each patch lists its commits in its header):
#        nvk-mesa-26.3.0 (what the 26.3.0 release has over rc3 in the files NVK is built from; it holds the RTX 50 fix
#        for a register read before its uniform Ldcg load finished), nvk-nak-fixes-main (four NAK fixes that are only on
#        Mesa main: barrier handling, RTX 50 matrix-instruction latencies), nvk-nak-sched-main (four code generation
#        changes that are only on Mesa main: instruction order before register allocation, fall-through branches,
#        flattening of small ifs)
#        then nvk-evwait (waits that have run 1 ms sleep on the GPU's completion event instead of polling, in every process;
#        nvkmd/nvrm/nvrm_evwait.h in it is a copy of kexts/NVRM/nvrm_evwait.h; NVRM_EVENT_WAIT=0 in nvrm610.conf polls)
#        then nvk-fence (channels registered with the kernel's per-surface fence, nvmtl_nvk_fence_v1 for the plugin;
#        nvkmd/nvrm/nvrm_fence_abi.h in it is a copy of kexts/NVRM/nvrm_fence_abi.h; boot-arg nvrmfence=0 turns it off)
#        then nvk-channel-recover (a faulted or stalled channel is rebuilt instead of losing the device; nvkmd/nvrm/
#        nvrm_chan_note_abi.h in it is a copy of kexts/NVRM/nvrm_chan_note_abi.h; NVK_CHANNEL_RECOVER=0 in nvrm610.conf
#        turns it off)
#   2.8  the set the 2.8 / 2.8.1 package was built from: the same chain without hubfix, perf-boost-burst, rated-tdp and
#        gps-maxpower, and with nvk/release-2.8/nvk-fixes-after-def16.patch (the copy-engine retry still logged to stderr)
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
#   NVK_SKIP_PATCHES   space-separated names of upstream-fix patches to leave out of the chain: nvk-mesa-26.3.0,
#                   nvk-nak-fixes-main, nvk-nak-sched-main. For finding which of them a rendering fault comes from
#                   (start with nvk-nak-sched-main: it changes the code of every shader and is not a fix).
#                   The summary line of a build made with either variable (also kept in <out dir>/BUILD.txt) ends with
#                   "WITHOUT <names>" or "PLUS <names>"; unset both for a library that is meant to be used.
#   NVK_REF_EXPORTS optional file with the exported symbols to compare against (one name per line, sorted)
#
# The Mesa git must contain the commit in nvk/MESA_BASE (2.9: 90a959eb, mesa-26.3.0-rc3) and, for 2.8, 17ca6174. The tree is exported with git archive, so no .git is
# present and the binary carries no git sha. Rebuilding is deterministic: the same inputs give the same LC_UUID.
set -euo pipefail

MESA_GIT=${1:?mesa git dir}; OGKM=${2:?open-gpu-kernel-modules 610.57.04 tree}; OUT=${3:?output dir}; REL=${4:-2.9}
HERE=$(cd "$(dirname "$0")/.." && pwd); NVK=$HERE/nvk
BASE=$(cat "$NVK/MESA_BASE")
JOBS=${NVK_JOBS:-6}
UPSTREAM_FIXES="nvk-mesa-26.3.0 nvk-nak-fixes-main nvk-nak-sched-main"   # the chain's patches that are upstream Mesa commits, unchanged
[ -n "${NVK_PATH:-}" ] && export PATH="$NVK_PATH:$PATH"

case $REL in
  2.9) FIXES=$NVK/nvk-fixes-after-def16.patch
       CHAIN="nvk-macos @FIXES nvk-620-kernel nvk-recycle nvk-slab nvk-slabdef nvk-hubfix nvk-perf-boost-burst nvk-rated-tdp nvk-gps-maxpower nvk-metal-wsi nvk-card-select hwrt/ttu-nak hwrt/ttu-rq curves/curves hwrt/wide nvk-warnings nvk-unbound-stage nvk-diag-nofollow nvk-mesa-26.3.0 nvk-nak-fixes-main nvk-nak-sched-main nvk-evwait nvk-fence nvk-channel-recover" ;;
  2.8) FIXES=$NVK/release-2.8/nvk-fixes-after-def16.patch
       BASE=17ca6174   # the 2.8 / 2.8.1 package was built on this Mesa; 2.9 moved to nvk/MESA_BASE (26.3)
       CHAIN="nvk-macos @FIXES nvk-620-kernel nvk-recycle nvk-slab nvk-slabdef" ;;
  *) echo "STOP: release must be 2.9 or 2.8"; exit 2 ;;
esac

# NVK_EXTRA_PATCHES appends patch files from nvk/ to the chain. 2.9 had one such file, nvk-upstream-nak; its commits are
# in the three upstream-fix patches of the chain now and the file is gone, so that name is dropped here with a note
# instead of stopping the build of someone who still sets it.
ADDED=
for p in ${NVK_EXTRA_PATCHES:-}; do
  if [ "$p" = nvk-upstream-nak ]; then
    echo "note: NVK_EXTRA_PATCHES=nvk-upstream-nak is ignored: that file is gone, its upstream fixes are in the default 2.9 chain ($UPSTREAM_FIXES)"
  else
    CHAIN="$CHAIN $p"; ADDED="$ADDED $p"
  fi
done
# NVK_SKIP_PATCHES leaves upstream-fix patches out. Only those can be named: they change compiler files that no later
# patch of the chain touches, and none of the three needs another, so the rest of the chain applies whichever are
# left out. Any other name stops the build: a misspelt name that was silently ignored would give a build that still has
# the patch its owner believes is out.
SKIP=" "
for p in ${NVK_SKIP_PATCHES:-}; do
  case " $UPSTREAM_FIXES " in
    *" $p "*) SKIP="$SKIP$p " ;;
    *) echo "STOP: NVK_SKIP_PATCHES can only name $UPSTREAM_FIXES (got: $p)"; exit 2 ;;
  esac
  case " $CHAIN " in
    *" $p "*) ;;
    *) echo "note: NVK_SKIP_PATCHES names $p, which the $REL chain does not have" ;;
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
UUID=$(otool -l "$OUT/libvulkan_nouveau.dylib" | awk '/uuid/{print $2; exit}')
# BUILD.txt is all the release build (build_release_inputs.sh) shows and records of this script; the rest of what is
# printed here goes to a log there. A library built with a patch left out or added (a variable still set from a fault
# hunt is enough) must not look like the default one in that record, so the line ends with what differs.
echo "release $REL  sha256 $(shasum -a 256 "$OUT/libvulkan_nouveau.dylib" | cut -c1-8)  LC_UUID $UUID  exports $(wc -l < "$OUT/exports.txt")$OTHER_CHAIN" | tee "$OUT/BUILD.txt"
if [ -n "${NVK_REF_EXPORTS:-}" ]; then
  diff "$NVK_REF_EXPORTS" "$OUT/exports.txt" > "$OUT/exports.diff" && echo "exports: identical to reference" || { echo "exports differ from reference:"; cat "$OUT/exports.diff"; }
fi
