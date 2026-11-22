#!/bin/sh
# Seed opencode's config into a WRITABLE dir, then start the bridge.
#
# opencode writes a .gitignore next to its config file on startup, so the
# config dir must be writable. The container runs with a read-only rootfs
# (hardening: the bridge executes a remote model, so prompt injection is a
# real risk), which is why the baked-in config under /etc is not usable
# directly. We copy it into the tmpfs-backed /tmp instead.
set -e

SEED=/etc/ocbridge-seed/opencode/opencode.json
TARGET="${BRIDGE_CONFIG_HOME:-/tmp/ocbridge}/opencode"

if [ ! -f "$TARGET" ]; then
  mkdir -p "$TARGET"
  cp "$SEED" "$TARGET/opencode.json"
  chmod 644 "$TARGET/opencode.json"
  echo "seeded $TARGET/opencode.json from $SEED"
fi

mkdir -p "${BRIDGE_WORK_ROOT:-/tmp/bridge-work}"

exec python3 /app/bridge.py