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

## Docker (nas)

`deploy/build.sh` builds the image and pushes it to `192.168.1.40:5000/clangy2` (`clangy` is v1's image). `deploy/compose.yml` runs it with `/srv/clangy/config.toml`, `/srv/clangy/data` and the token in `/srv/clangy/clangy.env`. Inside the container the data dir must be `/srv/clangy/data`, since firejail can whitelist only under roots such as `/srv` and `/opt`.

**Open problem:** firejail 0.9.74 detects that it runs in a container and then starts the program without any sandboxing ("an existing sandbox was detected"), even with `--force`. In a default container the probe shows an open network and visible data. It's untested whether added privileges (`SYS_ADMIN`, unconfined seccomp and AppArmor) change that. Options:

1. Run Clangy directly on the host as a systemd service with firejail from Debian. This matches the design and needs no container privileges.
2. Keep Docker and use bubblewrap inside it. Needs a seccomp profile that allows user namespaces.
3. Keep Docker without a per-conversation sandbox: an internal network with only the backend reachable. Conversations could then read each other's files.

Clangy fails closed: with `sandbox.firejail = true`, `clangy run` probes the sandbox first and refuses to start if the network is reachable or other data is visible.

## Checking a deployment

```bash
uv run clangy --config config.toml check
```

Without Discord, it checks the backends, the knowledge trees, the sandbox (a probe run through the real sandbox command line: no network, other data hidden, work folder writable, pi, node and python3 present) and one real answer.

`clangy timings` summarizes the timing records of all conversations.

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
