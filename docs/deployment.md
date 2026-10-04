# Deployment

Clangy runs as a systemd user service of the `clangy` user on the nas. There is no package or image: the working copy is copied over with rsync.

## Layout on the host

```
/home/clangy/                workspace_dir: the home folder is the workspace
├── clangy/                  working copy, replaced by every deploy; .venv with Debian's Python 3.13
├── clangy.toml              the config, from deploy/clangy.toml; mode 600, deploys never touch it
├── AGENTS.md                rules for agents working in the workspace, written once from templates/
├── tickets/                 problem reports for the developers, see design.md, Store
│   ├── Template.md          written once from templates/
│   ├── T-0001.md
│   ├── completed/
│   └── canceled/
├── notes/
│   ├── N-0001.md            a note without files
│   └── N-0002/              a note with files, e.g. a ticket's logs
│       ├── NOTE.md
│       └── <files>
├── plans/                   like notes, P-0001.md or P-0001/PLAN.md
├── data/                    data_dir
│   ├── conversations/<thread id>/
│   │   ├── state.json
│   │   ├── metrics.jsonl    kept after the conversation expires
│   │   ├── files/           received/, sent/<run>/, outbox/
│   │   └── work/            the sandbox's work folder
│   ├── records/<thread id>.md
│   ├── messages.sqlite      message cache
│   ├── python/<hash>/       the sandboxes' Python environment
│   └── check/               scratch folders of `clangy check`
├── repos/                   public git clones, mounted as repos/
│   ├── clone-or-pull        clones and pulls these and forgejo/ daily, rebuilds the venvs, syncs the hub plugins
│   ├── CometWorks/skills/   SE1 skills, linked as knowledge/skills
│   ├── CometWorks/skills2/  SE2 skills, linked as knowledge/skills
│   ├── blader/humanizer/    writing skill, linked as knowledge/humanizer
│   ├── pulsar-se1-plugins/<owner>/<repo>/   PluginHub plugins at their registered commits
│   ├── pulsar-se2-plugins/<owner>/<repo>/   PluginHub-SE2 plugins
│   ├── magnetar-plugins/<owner>/<repo>/     MagnetarHub plugins
│   └── <owner>/<repo>/      ecosystem repositories, and plugins that aren't in a hub
├── forgejo/                 private clones, kept out of the repos/ mount
│   ├── se-dev-books/        SE1 books, linked as knowledge/books
│   └── se2-dev-books/       SE2 books, linked as knowledge/books
├── .se-dev/, .se2-dev/      the skills' prepared data; each skill's Data folder links here
├── .steam/debian-installation/steamapps/workshop/content/244850/   SE1 workshop mods, for se-dev-mod
├── .ssh/                    config: one forgejo-<repo> host alias per deploy key
├── .pi/agent/bin/pi         pi, from its own installer
├── .local/share/pi-node/    pi's Node.js
├── .local/share/uv/tools/   graphify, for the code skills' graph queries
└── .local/bin/              uv (deploys, the sandboxes' Python environment) and graphify
```

Folders of the clangy user that Clangy doesn't use are left out.

uv hardlinks the sandboxes' packages from its cache in `~/.cache/uv`. The sandbox may read pi's `bin` and `install` folders and its Node.js, but not the rest of `~/.pi/agent` (`auth.json`, pi's own sessions). Lingering is enabled for `clangy`, so its user services run without a login.

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

Without Discord, this checks the backends, the knowledge links, the Python environment (building it if needed), the sandbox (a probe run through the real sandbox command line: no network, the config and other conversations hidden, work folder writable, pi, node and python3 present) and one real answer.

The test suite runs on the host too, including the real pi test:

```bash
cd ~/clangy && PATH=~/.pi/agent/bin:~/.local/share/pi-node/current/bin:$PATH ~/.local/bin/uv run --frozen --python /usr/bin/python3 pytest -q
```

`.venv/bin/clangy --config ~/clangy.toml timings` summarizes the timing records of all conversations.

## Knowledge

The knowledge sources are folders used in place, see `[[knowledge.*]]` in `~/clangy.toml`. The skills are the clones `~/repos/CometWorks/skills` and `~/repos/CometWorks/skills2`, which `~/repos/clone-or-pull` pulls daily with the other repositories. The books are the private clones `~/forgejo/se-dev-books` and `~/forgejo/se2-dev-books`. They stay out of `~/repos`, because that is mounted into every conversation, and the books may only show up as `knowledge/books` for the conversation's game. `clone-or-pull` pulls them too, each with its own read-only Forgejo deploy key: `~/.ssh/config` has a `forgejo-<repo>` host alias per key, and the clones' `origin` uses it (`forgejo-se-dev-books:CometWorks/se-dev-books.git`). Git sources (`url`) would be mirrored by `clangy sync-knowledge`, run daily by a timer or cron job; the nas uses none.

The skills keep their prepared data (decompiled code, indexes, graphs) in `~/.se-dev` and `~/.se2-dev`, and each skill's `Data` folder is a symlink to its part, e.g. `skills/se-dev-game-code/Data` to `~/.se-dev/game-code`. Both folders are `[[mounts]]`, linked into every conversation, because both games' skills are readable through `repos/` and their `Data` links must resolve whatever the game. The sandbox follows the symlinks, since their targets are mounted too.

The code skills query their graphs with `graphify`, installed with `uv tool install --python 3.12 'graphifyy[leiden]'`. Its tool folder `~/.local/share/uv/tools` is in `sandbox.read_only`, and its command is in `~/.local/bin`, which is on the sandbox's `PATH`.

The se-dev-mod skill reads workshop mods in place. They are in the Steam layout under `~/.steam`, copied from a machine that has the game, and `sandbox.read_only` has the workshop folder. There is no game on the host, so `SE_GAME_ROOT` in `sandbox.env` names where it would be, which is how the skill finds the workshop folder next to it. The paths in the skill's `Data/mods.json` must point at this host's folders. The index stores files as `<workshop id>/<path under Data/Scripts>`.

The plugin skills get every hub plugin's source without downloading anything. `deploy/sync-hub-plugins.py`, run by `clone-or-pull` after the skills are pulled, pulls the hub registries in the skills' data (PluginHub and MagnetarHub for SE1, PluginHub-SE2 for SE2). It clones each GitHub plugin they list into `~/repos/pulsar-se1-plugins`, `~/repos/magnetar-plugins` or `~/repos/pulsar-se2-plugins`, checked out at the commit the hub registers. Then it links the skill's `Data/Sources/<repo>` to that clone, so the skill lists the plugin as downloaded, and rebuilds the skill's code index when anything changed. A repository listed several times, in one hub (e.g. per Pulsar flavor) or in two, is checked out at its first entry's commit, and two repositories with the same name keep the first one's link. The SE2 plugin skill also keeps a record of its downloads in `Data/plugins.json`, and offers to download whatever isn't in it, so the script records its clones there with the skill's own `plugin_registry.update_plugin`. `clone-or-pull` doesn't list the plugins that are in a hub, so each has one working copy, in its hub's folder.

The code skills run their scripts with `uv run` from their own `.venv`. In the sandbox the skills are read-only and offline, so `sandbox.env` makes uv use the venvs as they are (`UV_NO_SYNC`, `UV_OFFLINE`), and the sandbox may read uv's Python installs (the SE1 skills pin Python 3.11). `~/repos/clone-or-pull` builds the venvs after every pull, as `clangy` and outside the sandbox, with `uv sync --frozen` in each skill folder and each `~/forgejo` clone that has a `pyproject.toml`. After moving a skills clone, delete its `.venv` folders first: a venv holds absolute paths, and `uv sync` doesn't rewrite them. Venvs copied from another machine don't work, they point to that machine's Python. Searches and graph queries work from the prepared indexes and graphs; preparing, indexing, downloading and refreshing the mod and script listings (which need the installed game) don't work in the sandbox.

The humanizer skill comes from `~/repos/blader/humanizer`, which `~/repos/clone-or-pull` keeps up to date. It is a `[[mounts]]` entry linked as `knowledge/humanizer`, so it is there before the game is known; the system prompt tells Clangy to read it for any answer longer than a sentence.

The clangy user's own pi loads the same skills through `skills` in `~/.pi/agent/settings.json`.

## llama.cpp backend

polaris serves one slot (`parallel = 1`). With more slots, set `parallel` to the server's `--parallel`. To keep each conversation's KV cache when several share a slot, start the server with `--slot-save-path` and set `slot_save = true`. Send all traffic to that server through Clangy, since slot pinning assumes Clangy decides which conversation owns which slot.

## Discord

v2 uses the bot registration v1 had (`Clangy#0168`), with its intents (Message Content), OAuth2 scopes, bot permissions and per-channel member overwrites (see v1's `docs/Permissions.md`). v2 also needs Create Public Threads and Send Messages in Threads in mention-mode channels, and Attach Files wherever it answers. v1 is retired.

v2 acts only in the channels listed in `[[channels]]`; keep the member overwrites in line with that list. v1's conversations aren't taken over: a mention in a thread v1 answered starts a new conversation there, which leaves v1's answers out.
