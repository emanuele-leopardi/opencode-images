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

Built by CI in the public repo `emanuele-leopardi/opencode-images` (multi-arch
Buildx → GHCR) with the tag `<YYYY-MM-DD>.<run>`, which is what the clusters
pin. This directory holds the same sources; build here only as an offline
fallback (Docker Buildx, not inside the agent container), pushing to GHCR:

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
   `jev-1.13-free`, a SystemOne decision model the chat client refuses).
3. The same script then re-renders every `MODEL_REFRESH_SECONDS` (900),
   **inside the pod**. If the roster is unchanged nothing is written; if it
   moved, the file is replaced with an atomic `mv` and opencode hot-reloads it
   in ~0.4 s — same PID, so a session that is generating is not interrupted.
4. A bridge outage during the loop keeps the config already being served, so a
   blip cannot shrink the picker. Only a first start falls back to
   `MODEL_DEFAULT` alone (then recovers on the next successful render).

`MODEL_DEFAULT` is a pin: it is never overwritten. If Zen retires it, each
render falls back to the first id in the roster *for that render only*, so the
pin comes back if Zen restores it. A pinned default that no longer exists must
not be the first turn's 404.

There is **no CronJob and no pod deletion**. The old 6-hourly refresh deleted
the agent pod to force a re-render; with the config hot-reloaded, that would
throw away in-flight work for nothing. Each render that changes the file prints
the full JSON to the container log, which is how the served config can be read
back without exec (`kubectl logs` is already read-only).

## Security model

- The bridge is keyless and has no cluster credentials
  (`automountServiceAccountToken: false`), a read-only rootfs, all
  capabilities dropped, `no-new-privileges`, and `/tmp` as an `emptyDir`.
- The agent runs as uid 1000 with a read-only rootfs. Its only writable
  paths are `/tmp`, `/home/node/.config` (rendered each start), `/home/node`
  (scratch cwd), and the PVC at `/home/node/.local/share` (sessions DB).
- The agent's bash permission is `*: deny` plus `kubectl` / `kubectl *:
  allow`. `edit` and `write` are denied; `webfetch` is **allowed** (an agent
  that cannot open a page cannot check a changelog or a CVE) and is limited by
  the NetworkPolicy instead: public HTTPS only, no port 80, with private,
  link-local and CGNAT ranges excluded, so it cannot reach cluster services,
  the LAN or the cloud metadata endpoint. Cluster changes remain GitOps +
  human-approved.
- `OPENCODE_SERVER_PASSWORD` comes from the `opencode-agent` Secret. The
  server is reachable only by port-forward (or a deliberate Service/Ingress);
  without the password it answers 401.
- NetworkPolicy: only the agent may reach the bridge; the bridge may reach DNS
  and public 443 only; the agent may reach DNS, the bridge, the Kubernetes API,
  and public 443 (Zen-direct, models.dev and webfetch: the fallback path, the
  live catalogue and the fetch tool). Private, link-local and CGNAT ranges stay
  excluded, and port 80 is not allowed.

## Deploy (per cluster)

Manifests live in `home-clusters`, at
`clusters/<cluster>/apps/{opencode,local/opencode}/` (`local/` on the Oracle
clusters, top level on `htpc`):

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
