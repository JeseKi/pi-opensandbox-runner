FROM node:24.4.1-bookworm-slim AS node

ARG PI_VERSION=0.82.1
ARG MIRROR_MODE=auto
RUN set -eux; \
    mirror_mode="$MIRROR_MODE"; \
    if [ "$mirror_mode" = "auto" ]; then \
        if node -e "const c = new AbortController(); setTimeout(() => c.abort(), 3000); fetch('https://www.google.com/generate_204', {signal: c.signal}).then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1));"; then \
            mirror_mode="global"; \
        else \
            mirror_mode="cn"; \
        fi; \
    fi; \
    case "$mirror_mode" in \
        cn) npm_registry="https://registry.npmmirror.com" ;; \
        global) npm_registry="https://registry.npmjs.org" ;; \
        *) echo "Unsupported MIRROR_MODE: $MIRROR_MODE"; exit 1 ;; \
    esac; \
    echo "Using npm mirror mode: $mirror_mode"; \
    npm config set registry "$npm_registry"; \
    npm install --global --ignore-scripts \
        "@earendil-works/pi-coding-agent@${PI_VERSION}"

FROM python:3.13.5-slim-bookworm

ARG MIRROR_MODE=auto
ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/root \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin

RUN set -eux; \
    mirror_mode="$MIRROR_MODE"; \
    if [ "$mirror_mode" = "auto" ]; then \
        if python -c "import urllib.request; urllib.request.urlopen('https://www.google.com/generate_204', timeout=3).close()"; then \
            mirror_mode="global"; \
        else \
            mirror_mode="cn"; \
        fi; \
    fi; \
    case "$mirror_mode" in \
        cn) \
            . /etc/os-release; \
            printf 'Types: deb\nURIs: https://mirrors.tuna.tsinghua.edu.cn/debian\nSuites: %s %s-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: https://mirrors.tuna.tsinghua.edu.cn/debian-security\nSuites: %s-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' "$VERSION_CODENAME" "$VERSION_CODENAME" "$VERSION_CODENAME" > /etc/apt/sources.list.d/debian.sources; \
            pypi_index="https://pypi.tuna.tsinghua.edu.cn/simple"; \
            npm_registry="https://registry.npmmirror.com"; \
            ;; \
        global) \
            pypi_index="https://pypi.org/simple"; \
            npm_registry="https://registry.npmjs.org"; \
            ;; \
        *) echo "Unsupported MIRROR_MODE: $MIRROR_MODE"; exit 1 ;; \
    esac; \
    echo "Using system/Python mirror mode: $mirror_mode"; \
    mkdir -p /root/.config/uv; \
    printf '[global]\nindex-url = %s\n' "$pypi_index" > /etc/pip.conf; \
    printf 'index-url = "%s"\n' "$pypi_index" > /root/.config/uv/uv.toml; \
    printf 'registry=%s\n' "$npm_registry" > /root/.npmrc; \
    apt-get update; \
    apt-get install --yes --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
        openssh-client \
        ripgrep; \
    python -m pip install \
        --no-cache-dir \
        --index-url "$pypi_index" \
        "uv==0.8.4"; \
    rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s \
        /usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js \
        /usr/local/bin/pi
COPY extensions/pi-runner-mcp/package.json extensions/pi-runner-mcp/package-lock.json /opt/pi-runner-mcp/
RUN npm ci --prefix /opt/pi-runner-mcp --omit=dev --ignore-scripts --no-audit --no-fund
COPY extensions/pi-runner-mcp/src /opt/pi-runner-mcp/src
COPY config/pi-models.json /opt/pi-runner/pi-models.json
COPY scripts/container-entrypoint.py /usr/local/bin/pi-runner-entrypoint
RUN chmod 755 /usr/local/bin/pi-runner-entrypoint
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN mkdir -p /root/.pi/agent/sessions /root/.pi/bridge /root/workspace

EXPOSE 8765
ENTRYPOINT ["/usr/local/bin/pi-runner-entrypoint"]
