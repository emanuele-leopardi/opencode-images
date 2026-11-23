#!/usr/bin/env bash
# Render the opencode config from the bridge's live roster, then serve.
#
# The bridge owns the model list: it refreshes its allowlist from the Zen
# catalogue every BRIDGE_ALLOW_REFRESH seconds (15 min by default) and forces
# a refresh when it sees an id it does not know. Baking that list into the
# image would be stale the day Zen publishes a model, so ask at every start --
# and keep asking, because opencode watches this file and hot-reloads it. A
# roster change is therefore just a rewrite of opencode.json: same process, no
# restart, so a generation that is in flight is not interrupted.
set -euo pipefail

BRIDGE_URL="${BRIDGE_URL:-http://zen-bridge:8099/v1}"
# The default model is a pin, not a guess: this is what a fresh session uses.
# It is never overwritten; when Zen retires it, each render falls back to the
# first id in the roster instead, so the pin comes back if Zen restores it.
MODEL_DEFAULT="${MODEL_DEFAULT:-deepseek-v4.1-flash-free}"
# How often to re-ask the bridge for its roster. The bridge only changes its
# own allowlist every BRIDGE_ALLOW_REFRESH (900 s), so matching that is enough.
MODEL_REFRESH_SECONDS="${MODEL_REFRESH_SECONDS:-900}"
if ! [[ "$MODEL_REFRESH_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid MODEL_REFRESH_SECONDS='$MODEL_REFRESH_SECONDS'; using 900" >&2
  MODEL_REFRESH_SECONDS=900
fi
# Zen's catalogue includes ids that are not chat models (jev-1.13-free is a
# SystemOne decision model: the genuine client refuses it as a chat model).
# The bridge's allowlist is "worth attempting", not "servable in a chat loop";
# keep those ids out of the agent's model picker.
MODEL_DENYLIST="${MODEL_DENYLIST:-jev-1.13-free}"
CONFIG_DIR="${XDG_CONFIG_HOME:-/home/node/.config}/opencode"
CONFIG_FILE="$CONFIG_DIR/opencode.json"
# Same directory, so the swap is an atomic rename and the watcher only ever
# sees a complete file. The name does not end in .json, so it is never read as
# a config itself.
TMP_CONFIG="$CONFIG_DIR/.opencode.json.tmp"

mkdir -p "$CONFIG_DIR" \
         "${XDG_DATA_HOME:-/home/node/.local/share}/opencode" \
         "${XDG_STATE_HOME:-/tmp/state}/opencode" \
         "${XDG_CACHE_HOME:-/tmp/cache}"

log_config() {
  echo "agent config: $(jq -r '.provider.zen.models | length' "$CONFIG_FILE") model(s), default $(jq -r '.model' "$CONFIG_FILE")"
  # This is the only way to read the in-cluster config back: it is a file on
  # the pod's own filesystem, so RBAC cannot reach it, and exec/port-forward
  # are deliberately not granted. Nothing here is a secret -- the bridge URL,
  # the model ids and the permission matrix; the server password comes from a
  # Secret -- and `kubectl logs` already needs no extra permission.
  echo "--- $CONFIG_FILE (sha256 $(sha256sum "$CONFIG_FILE" | cut -d' ' -f1))"
  cat "$CONFIG_FILE"
}

# Render one config from the bridge's current roster and swap it in.
# $1 = fetch attempts (boot retries while the bridge starts; the refresh loop
# tries once and keeps the last good config on failure).
render_config() {
  local attempts="${1:-1}" i models="" model_json deny_json default cfg new_hash cur_hash

  for i in $(seq 1 "$attempts"); do
    if models=$(curl -fsS --max-time 5 "$BRIDGE_URL/models" 2>/dev/null); then
      break
    fi
    models=""
    if [ "$i" -lt "$attempts" ]; then sleep 2; fi
  done

  model_json=$(printf '%s' "$models" | jq -c '[.data[].id] | sort | unique' 2>/dev/null || true)
  if [ -n "$model_json" ] && [ "$model_json" != "null" ]; then
    deny_json=$(printf '%s' "$MODEL_DENYLIST" | jq -Rc 'split(",") | map(select(length > 0))')
    model_json=$(printf '%s' "$model_json" | jq -c --argjson deny "$deny_json" '. - $deny')
  fi

  if [ -z "$model_json" ] || [ "$model_json" = "null" ] || [ "$model_json" = "[]" ]; then
    if [ -f "$CONFIG_FILE" ]; then
      # A bridge outage must not shrink the picker: the ids already in the
      # served config may still be servable through the direct Zen provider.
      echo "bridge roster unavailable; keeping the config already being served" >&2
      return 0
    fi
    # Cold start with no bridge: a degraded boot beats a crashloop. The
    # refresh loop replaces this as soon as the bridge answers.
    echo "bridge roster unavailable at boot; starting with the pinned default only" >&2
    model_json=$(printf '["%s"]' "$MODEL_DEFAULT")
  fi

  # A pinned default that Zen retired must not be the first turn's 404.
  default="$MODEL_DEFAULT"
  if ! printf '%s' "$model_json" | jq -e --arg m "$default" 'index($m)' >/dev/null; then
    default=$(printf '%s' "$model_json" | jq -r '.[0]')
    echo "pinned default $MODEL_DEFAULT is not in the roster; using $default" >&2
  fi

  # Two providers, on purpose:
  #   zen      -- the bridge. The default: it owns Zen attestation, so the agent
  #               can keep upgrading its CLI without losing the free tier.
  #   opencode -- Zen direct through the built-in provider. It needs public
  #               HTTPS (the NetworkPolicy allows it) and buys two things: a
  #               fallback when the bridge is down, and models.dev's LIVE roster
  #               to compare against the bridge's to spot ids Zen has dropped.
  # Everything else -- notably the keyless `openai` provider -- stays hidden: it
  # has no credentials and only adds dead entries to the picker. Allowlisting
  # by name also means a future built-in provider does not leak in.
  #
  # webfetch is allowed on purpose: an agent that cannot open a page cannot
  # check a changelog, a CVE or an API doc. It is also the one tool that can
  # send bytes to a host the model names, so the NetworkPolicy does the limiting
  # instead of the tool: public HTTPS only, with the private, link-local and
  # CGNAT ranges excluded, so it can reach the internet but neither the
  # cluster, nor the LAN, nor the cloud instance-metadata endpoint.
  if ! cfg=$(jq -n \
      --arg base "$BRIDGE_URL" \
      --arg default "$default" \
      --argjson models "$model_json" \
      '{
        "$schema": "https://opencode.ai/config.json",
        "model": ("zen/" + $default),
        "enabled_providers": ["zen", "opencode"],
        "permission": {
          "read": {
            "*": "allow",
            "/var/run/secrets/*": "deny",
            "/proc/*/environ": "deny"
          },
          "glob": {
            "*": "allow",
            "/var/run/secrets/*": "deny",
            "/proc/*/environ": "deny"
          },
          "grep": {
            "*": "allow",
            "/var/run/secrets/*": "deny",
            "/proc/*/environ": "deny"
          },
          "edit": "deny",
          "write": "deny",
          "webfetch": "allow",
          "bash": {
            "*": "deny",
            "kubectl": "allow",
            "kubectl *": "allow"
          }
        },
        "provider": {
          "zen": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Zen free (via bridge)",
            "options": {"baseURL": $base, "apiKey": "not-needed"},
            "models": ($models | map({key: ., value: {name: .}}) | from_entries)
          }
        }
      }'); then
    echo "rendering the config failed; keeping the current one" >&2
    return 1
  fi

  new_hash=$(printf '%s\n' "$cfg" | sha256sum | cut -d' ' -f1)
  if [ -f "$CONFIG_FILE" ]; then
    cur_hash=$(sha256sum "$CONFIG_FILE" | cut -d' ' -f1)
    if [ "$new_hash" = "$cur_hash" ]; then
      echo "config unchanged ($new_hash)"
      return 0
    fi
  fi

  printf '%s\n' "$cfg" > "$TMP_CONFIG"
  mv -f "$TMP_CONFIG" "$CONFIG_FILE"
  echo "config changed; opencode hot-reloads it (no restart)"
  log_config
}

# First render: retry for ~60 s while the bridge starts, then boot anyway
# rather than crashloop.
render_config 30

# Keep the roster fresh without touching the server process.
( while sleep "$MODEL_REFRESH_SECONDS"; do render_config || true; done ) &

exec opencode serve --port "${AGENT_PORT:-4096}" --hostname 0.0.0.0
