# opencode-images

Build and publish the two container images the **per-cluster opencode agents**
run, so they are maintained by CI instead of a hand-run `docker buildx` on a
laptop.

| image | context | used by |
|---|---|---|
| `ghcr.io/emanuele-leopardi/opencode-agent` | `agent/` | the headless `opencode serve` in each cluster |
| `ghcr.io/emanuele-leopardi/zen-bridge` | `zen-bridge/` | the opencode client the bridge drives on the cluster's behalf |

## How it builds

`.github/workflows/build-images.yml` runs on every push to `main` that touches
`agent/**` or `zen-bridge/**`, and can be dispatched by hand with an explicit
tag. It builds **multi-arch** (`linux/amd64,linux/arm64`) with Buildx + QEMU and
pushes to GHCR using only the automatic `GITHUB_TOKEN` (`packages: write`) — no
repository secret needed.

Tags published per build:

| tag | meaning |
|---|---|
| `<YYYY-MM-DD>` | immutable date tag — **this is what the clusters pin** |
| `sha-<short>` | the commit it was built from |
| `latest` | most recent build |

Hand-dispatch with a tag when you want to reproduce an older date:

```
Actions → build-images → Run workflow → tag: 2026-11-21
```

## Consuming it from the clusters

The manifests live in `home-clusters`, e.g.
`clusters/oracle-ema/apps/local/opencode/{agent,bridge}.yaml`. They reference
`ghcr.io/emanuele-leopardi/{opencode-agent,zen-bridge}:<date>`, so after each
build bump that tag and push:

```
# example: move ema to the image built on 2026-11-22
sed -i 's#\(opencode-agent\|zen-bridge\):2026-11-21#\1:2026-11-22#' \
  clusters/oracle-ema/apps/local/opencode/{agent,bridge,refresh}.yaml
```

### Packages are pulled anonymously

The cluster manifests carry no `imagePullSecrets`, so the pods pull GHCR
without credentials. This repository is **public**, and GHCR packages pushed
from a public repo inherit public visibility — so the two packages
(`opencode-agent`, `zen-bridge`) are pullable by the clusters as-is, and the
first successful build makes them show up under *Packages*.

(If the repo is ever made private, either set each package to public under
*Package settings → Change visibility*, or add an `imagePullSecret` with a
`read:packages` token to the clusters' `opencode` namespace.)

## Relationship to `llm/agent` and `llm/zen-bridge`

These Dockerfiles and sources are the **same** ones the `llm` compose stack
builds locally (`llm/docker-compose.yaml`), copied here so a repo with a
remote can own the images. `llm` has no git remote, which is why this
duplication exists.

When you change the agent or the bridge, update it here and commit; CI will
rebuild. If you also keep the `llm` copies in play, treat this repo as the
source of truth for the published images and keep them in sync:

```
diff -r agent       /home/node/bin/llm/agent
diff -r zen-bridge  /home/node/bin/llm/zen-bridge
```

## Build args

| arg | default | image |
|---|---|---|
| `OPENCODE_CLI_VERSION` | see the Dockerfile | both |

`zen-bridge` additionally pins `opencode-ai==0.1.0a36` via pip inside its
Dockerfile (needed for the warm pool; the container runs with the pool
disabled). `/health` on the bridge reports the attested client version.
