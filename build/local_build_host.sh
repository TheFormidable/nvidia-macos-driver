#!/bin/bash
# local_build_host.sh: ssh, rsync and scp for a "build Mac" that is this Mac.
# build_plugin_release.sh, build_xlate_release.sh and build_nvencvt.sh send their compile to the ssh host
# $NULLMOTH_BUILD_HOST and copy files there and back with rsync and scp. ./build.sh runs them unchanged on the tester's
# own Mac: it links this file as ssh, rsync and scp in a folder it puts first on PATH and sets NULLMOTH_BUILD_HOST to
# the name below. For that host only:
#   ssh <host> <command>        runs <command> here with /bin/bash -c, in a login-like environment (env -i: HOME, USER,
#                               LOGNAME, TMPDIR, LANG, PATH=$NM_LOCAL_HOST_PATH, and DEVELOPER_DIR / CARGO_NET_OFFLINE
#                               when set), as ssh would on a build Mac; no ssh login or Remote Login is needed
#   rsync -a <dir>/ <host>:<dir>/   copies the folder's contents (the one form the scripts use)
#   scp [-q] <host>:<file>... <dir>/   copies the files
# Any other host or form is refused (exit 255, as ssh fails), so the scripts never reach a real machine by mistake.
set -u
me=${0##*/}
HOST=${NM_LOCAL_BUILD_HOST:-nullmoth-local-build}
die() { echo "local_build_host.sh ($me): $*" >&2; exit 255; }
local_path() {   # <host>:<path> -> <path>, refusing any other host
  case "$1" in
    "$HOST":*) printf '%s\n' "${1#"$HOST":}" ;;
    *:*) die "refusing '$1': only $HOST (this Mac) is served here" ;;
    *) printf '%s\n' "$1" ;;
  esac
}
case "$me" in
  ssh)
    [ $# -ge 2 ] || die "usage: ssh <host> <command>"
    [ "$1" = "$HOST" ] || die "refusing host '$1': only $HOST (this Mac) is served here"
    shift
    cmd="$*"   # ssh joins its arguments with spaces before the remote shell reads them
    envs=( "HOME=$HOME" "USER=${USER:-}" "LOGNAME=${LOGNAME:-${USER:-}}" "SHELL=/bin/bash" "TMPDIR=${TMPDIR:-/tmp}"
           "LANG=${LANG:-en_US.UTF-8}" "PATH=${NM_LOCAL_HOST_PATH:-/usr/bin:/bin:/usr/sbin:/sbin}" )
    [ -n "${DEVELOPER_DIR:-}" ] && envs+=( "DEVELOPER_DIR=$DEVELOPER_DIR" )
    [ -n "${CARGO_NET_OFFLINE:-}" ] && envs+=( "CARGO_NET_OFFLINE=$CARGO_NET_OFFLINE" )
    [ -n "${PYTHONDONTWRITEBYTECODE:-}" ] && envs+=( "PYTHONDONTWRITEBYTECODE=$PYTHONDONTWRITEBYTECODE" )
    exec /usr/bin/env -i "${envs[@]}" /bin/bash -c "$cmd"
    ;;
  rsync)
    [ $# = 3 ] && [ "$1" = -a ] || die "only 'rsync -a <dir>/ <host>:<dir>/' is supported (got: $*)"
    src=$(local_path "$2") || exit 255; dst=$(local_path "$3") || exit 255
    case "$src" in */) ;; *) die "the source must end in / (copy the folder's contents): $2" ;; esac
    [ -d "$src" ] || die "$src is not a folder"
    mkdir -p "$dst" && cp -pR "$src". "$dst" || die "copy of $src to $dst failed"
    ;;
  scp)
    [ "${1:-}" = -q ] && shift
    [ $# -ge 2 ] || die "usage: scp [-q] <host>:<file>... <dir>/"
    n=$#; i=0; files=()
    for a in "$@"; do
      i=$((i + 1))
      if [ $i -lt $n ]; then f=$(local_path "$a") || exit 255; files+=( "$f" ); else dst=$(local_path "$a") || exit 255; fi
    done
    [ -d "$dst" ] || die "$dst is not a folder"
    for f in "${files[@]}"; do cp -p "$f" "$dst/" || die "copy of $f failed"; done
    ;;
  *) die "run this as ssh, rsync or scp (./build.sh links it so)" ;;
esac
