# Deployment

Not deployed yet. These are the steps for the first deployment.

## Host

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Node.js 22+ and pi: `npm install -g @mariozechner/pi-coding-agent` (developed against 0.66.1)
- firejail
- python3 for the agent's log analysis scripts
- git for the knowledge sync

```bash
uv sync --locked
cp config.example.toml config.toml
DISCORD_TOKEN=... uv run clangy --config config.toml run
```

Run it as a systemd service with the token (and any backend API keys) in an environment file only the service user can read.

## Knowledge sync

Daily cron, no inference:

```bash
uv run clangy --config config.toml sync-knowledge
```

It does nothing when no repository changed.

## llama.cpp backend

Start the server with as many slots as `parallel`, prompt caching on (the default), and a slot save folder if `slot_save = true`:

```bash
llama-server -m model.gguf -c 262144 --parallel 2 --jinja --slot-save-path /var/cache/llama-slots
```

Unless the server uses a unified KV cache, the context is split between slots, so `-c` should be `parallel` × the per-conversation context. Send all traffic to that server through Clangy, since slot pinning assumes Clangy decides which conversation owns which slot.

## Discord

v2 reuses v1's application, bot user and token, its intents (Message Content), OAuth2 scopes, bot permissions and per-channel member overwrites (see v1's `docs/Permissions.md`). v2 additionally needs Create Public Threads and Send Messages in Threads in mention-mode channels, and Attach Files wherever it answers.

v1 found its channels from those overwrites. v2 reads them from `[[channels]]`, so the two must match at cutover.

v1 and v2 can't share a token, because both would answer. Test on a separate test application and server, then stop v1 and start v2 in one step. Threads v1 started are never continued.
