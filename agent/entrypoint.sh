#!/usr/bin/env bash
# Render the opencode config from the bridge's live roster, then serve.
#
# The bridge owns the model list: it refreshes its allowlist from the Zen
# catalogue every BRIDGE_ALLOW_REFRESH seconds (15 min by default) and forces
# a refresh when it sees an id it does not know. Baking that list into the
# image would be stale the day Zen publishes a model, so ask at every start.
set -euo pipefail

BRIDGE_URL="${BRIDGE_URL:-http://zen-bridge:8099/v1}"
MODEL_DEFAULT="${MODEL_DEFAULT:-deepseek-v4.1-flash-free}"
# Zen's catalogue includes ids that are not chat models (jev-1.13-free is a
# SystemOne decision model: the genuine client refuses it as a chat model).
# The bridge's allowlist is "worth attempting", not "servable in a chat loop";
# keep those ids out of the agent's model picker.
MODEL_DENYLIST="${MODEL_DENYLIST:-jev-1.13-free}"
CONFIG_DIR="${XDG_CONFIG_HOME:-/home/node/.config}/opencode"

mkdir -p "$CONFIG_DIR" \
         "${XDG_DATA_HOME:-/home/node/.local/share}/opencode" \
         "${XDG_STATE_HOME:-/tmp/state}/opencode" \
         "${XDG_CACHE_HOME:-/tmp/cache}"

# The bridge may still be starting (or warming its pool). Retry for ~60s,
# then boot anyway: the model refresh CronJob restarts this pod once the
# bridge answers, and a degraded start beats a crashloop.
models=""
for _ in $(seq 1 30); do
  if models=$(curl -fsS --max-time 5 "$BRIDGE_URL/models" 2>/dev/null); then
    break
  fi
  sleep 2
done

model_json=$(printf '%s' "$models" | jq -c '[.data[].id] | sort | unique' 2>/dev/null || true)
if [ -n "$model_json" ] && [ "$model_json" != "null" ]; then
  deny_json=$(printf '%s' "$MODEL_DENYLIST" | jq -Rc 'split(",") | map(select(length > 0))')
  model_json=$(printf '%s' "$model_json" | jq -c --argjson deny "$deny_json" '. - $deny')
fi
if [ -z "$model_json" ] || [ "$model_json" = "null" ] || [ "$model_json" = "[]" ]; then
  model_json=$(printf '["%s"]' "$MODEL_DEFAULT")
fi

# A pinned default that Zen retired must not be the first turn's 404.
if ! printf '%s' "$model_json" | jq -e --arg m "$MODEL_DEFAULT" 'index($m)' >/dev/null; then
  MODEL_DEFAULT=$(printf '%s' "$model_json" | jq -r '.[0]')
fi

# The bridge is the agent's ONLY upstream: expose no other provider.
# Without this the built-in `opencode` (Zen-direct) and `openai` providers
# stay enabled, so the model picker lists ~90 dead entries and 7 of the bridge
# ids appear twice (e.g. opencode/deepseek-v4.1-flash-free next to
# zen/deepseek-v4.1-flash-free). The agent's NetworkPolicy forbids public
# egress, so picking any built-in one fails with the misleading
# "ECONNREFUSED: Unable to connect. Is the computer able to access the url?"
# (reproduced with opencode v2.2.7). Allowlisting `zen` keeps the picker to
# exactly the bridge roster.
jq -n \
  --arg base "$BRIDGE_URL" \
  --arg default "$MODEL_DEFAULT" \
  --argjson models "$model_json" \
  '{
    "$schema": "https://opencode.ai/config.json",
    "model": ("zen/" + $default),
    "enabled_providers": ["zen"],
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
      "webfetch": "deny",
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
  }' > "$CONFIG_DIR/opencode.json"

echo "agent config: $(jq -r '.provider.zen.models | length' "$CONFIG_DIR/opencode.json") model(s), default $MODEL_DEFAULT"
exec opencode serve --port "${AGENT_PORT:-4096}" --hostname 0.0.0.0
