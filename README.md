# Clangy

Clangy v2, the support bot of the [Pulsar](https://github.com/SpaceGT/Pulsar) Discord server. It collects what developers need from bug reports, files tickets, and points plugin, mod and script developers to the right resources, for both Space Engineers 1 and 2.

Each Discord thread runs a [pi](https://pi.dev) agent in its own firejail sandbox. The answers come from OpenAI compatible backends by priority: OpenRouter's free models, our own llama.cpp server, and paid models as the last resort.

## Quick start

```bash
uv sync
uv run pytest
cp config.example.toml config.toml   # then edit
uv run clangy run                    # token in config.toml or DISCORD_TOKEN
```

## Docs

- [Design](docs/design.md): how a run works, sandbox, prompt caching, backends and failover, knowledge, tools, posts from the developers, metrics, data retention
- [Configuration](docs/configuration.md)
- [Deployment](docs/deployment.md): systemd service on the nas, `deploy/deploy.sh`, commands
- [Testing](docs/testing.md)
