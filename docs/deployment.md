# Deployment

Clangy runs as a systemd user service of the `clangy` user on the nas. There is no package or image: the working copy is copied over with rsync.

## Layout on the host

| Path | What |
|---|---|
| `~/clangy/` | The working copy, replaced by every deploy. Its `.venv` is installed there with Python 3.13 from Debian. |
| `~/clangy.toml` | The config, mode 600 since it holds the Discord token. `deploy/clangy.toml` is its template; deploys never touch it. |
| `~/data/` | Conversations, store, metrics. |
| `~/skills`, `~/skills2`, `~/se-dev-books`, `~/se2-dev-books`, `~/.se-dev`, `~/.se2-dev` | The prepared knowledge, used in place. |
| `~/repos/` | Ecosystem and plugin repositories, mounted read-only into every conversation as `repos`; `~/repos/clone-or-pull` updates them daily. |
| `~/.pi/agent/bin/pi` | pi, installed by its own installer, with Node.js under `~/.local/share/pi-node`. |
| `~/.local/bin/uv` | uv: deploys, and the work folders' Python environments (`sandbox.uv`). Its cache in `~/.cache/uv` holds the packages they are copied from. |

The sandbox may read pi's `bin` and `install` folders and its Node.js, but not the rest of `~/.pi/agent` (`auth.json`, pi's own sessions). Lingering is enabled for `clangy`, so its user services run without a login.

## Requirements

- firejail, pandoc and zip (`sudo apt install firejail pandoc zip`). The sandbox uses pandoc, zip and the other archive tools from `/usr/bin`. Without a working sandbox `clangy run` refuses to start, with exit status 78, which the unit doesn't restart on.
- The backends reachable from the host (`polaris:8080`, and `openrouter.ai` if the OpenRouter backend is enabled).
- Optionally squid, see Proxy.

## Proxy

The sandbox has no network, except an optional squid on the host (`proxy = "127.0.0.1:3128"`) whose domain allowlist decides what commands in the sandbox can reach. Let squid listen on loopback only, since only Clangy uses it and the allowlist has no source restriction:

```
http_port 127.0.0.1:3128
```

## Deploy

From a working copy with SSH access to `clangy@nas`:

```bash
deploy/deploy.sh
```

The host defaults to `clangy@nas`, another one can be given as the first argument. It rsyncs the working copy (without `.git`, `.venv`, `data/` and a local `config.toml`), runs `uv sync`, links and enables `deploy/clangy.service`, and restarts the service if it's running.

## Run

Set `discord_token` in `~/clangy.toml` and the channels to serve. For the OpenRouter backends (the free models and the paid fallback), uncomment them in `~/clangy.toml` and put the key into their `api_key`; the unit has no environment file. Then on the host:

```bash
systemctl --user start clangy
```

The `clangy` crontab restarts it daily at 05:27 (Europe/Stockholm), ten minutes after `~/repos/clone-or-pull` updates the repos. Its top sets the environment cron doesn't provide (`XDG_RUNTIME_DIR` for `systemctl --user`, and a `PATH` with uv and pi):

```
SHELL=/bin/bash
HOME=/home/clangy
LANG=en_US.UTF-8
PATH=/home/clangy/.local/bin:/home/clangy/.pi/agent/bin:/usr/local/bin:/usr/bin:/bin
XDG_RUNTIME_DIR=/run/user/1009

17 5 * * * cd $HOME/repos && ./clone-or-pull > clone-or-pull.log 2>&1
27 5 * * * systemctl --user restart clangy
```

Logs:

```bash
journalctl --user -u clangy -f
```

## Commands

```
clangy [--config FILE] run | check | sync-knowledge | timings
```

| Command | Does |
|---|---|
| `run` | The bot. Refuses to start (exit status 78) without a working sandbox or a Discord token. |
| `check` | Self-test without Discord, see below. |
| `sync-knowledge` | Mirrors the knowledge sources given by `url`, see Knowledge. |
| `timings` | Summarizes the `metrics.jsonl` of all conversations. |

`--config` defaults to `config.toml` in the working directory.

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
