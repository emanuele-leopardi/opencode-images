FROM node:24-slim

# node:24-slim is DEBIAN-based -> apt, not apk. (An earlier revision of this
# file pasted the docker-gate's alpine `apk add` line and failed with
# "apk: not found" 2026-10-06.) The bridge needs no Docker CLI at all: it
# only drives the opencode client and serves HTTP.
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Python SDK for talking to the warm `opencode serve` pool. opencode-ai is
# published as pre-release alphas ONLY, so it must be pinned exactly or pip
# refuses it ("No matching distribution found for opencode-ai>=0.1").
# Without this the pool silently degrades to one-shot CLI calls (~5s each).
RUN pip install --no-cache-dir --break-system-packages \
      opencode-ai==0.1.0a36

# opencode CLI. The npm package moved at the 2.x cutover:
# opencode-ai (legacy line, ended at 1.18.35) -> @opencode/cli.
# The CATALOGUE is version-independent (a plain HTTPS GET of
# https://opencode.ai/zen/v1/models lists every id), and the allowlist
# refreshes itself from it -- but whether a model ANSWERS can depend on the
# client version, since Zen gates the free tier on the genuine client.
# 2.1.2 = the harness container's version, verified 2026-10-22: this
# container's own @opencode/cli@2.1.2 served glm-5.3-flash-free and
# beam-free ("OK") while the previous pin opencode-ai@1.18.35 got
# UnknownError 500 on the ENTIRE new free roster (beam, flint-40b,
# deepseek-v4.1-flash, glm-5.3-flash, minimax-m3.1-flash,
# nemotron-3.5-super-vl); legacy ids (big-pickle, mimo-v2.6-flash,
# nemotron-3.5-lightning) worked on both. jev-1.13-free is servable from
# NEITHER: it is absent from the @opencode/cli 2.1.2 roster ("Model
# unavailable" and no call placed) and appears in the raw catalogue only.
#
# The version is a build arg so a per-cluster rebuild can be triggered
# without editing this file, and /health reports it: the attested client
# version is the first thing to compare when every free id starts failing.
ARG OPENCODE_CLI_VERSION=2.1.2
RUN npm install -g @opencode/cli@${OPENCODE_CLI_VERSION}

# Seed location for the client config. The RUNTIME config dir is a tmpfs copy
# (see entrypoint.sh): opencode writes a .gitignore next to its config on
# startup, which a read-only rootfs forbids.
COPY ocbridge-opencode.json /etc/ocbridge-seed/opencode/opencode.json
COPY bridge.py /app/bridge.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV BRIDGE_PORT=8099 BRIDGE_MAX_CONCURRENT=2 BRIDGE_RUN_TIMEOUT=60 \
    BRIDGE_SEM_WAIT=90 BRIDGE_WORK_ROOT=/tmp/bridge-work \
    BRIDGE_CONFIG_HOME=/tmp/ocbridge \
    OPENCODE_CLI_VERSION=${OPENCODE_CLI_VERSION}

EXPOSE 8099
ENTRYPOINT ["/app/entrypoint.sh"]