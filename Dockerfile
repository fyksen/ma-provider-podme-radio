# Music Assistant server with the PodMe provider baked in.
#
# Music Assistant only loads providers from inside its own package directory, so a
# bind mount is impossible under the Home Assistant add-on (the Supervisor owns the
# container definition). Shipping an image solves that for anyone running the server
# themselves: swap the image and the provider is simply there, and it survives
# restarts and recreation.
#
# Declared before FROM so it can be used in the base tag.
ARG MA_VERSION=latest
FROM ghcr.io/music-assistant/server:${MA_VERSION}

LABEL org.opencontainers.image.source="https://github.com/fyksen/ma-provider-podme-radio" \
      org.opencontainers.image.description="Music Assistant server with the PodMe podcast provider pre-installed. Unofficial community build, not affiliated with PodMe or Music Assistant." \
      org.opencontainers.image.licenses="Apache-2.0"

COPY podme/ /tmp/podme/

# The site-packages path carries the interpreter version (python3.14 at time of
# writing), and that moves between server releases - resolve it at build time rather
# than hardcoding a path that would silently stop being scanned after an upgrade.
RUN set -eu; \
    pyver=""; \
    for d in /app/venv/lib/python3.*/; do \
        [ -d "$d" ] || continue; \
        pyver=$(basename "$d"); \
        break; \
    done; \
    if [ -z "$pyver" ]; then \
        echo "could not find a python3.* directory under /app/venv/lib" >&2; \
        exit 1; \
    fi; \
    dst="/app/venv/lib/$pyver/site-packages/music_assistant/providers/podme"; \
    rm -rf "$dst"; \
    mv /tmp/podme "$dst"; \
    echo "installed podme into $dst"

# The server installs a provider's pinned requirements itself on first load, using uv
# (there is no pip in this venv). Doing it here instead means the first start needs no
# network round trip, and a PodMe sign-in cannot fail because the install did.
# Keep this pinned to the same version as manifest.json.
RUN /app/venv/bin/uv pip install --no-cache podme-api==1.4.3
