# Deployment

Clangy runs as a systemd user service of the `clangy` user on the nas. There is no package or image: the working copy is copied over with rsync.

## Layout on the host

| Path | What |
|---|---|
| `~/clangy/` | The working copy, replaced by every deploy. Its `.venv` is installed there with Python 3.13 from Debian. |
| `~/clangy.toml` | The config, mode 600 since it holds the Discord token. `deploy/clangy.toml` is its template; deploys never touch it. |
| `~/data/` | Conversations, store, metrics. |
| `~/skills`, `~/skills2`, `~/se-dev-books`, `~/se2-dev-books`, `~/.se-dev`, `~/.se2-dev` | The prepared knowledge, used in place. |
| `~/.pi/agent/bin/pi` | pi, installed by its own installer, with Node.js under `~/.local/share/pi-node`. |
| `~/.local/bin/uv` | uv, only needed by deploys. |

The sandbox may read pi's `bin` and `install` folders and its Node.js, but not the rest of `~/.pi/agent` (`auth.json`, pi's own sessions). Lingering is enabled for `clangy`, so its user services run without a login.

## Requirements

- firejail (`sudo apt install firejail`). Without a working sandbox `clangy run` refuses to start, with exit status 78, which the unit doesn't restart on.
- The backends reachable from the host (`polaris:8080`).

## Deploy

From a working copy with SSH access to `clangy@nas`:

```bash
deploy/deploy.sh
```

It rsyncs the working copy (without `.git`, `.venv`, `data/` and a local `config.toml`), runs `uv sync`, links and enables `deploy/clangy.service`, and restarts the service if it's running.

## Run

Set `discord_token` in `~/clangy.toml` and the channels to serve, then on the host:

```bash
systemctl --user start clangy
```

Logs:

```bash
journalctl --user -u clangy -f
```

## Checks on the host

```bash
cd ~/clangy && .venv/bin/clangy --config ~/clangy.toml check
```

Without Discord, this checks the backends, the knowledge links, the sandbox (a probe run through the real sandbox command line: no network, the config and other conversations hidden, work folder writable, pi, node and python3 present) and one real answer.

The test suite runs on the host too, including the real pi test:

```bash
cd ~/clangy && PATH=~/.pi/agent/bin:~/.local/share/pi-node/current/bin:$PATH ~/.local/bin/uv run --frozen --python /usr/bin/python3 pytest -q
```

`.venv/bin/clangy --config ~/clangy.toml timings` summarizes the timing records of all conversations.

## Knowledge

The prepared folders are used in place, see `[[knowledge.*]]` in `~/clangy.toml`. Git sources (`url`) would be mirrored by `clangy sync-knowledge`, run daily by a timer or cron job; the nas uses none.

The code skills run their scripts with `uv run` from their own `.venv`. In the sandbox the skills are read-only and offline, so `sandbox.env` makes uv use the venvs as they are (`UV_NO_SYNC`, `UV_OFFLINE`), and the sandbox may read uv's Python installs (the SE1 skills pin Python 3.11). After copying new skills, build their venvs once as `clangy`, outside the sandbox: run any of their `uv run` commands, or `uv sync --frozen` in each skill folder. Venvs copied from another machine don't work, they point to that machine's Python. Searches work from the prepared indexes; preparing, indexing, downloading and the mod and script listings (which need the installed game) don't work in the sandbox.

The clangy user's own pi loads the same skills through `skills` in `~/.pi/agent/settings.json`.

## llama.cpp backend

polaris serves one slot (`parallel = 1`). With more slots, set `parallel` to the server's `--parallel`. To keep each conversation's KV cache when several share a slot, start the server with `--slot-save-path` and set `slot_save = true`. Send all traffic to that server through Clangy, since slot pinning assumes Clangy decides which conversation owns which slot.

## Discord

v2 uses the bot registration v1 had (`Clangy#0168`), with its intents (Message Content), OAuth2 scopes, bot permissions and per-channel member overwrites (see v1's `docs/Permissions.md`). v2 also needs Create Public Threads and Send Messages in Threads in mention-mode channels, and Attach Files wherever it answers. v1 is retired.

v2 acts only in the channels listed in `[[channels]]`; keep the member overwrites in line with that list. Threads v1 started are never continued.
