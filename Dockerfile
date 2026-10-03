FROM python:3.12-slim-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends firejail git nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @mariozechner/pi-coding-agent@0.66.1 && npm cache clean --force

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

# firejail can whitelist only under a few roots such as /opt and /srv, so the app lives in /opt
WORKDIR /opt/clangy
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

RUN useradd --uid 1000 --create-home clangy
USER clangy
ENV PATH=/opt/clangy/.venv/bin:$PATH
ENTRYPOINT ["clangy", "--config", "/srv/clangy/config.toml"]
CMD ["run"]
