#!/bin/bash
# package_local.sh <base package> <build out dir> <package dir>: a package tree that package/install.sh accepts, made
# from a release package and the parts this checkout just built (./build.sh package runs it).
#
# Why a base package: a package cannot be made from this repository alone. install.sh refuses a package without
# nullmoth-runtime-check(.sh) and fails without nvmtl-allow.txt, and the plugin bundle's Info.plist and air-runtime, the
# nine video libraries, libvulkan 1.4.362, nvk_icd.json and nvrm610.conf have no source here (docs/PROVENANCE.md). The
# release itself starts each package from the previous one and replaces what it rebuilds; this does the same.
#
#   <base package>   nullmoth-nvidia-<version>.tar.gz, or an unpacked pkgroot folder
#   <build out dir>  ./build.sh's out/ folder (kexts/, nvk/, xlate/, plugin/, nvencvt/, userland/, dlss-pkg/, ...)
#   <package dir>    gets base/ (the unpacked base), pkgroot/ (the result) and the tarball
# Environment: NM_BASE_PACKAGE_SHA256 (checked when set), NM_PACKAGE_VERSION (written to VERSION when set; install.sh
# keeps only digits and dots; ./build.sh package always sets it, by default to CHANGELOG.md's top release, and run
# alone without it this keeps the base package's VERSION), NM_PACKAGE_NAME (tarball name, default nullmoth-nvidia-local.tar.gz), NM_BUILD_INFO
# (a text file copied to NULLMOTH-LOCAL-BUILD.txt in the package).
#
# Every replaced file gets its SHA256SUMS line refreshed (check_package.sh does not compare hashes, install.sh does);
# a replaced kext or bundle loses the lines of its old files first. package/finish_package.sh then stages libnvencvt,
# the AIR decoder, NMSMN with this build's ryzenadj, the boost daemon and the Vulkan loader, reseals the plugin bundle
# and runs check_package.sh; the last check here is the one install.sh makes: shasum --strict -c SHA256SUMS.
set -euo pipefail
BASEPKG=${1:?base package (tarball or pkgroot folder)}; OUT=${2:?build out dir}; PKG=${3:?package dir}
HERE=$(cd "$(dirname "$0")/.." && pwd); P=$HERE/package
mkdir -p "$PKG"; PKG=$(cd "$PKG" && pwd); OUT=$(cd "$OUT" && pwd)
stop() { echo "STOP: $*"; exit 1; }

# 1. the base
if [ -f "$BASEPKG" ]; then
  if [ -n "${NM_BASE_PACKAGE_SHA256:-}" ]; then
    got=$(shasum -a 256 "$BASEPKG" | cut -d' ' -f1)
    [ "$got" = "$NM_BASE_PACKAGE_SHA256" ] || stop "$BASEPKG has SHA-256 $got, not NM_BASE_PACKAGE_SHA256=$NM_BASE_PACKAGE_SHA256"
  fi
  rm -rf "$PKG/base"; mkdir -p "$PKG/base"
  tar -xzf "$BASEPKG" -C "$PKG/base" || stop "could not unpack $BASEPKG"
  B=$(find "$PKG/base" -maxdepth 2 -type f -name install.sh | sed -n 1p)
  [ -n "$B" ] || stop "$BASEPKG holds no install.sh: not a release package"
  B=$(dirname "$B")
elif [ -d "$BASEPKG" ]; then
  B=$(cd "$BASEPKG" && pwd)
else
  stop "$BASEPKG is neither a file nor a folder"
fi
for f in install.sh SHA256SUMS nullmoth-runtime-check.sh Library/GPUBundles/nvmtl-allow.txt Library/GPUBundles/NVMTLDriver.bundle/Contents/Info.plist; do
  [ -e "$B/$f" ] || stop "the base package has no $f (it must be a NullMoth release package; see docs/BUILDING.md)"
done
( cd "$B" && shasum -a 256 --strict -c SHA256SUMS --quiet ) || stop "the base package does not match its own SHA256SUMS"
echo "base package: $BASEPKG ($(sed -n 1p "$B/VERSION" 2>/dev/null || echo 'no VERSION'))"

# 2. a fresh copy
R=$PKG/pkgroot
rm -rf "$R"; cp -pR "$B" "$R"
S=$R/SHA256SUMS
drop_lines() {   # <rel dir>: remove every SHA256SUMS line under it (both spellings)
  awk -v a="$1/" -v b="./$1/" '{ p = substr($0, 67) } !(index(p, a) == 1 || index(p, b) == 1)' "$S" > "$S.new" && mv -f "$S.new" "$S"
}
refresh_tree() {   # <rel dir>: a line for every regular file under it
  local list=() f
  while IFS= read -r f; do list+=( "$f" ); done < <(cd "$R" && find "$1" -type f | LC_ALL=C sort)
  [ ${#list[@]} -gt 0 ] || stop "$1 is empty"
  bash "$P/sums_refresh.sh" "$R" "${list[@]}"
}
put_file() {   # <src> <rel> [mode]
  [ -f "$1" ] || stop "$1 was not built"
  mkdir -p "$(dirname "$R/$2")"; cp -p "$1" "$R/$2"; chmod "${3:-644}" "$R/$2"
  bash "$P/sums_refresh.sh" "$R" "$2"; echo "replaced $2"
}
put_signed() {   # <src> <rel>: a Mach-O outside a kext, signed ad hoc (check_package.sh requires a signature)
  [ -f "$1" ] || stop "$1 was not built"
  mkdir -p "$(dirname "$R/$2")"; cp -p "$1" "$R/$2"; chmod 755 "$R/$2"
  codesign -f -s - "$R/$2" >/dev/null 2>&1 || stop "could not sign $2"
  bash "$P/sums_refresh.sh" "$R" "$2"; echo "replaced $2 (signed ad hoc)"
}
put_kext() {   # <src.kext> <rel.kext>: the whole bundle; signed ad hoc when the base's copy was signed
  [ -f "$1/Contents/Info.plist" ] || stop "$1 was not built"
  local signed=0
  [ -d "$R/$2/Contents/_CodeSignature" ] && signed=1
  drop_lines "$2"; rm -rf "${R:?}/$2"; mkdir -p "$(dirname "$R/$2")"; cp -pR "$1" "$R/$2"
  if [ "$signed" = 1 ]; then codesign --force --sign - "$R/$2" >/dev/null 2>&1 || stop "could not sign $2"; fi
  refresh_tree "$2"
  if [ "$signed" = 1 ]; then echo "replaced $2 (signed ad hoc, as the base one was)"; else echo "replaced $2"; fi
}

# 3. what this checkout builds
put_file "$P/install.sh" install.sh 755
put_file "$P/uninstall.sh" uninstall.sh 755
put_file "$P/fw_fetch.sh" fw_fetch.sh 755
for s in safari_gpu_copy.sh boost_daemon.sh dmc_fetch.sh dmc_pins.sh nm-dmc-load.sh; do
  if [ -f "$R/$s" ]; then put_file "$P/$s" "$s" 755; fi
done
D=$OUT/dlss-pkg
put_file "$D/dlss_fetch.sh" dlss_fetch.sh 755
put_file "$D/dlss/dlss_cache" dlss/dlss_cache 755
put_file "$D/dlss/nm_unzstd" dlss/nm_unzstd 755
put_file "$D/dlss/nm_unzstd.LICENSE" dlss/nm_unzstd.LICENSE
put_file "$D/dlss/assertfail_trap.ptx" dlss/assertfail_trap.ptx
put_kext "$OUT/kexts/nvrm/NVRM.kext" Library/Extensions/NVRM.kext
put_kext "$OUT/kexts/fb/NVRMFB.kext" Library/Extensions/NVRMFB.kext
put_kext "$OUT/kexts/agdc/NVRMAGDC.kext" Library/Extensions/NVRMAGDC.kext
put_kext "$OUT/kexts/accel/NVAccel.kext" Library/NullMoth/kexts/15/NVAccel.kext
put_kext "$OUT/kexts/accel26/NVAccel.kext" Library/NullMoth/kexts/26/NVAccel.kext
if [ -f "$OUT/selector/NVPrimarySelector.kext/Contents/Info.plist" ] && [ -d "$R/Library/Extensions/NVPrimarySelector.kext" ]; then
  put_kext "$OUT/selector/NVPrimarySelector.kext" Library/Extensions/NVPrimarySelector.kext
  codesign --force --sign - "$R/Library/Extensions/NVPrimarySelector.kext" >/dev/null 2>&1 || stop "could not sign NVPrimarySelector.kext"
  refresh_tree Library/Extensions/NVPrimarySelector.kext
fi
put_signed "$OUT/nvk/libvulkan_nouveau.dylib" Library/GPUBundles/nvmtl/libvulkan_nouveau.dylib
put_signed "$OUT/xlate/libnvmtl_translate.dylib" Library/GPUBundles/nvmtl/libnvmtl_translate.dylib
if [ -f "$R/Library/GPUBundles/nvmtl/libnvmtl_osver.dylib" ]; then
  put_signed "$OUT/osver/libnvmtl_osver.dylib" Library/GPUBundles/nvmtl/libnvmtl_osver.dylib
else
  echo "note: the base package has no Library/GPUBundles/nvmtl/libnvmtl_osver.dylib; the one built here is not added"
fi
# the plugin binary goes in before finish_package.sh, whose reseal refreshes it with the bundle's seal
put_file "$OUT/plugin/NVMTLDriver" Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver 755
if [ -d "$R/licenses" ]; then
  for f in "$HERE"/licenses/*; do if [ -f "$f" ]; then put_file "$f" "licenses/$(basename "$f")"; fi; done
fi
if [ -n "${NM_PACKAGE_VERSION:-}" ]; then
  printf '%s\n' "$NM_PACKAGE_VERSION" > "$R/VERSION"; bash "$P/sums_refresh.sh" "$R" VERSION; echo "VERSION $NM_PACKAGE_VERSION"
fi
if [ -n "${NM_BUILD_INFO:-}" ]; then put_file "$NM_BUILD_INFO" NULLMOTH-LOCAL-BUILD.txt; fi

# 4. the release's own last stage
RZ=$OUT/ryzenadj/ryzenadj
[ -f "$RZ" ] || stop "$RZ was not built"
RZSHA=$(shasum -a 256 "$RZ" | cut -d' ' -f1)
bash "$P/finish_package.sh" "$R" --nvencvt "$OUT/nvencvt/libnvencvt.dylib" --air-dis "$OUT/userland/nvmtl-air-dis" \
  --nmsmn "$OUT/kexts/nmsmn/NMSMN.kext" "$RZ" "$RZSHA" --boost "$OUT/boost/nvmtl-boost" --vulkan-loader "$OUT/vulkan-loader"

# 5. what install.sh will check first
( cd "$R" && shasum -a 256 --strict -c SHA256SUMS --quiet ) || stop "SHA256SUMS does not match the staged files"
bash "$R/sums_complete.sh" "$R" || stop "a staged file is not listed in SHA256SUMS"
T=$PKG/${NM_PACKAGE_NAME:-nullmoth-nvidia-local.tar.gz}
rm -f "$T"; ( cd "$PKG" && COPYFILE_DISABLE=1 tar -czf "$T" pkgroot ) || stop "could not write $T"
echo "package: $R"
echo "tarball: $T ($(shasum -a 256 "$T" | cut -d' ' -f1))"
