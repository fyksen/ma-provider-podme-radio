#!/usr/bin/env sh
#
# Install the PodMe provider into a running Music Assistant container.
#
# Written for Home Assistant OS / Supervised, where the server runs as an add-on and
# the Supervisor owns the container definition, so a bind mount is not possible. The
# provider files have to be copied inside the container instead.
#
# Needs a shell with host Docker access. On Home Assistant OS that means the
# "Advanced SSH & Web Terminal" community add-on with Protection mode OFF; the
# official "Terminal & SSH" add-on is sandboxed and cannot reach Docker.
#
#   curl -fsSL https://raw.githubusercontent.com/fyksen/ma-provider-podme-radio/main/scripts/install_provider.sh | sh
#
# Re-running upgrades in place. Pass flags through a pipe with `sh -s --`:
#   curl -fsSL .../install_provider.sh | sh -s -- --force
#
set -eu

REPO_OWNER="fyksen"
REPO_NAME="ma-provider-podme-radio"
PROVIDER_DIR="podme"
PROVIDER_LABEL="PodMe"
BRANCH="main"

CONTAINER=""
SOURCE_DIR=""
FORCE=0
RESTART=1

usage() {
    cat <<EOF
Install the $PROVIDER_LABEL provider into a running Music Assistant container.

Options:
  --container NAME     Target container (default: autodetected)
  --source DIR         Install from a local directory holding $PROVIDER_DIR/
                       (default: alongside this script, else downloaded)
  --repo-owner OWNER   Download from this GitHub owner (default: $REPO_OWNER)
  --branch REF         Download this branch or tag (default: $BRANCH)
  --force              Overwrite without asking
  --no-restart         Leave the container running the old code
  -h, --help           Show this help
EOF
}

log()  { echo "==> $*"; }
warn() { echo "warning: $*" >&2; }
die()  { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --container)   CONTAINER="${2:?--container needs a value}"; shift 2 ;;
        --source)      SOURCE_DIR="${2:?--source needs a value}"; shift 2 ;;
        --repo-owner)  REPO_OWNER="${2:?--repo-owner needs a value}"; shift 2 ;;
        --branch)      BRANCH="${2:?--branch needs a value}"; shift 2 ;;
        --force)       FORCE=1; shift ;;
        --no-restart)  RESTART=0; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             die "unknown option: $1 (try --help)" ;;
    esac
done

command -v docker >/dev/null 2>&1 || die "the 'docker' command was not found.
This script needs host Docker access. On Home Assistant OS, use the
'Advanced SSH & Web Terminal' add-on with Protection mode OFF - the official
'Terminal & SSH' add-on is sandboxed and cannot reach Docker."

# --- find the Music Assistant container -------------------------------------
# The Supervisor renamed its prefix from addon_ to app_, so accept either, plus the
# plain names used by standalone Docker installs.
if [ -z "$CONTAINER" ]; then
    CONTAINER=$(docker ps --format '{{.Names}}' \
        | grep -E '^(addon|app)_[0-9a-f]+_music_assistant(_beta|_nightly|_dev)?$' \
        | head -n1) || true
fi
if [ -z "$CONTAINER" ]; then
    CONTAINER=$(docker ps --format '{{.Names}}' \
        | grep -E '^music[-_]assistant' | head -n1) || true
fi
[ -n "$CONTAINER" ] || die "could not find a running Music Assistant container.
Start the server and try again, or name it with --container NAME.
Running containers:
$(docker ps --format '  {{.Names}}')"
log "Music Assistant container: $CONTAINER"

# --- locate the provider source ---------------------------------------------
CLEANUP=""
# shellcheck disable=SC2064
trap 'if [ -n "$CLEANUP" ]; then rm -rf "$CLEANUP"; fi' EXIT INT TERM

if [ -z "$SOURCE_DIR" ]; then
    script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd 2>/dev/null) || script_dir=""
    if [ -n "$script_dir" ] && [ -f "$script_dir/../$PROVIDER_DIR/manifest.json" ]; then
        SOURCE_DIR=$(CDPATH='' cd -- "$script_dir/.." && pwd)
        log "using the checkout this script lives in: $SOURCE_DIR"
    fi
fi

if [ -z "$SOURCE_DIR" ]; then
    command -v curl >/dev/null 2>&1 || die "need curl to download the provider"
    tmp=$(mktemp -d)
    CLEANUP="$tmp"
    url="https://codeload.github.com/$REPO_OWNER/$REPO_NAME/tar.gz/refs/heads/$BRANCH"
    log "downloading $REPO_OWNER/$REPO_NAME@$BRANCH"
    curl -fsSL "$url" -o "$tmp/src.tar.gz" \
        || die "download failed: $url
Check --repo-owner and --branch, or pass --source with a local checkout."
    tar -xzf "$tmp/src.tar.gz" -C "$tmp" || die "could not unpack the download"
    SOURCE_DIR=$(find "$tmp" -maxdepth 1 -type d -name "$REPO_NAME-*" | head -n1)
    [ -n "$SOURCE_DIR" ] || die "unexpected archive layout"
fi

SRC="$SOURCE_DIR/$PROVIDER_DIR"
[ -f "$SRC/manifest.json" ] || die "no $PROVIDER_DIR/manifest.json under $SOURCE_DIR"

# --- resolve the destination inside the container ---------------------------
# site-packages carries the interpreter version, and that moves between releases -
# detect it rather than hardcoding a path that quietly stops being scanned.
PYVER=$(docker exec "$CONTAINER" sh -c 'ls /app/venv/lib 2>/dev/null' \
    | grep -m1 '^python3') || true
[ -n "$PYVER" ] || die "could not find a python3.* directory in $CONTAINER.
Is this really a Music Assistant container?"
DST_PARENT="/app/venv/lib/$PYVER/site-packages/music_assistant/providers"
DST="$DST_PARENT/$PROVIDER_DIR"
log "destination: $DST"

# A bind mount at the destination means the provider is already installed the
# standalone way; copying over it fails with a bare "marked read-only" from Docker.
if docker exec "$CONTAINER" sh -c "grep -q ' ${DST} ' /proc/self/mounts" 2>/dev/null; then
    die "$DST is a bind mount inside $CONTAINER.
That means the provider is already installed by mounting it from the host, which is
the better setup - this script is for Home Assistant add-ons, where mounting is not
possible. Edit the mount source on the host instead, then restart the container."
fi

if docker exec "$CONTAINER" test -e "$DST" 2>/dev/null; then
    if [ "$FORCE" -eq 0 ] && [ -t 0 ]; then
        printf "%s already exists in the container. Overwrite? [y/N] " "$PROVIDER_DIR"
        read -r answer
        case "$answer" in
            [yY]*) ;;
            *) die "aborted" ;;
        esac
    elif [ "$FORCE" -eq 0 ]; then
        die "$PROVIDER_DIR already exists in the container; re-run with --force to overwrite.
Through a pipe that is: curl -fsSL ... | sh -s -- --force"
    fi
    docker exec "$CONTAINER" rm -rf "$DST"
fi

# --- install ----------------------------------------------------------------
docker cp "$SRC" "$CONTAINER:$DST" || die "docker cp failed"
docker exec "$CONTAINER" sh -c "rm -rf '$DST/__pycache__'" 2>/dev/null || true
log "copied $PROVIDER_DIR into $CONTAINER"

if [ "$RESTART" -eq 1 ]; then
    log "restarting $CONTAINER"
    # docker restart, NOT the Home Assistant UI: restarting the add-on from HA
    # recreates the container from its image and discards what we just copied.
    docker restart "$CONTAINER" >/dev/null || die "could not restart $CONTAINER"
    log "restarted"
fi

cat <<EOF

Done. Add it in Music Assistant under:
  Settings -> Music Providers -> Add Provider -> $PROVIDER_LABEL

You will be asked for your PodMe e-mail, password and region. On first load the
server installs this provider's podme-api dependency itself, so give it a moment.

Note: this lives in the container's writable layer, so it is lost whenever the
container is recreated - an add-on update, or restarting the add-on from the Home
Assistant UI. Re-run this script after those. Use 'docker restart $CONTAINER' when
you just need a restart, which preserves it.
EOF
