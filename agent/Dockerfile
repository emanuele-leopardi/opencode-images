FROM node:24-slim

# The agent is the opencode SERVER; the zen-bridge next to it is the genuine
# client that talks to Zen. Only the bridge needs the client version Zen
# attests, so this image is free to track the newest CLI.
ARG OPENCODE_CLI_VERSION=2.2.7
# Oracle clusters are arm64 (Ampere), htpc is amd64: build multi-arch.
ARG KUBECTL_VERSION=v1.37.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl jq git \
 && rm -rf /var/lib/apt/lists/*

RUN arch="$(dpkg --print-architecture)" \
 && curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
 && chmod +x /usr/local/bin/kubectl \
 && kubectl version --client

RUN npm install -g @opencode/cli@${OPENCODE_CLI_VERSION}

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# The agent must never reach Zen directly or update itself: its only
# upstream is the bridge, which owns attestation and the roster.
ENV OPENCODE_CLI_VERSION=${OPENCODE_CLI_VERSION} \
    BRIDGE_URL=http://zen-bridge:8099/v1 \
    MODEL_DEFAULT=deepseek-v4.1-flash-free \
    MODEL_DENYLIST=jev-1.13-free \
    AGENT_PORT=4096 \
    HOME=/home/node \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_MODELS_FETCH=1 \
    XDG_CONFIG_HOME=/home/node/.config \
    XDG_DATA_HOME=/home/node/.local/share \
    XDG_STATE_HOME=/tmp/state \
    XDG_CACHE_HOME=/tmp/cache

USER 1000:1000
WORKDIR /home/node
EXPOSE 4096
ENTRYPOINT ["/app/entrypoint.sh"]
