#!/bin/bash
# sign_graphics.sh: re-sign the three graphics units of a finished package with your private AMFIAnchor chain, so they
# can load under AMFI once the matching anchor is registered. Runs on your Mac, never as root, and changes nothing that
# is installed.
#
#   bash tools/sign_graphics.sh --base <pkgroot> --keydir <signing dir> --output <signed-GPUBundles>
#
# <pkgroot>   a FINISHED package tree (after finish_package.sh), e.g. ~/nullmoth-build/package/pkgroot
# --keydir    the directory tools/make_amfi_anchor.sh wrote (holds leaf.key.pem and chain.cert.pem)
# --output    a new directory; gets signed copies of NVMTLDriver.bundle, NVKZinkGLDriver.bundle and nvmtl at top level
#
# It copies each unit out of the package (symlinks preserved), signs every Mach-O with the leaf chain deepest-first,
# then seals the two bundles. Hand the result to tools/stage_signed_graphics.py with the leaf fingerprint.
set -eu
umask 022

BASE=""; KEYDIR=""; OUTPUT=""; RCODESIGN="${RCODESIGN:-rcodesign}"
die() { echo "sign_graphics: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE=${2:?}; shift 2;;
    --keydir) KEYDIR=${2:?}; shift 2;;
    --output) OUTPUT=${2:?}; shift 2;;
    --rcodesign) RCODESIGN=${2:?}; shift 2;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) die "unknown option: $1";;
  esac
done
[ "$(id -u)" != 0 ] || die "do not run as root"
[ -n "$BASE" ] && [ -n "$KEYDIR" ] && [ -n "$OUTPUT" ] || die "need --base, --keydir and --output"
command -v "$RCODESIGN" >/dev/null || die "$RCODESIGN not found (apple-codesign 0.29.0)"

GPU="Library/GPUBundles"
UNITS="NVMTLDriver.bundle NVKZinkGLDriver.bundle nvmtl"
for f in leaf.key.pem leaf.cert.pem intermediate.cert.pem root.cert.pem; do
  [ -s "$KEYDIR/$f" ] || die "$KEYDIR/$f is missing (run tools/make_amfi_anchor.sh first)"
done
for u in $UNITS; do [ -d "$BASE/$GPU/$u" ] || die "$BASE/$GPU/$u not found; pass a finished pkgroot"; done
[ ! -e "$OUTPUT" ] || die "$OUTPUT already exists; choose a new directory"

PEM="--pem-file $KEYDIR/leaf.key.pem --pem-file $KEYDIR/leaf.cert.pem --pem-file $KEYDIR/intermediate.cert.pem --pem-file $KEYDIR/root.cert.pem"

is_macho() {  # 0 if $1 starts with a Mach-O or fat magic
  m=$(head -c4 "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n') || return 1
  case "$m" in cffaedfe|cefaedfe|feedface|feedfacf|cafebabe|bebafeca|cafebabf|bfbafeca) return 0;; *) return 1;; esac
}

mkdir -p "$OUTPUT"
OUTABS=$(cd "$OUTPUT" && pwd -P)
for u in $UNITS; do
  # ditto keeps symlinks as symlinks and copies resource data unchanged, which the importer requires.
  ditto "$BASE/$GPU/$u" "$OUTABS/$u"
  # Sign every Mach-O deepest-first so a bundle seal below covers already-signed nested code.
  find "$OUTABS/$u" -type f | awk '{print gsub(/\//,"/"), $0}' | sort -rn | cut -d' ' -f2- | while IFS= read -r f; do
    is_macho "$f" || continue
    # shellcheck disable=SC2086
    "$RCODESIGN" sign -C /dev/null --timestamp-url none $PEM "$f" >/dev/null 2>&1 \
      || die "could not sign ${f#"$OUTABS/"}"
  done
  case "$u" in
    *.bundle)
      # shellcheck disable=SC2086
      "$RCODESIGN" sign -C /dev/null --timestamp-url none $PEM "$OUTABS/$u" >/dev/null 2>&1 \
        || die "could not seal $u"
      "$RCODESIGN" verify "$OUTABS/$u" >/dev/null 2>&1 || die "$u does not verify after sealing";;
  esac
done

LEAF_FP=$(openssl x509 -in "$KEYDIR/leaf.cert.pem" -noout -fingerprint -sha256 | sed 's/.*=//; s/://g' | tr 'A-F' 'a-f')
echo "Signed graphics units written to: $OUTABS"
echo "Import them with:"
echo "  python3 tools/stage_signed_graphics.py --base $BASE \\"
echo "    --signed-gpu $OUTABS --leaf-fingerprint $LEAF_FP --output <pkgroot-signed>"
