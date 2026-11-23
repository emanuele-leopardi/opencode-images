# opencode-agent — per-cluster, independent of the host

A headless `opencode serve` inside each Kubernetes cluster, next to its own
`zen-bridge`. It gives every cluster a self-contained agent for read-only
inspection and upgrades, with its **own Zen free-tier quota** (different
egress IP / subnet, so one cluster's usage does not drain another's).

```
            cluster (namespace opencode)
  ┌──────────────────────────────────────────────┐
  │  opencode-agent  ──HTTP──▶  zen-bridge  ──▶ Zen │
  │  (server :4096)             (:8099)          │
  │       │ SA opencode-readonly (view)          │
  │       ▼                                      │
  │   Kubernetes API (read-only)                 │
  └──────────────────────────────────────────────┘
```

The bridge is the genuine opencode client that Zen attests; the agent is just
an OpenAI-compatible consumer. This is why the agent can run any CLI version
and still reach the free tier.

## Images

Built from this directory, pushed to GHCR:

| image | Dockerfile | arch |
|---|---|---|
| `ghcr.io/emanuele-leopardi/opencode-agent:<date>` | `agent/Dockerfile` | linux/arm64 + linux/amd64 |
| `ghcr.io/emanuele-leopardi/zen-bridge:<date>` | `zen-bridge/Dockerfile` | linux/arm64 + linux/amd64 |

On the host (Docker Buildx, not inside the agent container):

```sh
cd /home/emanuele/Documents/bin/llm
TAG=2026-11-21
docker buildx build --platform linux/arm64,linux/amd64 \
  -t ghcr.io/emanuele-leopardi/opencode-agent:$TAG --push agent
docker buildx build --platform linux/arm64,linux/amd64 \
  -t ghcr.io/emanuele-leopardi/zen-bridge:$TAG --push zen-bridge
```

`--build-arg OPENCODE_CLI_VERSION=<x.y.z>` overrides the agent pin.
`--build-arg OPENCODE_CLI_VERSION=<x.y.z>` on `zen-bridge` changes the
attested client (keep it at the version verified against the current free
roster; `/health` reports it).

## Model freshness

Nothing bakes the free roster into the agent:

1. The bridge refreshes its allowlist from `https://opencode.ai/zen/v1/models`
   every `BRIDGE_ALLOW_REFRESH` seconds (900) and forces a refresh when it
   sees an unknown id.
2. `entrypoint.sh` asks the bridge's `/v1/models` at pod start and renders
   `opencode.json` with exactly those ids, minus `MODEL_DENYLIST` (default
   `jev-1.13-free`, a SystemOne decision model the chat client refuses). A
   pinned `MODEL_DEFAULT` that no longer exists is replaced by the first
   available id.
3. A 6-hourly CronJob (`refresh.yaml`) asks the **running agent** for its own
   `/api/model` list and hashes it. When the hash differs from the
   `opencode-roster` ConfigMap, the ConfigMap is updated and the agent pod is
   deleted; the Deployment recreates it with a freshly rendered config. The
   bridge's `/v1/models` is the fallback when the agent is not answering.

The agent is hashed, not the bridge, on purpose: the image also fetches
`models.dev` at pod start, so a new free id can appear there without the
bridge ever hearing about it. Hashing the agent's own list is the honest one —
it is what the running server would actually offer.

The CronJob deletes the pod instead of patching the Deployment on purpose:
Flux owns the Deployment and would revert a runtime annotation within its
1-minute reconcile loop, causing a rollout every cycle. It needs one secret
read (`opencode-agent` → `password`) to ask the agent for its list.

## Security model

- The bridge is keyless and has no cluster credentials
  (`automountServiceAccountToken: false`), a read-only rootfs, all
  capabilities dropped, `no-new-privileges`, and `/tmp` as an `emptyDir`.
- The agent runs as uid 1000 with a read-only rootfs. Its only writable
  paths are `/tmp`, `/home/node/.config` (rendered each start), `/home/node`
  (scratch cwd), and the PVC at `/home/node/.local/share` (sessions DB).
- The agent's bash permission is `*: deny` plus `kubectl` / `kubectl *:
  allow`. `edit`, `write`, and `webfetch` are denied. Cluster changes remain
  GitOps + human-approved.
- `OPENCODE_SERVER_PASSWORD` comes from the `opencode-agent` Secret. The
  server is reachable only by port-forward (or a deliberate Service/Ingress);
  without the password it answers 401.
- NetworkPolicy: only the agent and the refresh CronJob may reach the bridge;
  the bridge may reach DNS and public 443 only; the agent may reach DNS, the
  bridge, the Kubernetes API, and public 443 (Zen-direct + models.dev: the
  fallback path and the live roster). Private ranges stay excluded.

## Deploy (per cluster)

Manifests live in `home-clusters/clusters/<cluster>/apps/opencode/`:

```sh
# 1. Secret (never in git; the Deployment waits for it)
kubectl -n opencode create secret generic opencode-agent \
  --from-literal=password="$(openssl rand -base64 32)"

# 2. Flux applies the rest once the image exists
flux reconcile kustomization apps --with-source
```

For a SealedSecret cluster, seal the same key on the host and commit it
instead of creating the Secret by hand.

## Verify

```sh
kubectl -n opencode get pods
kubectl -n opencode port-forward svc/zen-bridge 8099:8099
curl -s localhost:8099/health | jq '{ok,cli,models,allowlist_checked}'

kubectl -n opencode port-forward svc/opencode-agent 4096:4096
# opencode web UI / API: basic auth, user "opencode", password from the Secret
curl -s -u "opencode:$PASSWORD" localhost:4096/api/config | jq '.[0].info.model'
```

Expected: `cli` matches the bridge build arg, `models` is the current free
roster, and the agent config's model count equals the bridge's minus the
denylist.

## Notes

- Models are fetched live from `models.dev` (no `OPENCODE_DISABLE_MODELS_FETCH`):
  the built-in `opencode` provider then shows Zen's current roster and works as
  a fallback when the bridge is down. A fetch failure falls back to the bundled
  snapshot. This needs public HTTPS egress from the agent.
- `OPENCODE_DISABLE_AUTOUPDATE=1`: the image tag is the unit of upgrade.
- The agent's PVC is `local-path`; it is node-local and survives pod restarts,
  not node loss. Sessions are not cluster state worth more than that.
