#!/bin/bash
# make_amfi_anchor.sh: create, on this machine only, a private code-signing chain and the matching codeless AMFIAnchor
# provider kext, so that graphics payloads signed with this chain can load into Apple platform processes (WindowServer)
# with AMFI enabled, instead of needing amfi_get_out_of_my_way=0x1 amfi=0x80 in boot-args.
#
#   bash tools/make_amfi_anchor.sh --amfianchor <AMFIAnchor checkout> [options]
#
# What it does, following https://github.com/b00t0x/AMFIAnchor/blob/main/Docs/Signing.md :
#   1. Generates a dedicated RSA chain (4096-bit root and intermediate, 2048-bit leaf), each carrying the Code Signing
#      EKU and the platform marker 1.2.840.113635.100.6.22, exactly the tested AMFIAnchor profile.
#   2. Runs the AMFIAnchor project's own tools/make_injector.py (from the checkout you pass) against the PUBLIC root
#      certificate to build a codeless anchor kext for OpenCore.
#   3. Prints the leaf certificate fingerprint for tools/sign_graphics.sh and tools/stage_signed_graphics.py.
#
# It NEVER writes a private key inside a git working tree, never uploads anything, and never reuses another project's
# signing identity. The keys stay in a local directory you control (default ~/nullmoth-signing). Only the public root
# certificate and the generated anchor kext are meant to leave this machine.
set -eu
umask 077

KEYDIR="${NULLMOTH_SIGNING_DIR:-$HOME/nullmoth-signing}"
AMFIANCHOR=""
BUNDLE_ID="org.theformidable.NVAnchorInjector"
ANCHOR_ID="org.theformidable.nvidia-signing"
ROOT_CN="NullMoth Fork Root CA"
IM_CN="NullMoth Fork Code Signing CA"
LEAF_CN="NullMoth Fork Software Signing"
FORCE=0

die() { echo "make_amfi_anchor: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --amfianchor) AMFIANCHOR=${2:?}; shift 2;;
    --keydir) KEYDIR=${2:?}; shift 2;;
    --bundle-id) BUNDLE_ID=${2:?}; shift 2;;
    --anchor-id) ANCHOR_ID=${2:?}; shift 2;;
    --force) FORCE=1; shift;;
    -h|--help) sed -n '2,33p' "$0"; exit 0;;
    *) die "unknown option: $1";;
  esac
done

command -v openssl >/dev/null || die "openssl not found"
[ -n "$AMFIANCHOR" ] || die "pass --amfianchor <path to a b00t0x/AMFIAnchor checkout> so the anchor kext is built by its own tool"
MKINJ="$AMFIANCHOR/tools/make_injector.py"
[ -f "$MKINJ" ] || die "$MKINJ not found (clone https://github.com/b00t0x/AMFIAnchor)"

# Refuse to drop private keys inside any git repository: keys must never be committed or pushed.
abs() { ( cd "$(dirname "$1")" 2>/dev/null && printf '%s/%s\n' "$(pwd -P)" "$(basename "$1")" ); }
probe=$KEYDIR
while :; do
  parent=$(dirname "$probe")
  [ -e "$probe/.git" ] && die "refusing to create keys inside a git repository ($probe/.git exists); choose a --keydir outside any checkout"
  [ "$parent" = "$probe" ] && break
  probe=$parent
done

if [ -e "$KEYDIR" ] && [ "$FORCE" != 1 ]; then
  [ -z "$(ls -A "$KEYDIR" 2>/dev/null)" ] || die "$KEYDIR is not empty; move it aside or pass --force to add to it"
fi
mkdir -p "$KEYDIR"
chmod 700 "$KEYDIR"

CNF="$KEYDIR/amfi-signing-ca.cnf"
cat > "$CNF" <<'PROFILE'
# AMFIAnchor-compatible code-signing profile (public, carries no key material).
[req]
prompt = no
distinguished_name = subject
[subject]
[root_ca]
basicConstraints = critical,CA:TRUE
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
keyUsage = critical,keyCertSign,cRLSign
[intermediate_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
keyUsage = critical,digitalSignature,keyCertSign,cRLSign
extendedKeyUsage = critical,codeSigning
1.2.840.113635.100.6.22 = DER:05:00
[code_signing]
basicConstraints = critical,CA:FALSE
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
1.2.840.113635.100.6.22 = DER:05:00
PROFILE

cd "$KEYDIR"

gen() {  # make a key only if it is not there, so --force can resume without discarding a chain already in use
  [ -s "$1" ] || openssl genpkey -algorithm RSA -pkeyopt "rsa_keygen_bits:$2" -out "$1" 2>/dev/null
}

echo "== generating the signing chain in $KEYDIR (private; keep it off GitHub)"
gen root.key.pem 4096
[ -s root.cert.pem ] || openssl req -new -x509 -sha256 -days 3650 -key root.key.pem \
  -subj "/CN=$ROOT_CN" -config "$CNF" -extensions root_ca -out root.cert.pem

gen intermediate.key.pem 4096
openssl req -new -sha256 -key intermediate.key.pem -subj "/CN=$IM_CN" -config "$CNF" -out intermediate.csr.pem
openssl x509 -req -sha256 -days 1825 -in intermediate.csr.pem -CA root.cert.pem -CAkey root.key.pem \
  -CAcreateserial -extfile "$CNF" -extensions intermediate_ca -out intermediate.cert.pem 2>/dev/null

gen leaf.key.pem 2048
openssl req -new -sha256 -key leaf.key.pem -subj "/CN=$LEAF_CN" -config "$CNF" -out leaf.csr.pem
openssl x509 -req -sha256 -days 365 -in leaf.csr.pem -CA intermediate.cert.pem -CAkey intermediate.key.pem \
  -CAcreateserial -extfile "$CNF" -extensions code_signing -out leaf.cert.pem 2>/dev/null

openssl verify -CAfile root.cert.pem -untrusted intermediate.cert.pem leaf.cert.pem >/dev/null \
  || die "the generated chain does not verify"

# The whole chain, root last, for rcodesign --pem-file inputs.
cat leaf.cert.pem intermediate.cert.pem root.cert.pem > chain.cert.pem
chmod 600 ./*.key.pem

ANCHOR_KEXT="$KEYDIR/${BUNDLE_ID##*.}.kext"
rm -rf "$ANCHOR_KEXT"
echo "== building the codeless anchor kext with AMFIAnchor's own tool"
python3 "$MKINJ" --certificate "$KEYDIR/root.cert.pem" \
  --bundle "$ANCHOR_KEXT" --bundle-id "$BUNDLE_ID" --anchor-id "$ANCHOR_ID" >/dev/null

LEAF_FP=$(openssl x509 -in leaf.cert.pem -noout -fingerprint -sha256 | sed 's/.*=//; s/://g' | tr 'A-F' 'a-f')

cat <<EOF

Done. Private keys and the chain live in:
  $KEYDIR        (do NOT commit, push, or copy to GitHub)

Public material to use:
  root certificate (public): $KEYDIR/root.cert.pem
  anchor kext for OpenCore:  $ANCHOR_KEXT
  leaf fingerprint (sha256): $LEAF_FP

Next:
  1) OpenCore: load Lilu.kext, AMFIAnchor.kext (from b00t0x/AMFIAnchor releases) and the anchor kext above,
     in that order; see docs/SIGNED-GRAPHICS-EXPERIMENT.md.
  2) Sign the graphics payloads of a finished package:
       bash tools/sign_graphics.sh --base <pkgroot> --keydir "$KEYDIR" --output <signed-GPUBundles>
  3) Import them into a new package and recompute its manifest:
       python3 tools/stage_signed_graphics.py --base <pkgroot> \\
         --signed-gpu <signed-GPUBundles> --leaf-fingerprint $LEAF_FP --output <pkgroot-signed>
EOF
