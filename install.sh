#!/usr/bin/env bash
#
# Installs the PodMe provider into a Music Assistant server.
#
# Music Assistant loads providers only from inside its own package directory
# (PROVIDERS_PATH is hardcoded), so installing means getting this directory to
# music_assistant/providers/podme. The awkward part is that the path contains the
# image's Python version, which changes between releases - this script finds it.
#
# The podme-api dependency is installed by Music Assistant itself: it is pinned in
# manifest.json and pulled in when the provider first loads.
#
#   ./install.sh                  detect a running container and print the volume line
#   ./install.sh --copy           copy into the running container (until it is recreated)
#   ./install.sh --venv <path>    install into a source checkout / venv
#
set -euo pipefail

CONTAINER="${MA_CONTAINER:-music-assistant-server}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/podme"

die() { echo "error: $*" >&2; exit 1; }

[ -f "$SRC/manifest.json" ] || die "podme/manifest.json not found next to this script"

providers_path_in_container() {
  docker exec "$CONTAINER" python -c \
    'import music_assistant, os; print(os.path.join(os.path.dirname(music_assistant.__file__), "providers"))' \
    2>/dev/null || return 1
}

case "${1:-}" in
  --venv)
    [ -n "${2:-}" ] || die "--venv needs a path to the music_assistant package or its parent"
    target="$2"
    [ -d "$target/providers" ] || target="$target/music_assistant"
    [ -d "$target/providers" ] || die "no providers/ directory under $2"
    rm -rf "$target/providers/podme"
    cp -r "$SRC" "$target/providers/podme"
    echo "installed into $target/providers/podme"
    echo "restart Music Assistant, then add it under Settings -> Music Providers."
    ;;

  --copy)
    command -v docker >/dev/null || die "docker not found"
    docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || die "container '$CONTAINER' is not running"
    dest="$(providers_path_in_container)" || die "could not locate music_assistant inside '$CONTAINER'"
    docker cp "$SRC" "$CONTAINER:$dest/podme"
    docker restart "$CONTAINER" >/dev/null
    echo "copied into $CONTAINER:$dest/podme and restarted."
    echo
    echo "NOTE: this is lost the next time the container is recreated (docker compose up -d,"
    echo "      image pull, etc). Run without --copy to get a persistent bind mount instead."
    ;;

  ""|--help|-h)
    command -v docker >/dev/null || die "docker not found (use --venv for a source install)"
    if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
      echo "Container '$CONTAINER' is not running."
      echo "Set MA_CONTAINER=<name> if yours is named differently, or use:"
      echo "  ./install.sh --venv /path/to/music_assistant"
      exit 1
    fi
    dest="$(providers_path_in_container)" || die "could not locate music_assistant inside '$CONTAINER'"
    echo "Music Assistant providers directory in '$CONTAINER':"
    echo "  $dest"
    echo
    echo "Add this to the 'volumes:' section of your compose.yml, then 'docker compose up -d':"
    echo
    echo "      - $SRC:$dest/podme:ro"
    echo
    echo "Then add it under Settings -> Music Providers -> Add Provider -> PodMe,"
    echo "and sign in with your PodMe account."
    echo
    echo "The path contains the image's Python version, so re-run this after a major"
    echo "Music Assistant upgrade to check it still matches."
    ;;

  *)
    die "unknown option: $1 (try --help)"
    ;;
esac
