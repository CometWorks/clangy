# Configuration

Clangy reads a TOML file (`--config`, default `config.toml`). `config.example.toml` shows a complete one. Durations are in seconds.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | required | Conversations, store and knowledge live here. |
| `discord_token` | `""` | Bot token. Falls back to the `DISCORD_TOKEN` environment variable. Keep the config readable only by the bot's user. |
| `run_deadline` | `600` | Hard limit of a run after it leaves the queue. |
| `attachment_max_bytes` | 25 MiB | Larger attachments aren't downloaded; the agent gets a note instead. |
| `answer_max_bytes` | 10 MiB | Total size of the files attached to one answer. Keep it within the server's upload limit. Each file is limited to 5 MB regardless. |
| `progress_interval` | `60` | How often the progress message is edited. |
| `health_interval` | `30` | How often to check backends that lost conversations. |
| `max_retries` | `3` | Automatic retries after a backend loss. |
| `secret_markers` | book names, `knowledge/books` | Answers containing any of these (case-insensitive) are withheld. |

## `[sandbox]`

| Key | Default | Meaning |
|---|---|---|
| `firejail` | `true` | Set `false` only for development. |
| `pi_command` | `["pi"]` | Command that starts pi. |
| `path` | `/usr/local/bin:/usr/bin:/bin` | `PATH` inside the sandbox. Must reach `node`, `pi` and `python3`. |
| `read_only` | `[]` | Extra host paths the sandbox may read, typically the node installation that holds pi. |
| `env` | `{}` | Extra environment variables inside the sandbox, e.g. `UV_OFFLINE = "1"` for skills run with `uv run`. |
| `proxy` | none | `host:port` of an HTTP proxy with a domain allowlist the sandbox may use, e.g. a local squid. Unset means no network at all. |

## `[[backends]]`

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Unique name. Conversations are pinned to it, so don't rename a backend with live conversations. |
| `url` | required | OpenAI compatible base URL including `/v1`. |
| `model` | required | Model id sent to the backend. |
| `api_key_env` | none | Environment variable with the API key. The key never enters the sandbox. |
| `parallel` | `1` | Runs at once. For llama.cpp, match the server's `--parallel` (slots). |
| `kind` | `openai` | `llamacpp` enables slot pinning with `id_slot`. |
| `slot_save` | `false` | llama.cpp: save and restore slot KV caches when conversations swap slots. Needs `--slot-save-path` on the server. |
| `context_window`, `max_tokens` | `131072`, `4096` | Passed to pi. |
| `thinking` | `off` | pi thinking level: `off`, `minimal`, `low`, `medium`, `high`. |
| `compat` | `{}` | pi `compat` options for the model, e.g. `thinkingFormat = "qwen-chat-template"`. See pi's `docs/models.md`. |
| `prompt_tps`, `gen_tps` | `300`, `40` | Starting speeds for the ETA. Measured speeds replace them. |

## `[[channels]]`

Clangy acts only in listed channels. The Discord permission overwrites must match this list.

| Key | Default | Meaning |
|---|---|---|
| `id` | required | Channel id. |
| `mode` | required | `support` (forum), `record` (forum) or `mention` (text channel). |
| `initial_response` | `false` | Forums: answer the post that starts a thread. |
| `initial_delay` | `0` | Forums: the first answer waits until nobody wrote, edited or deleted anything in the new thread for this long, so people can add details and corrections. Mentions are never delayed and cancel the wait. |
| `history_messages`, `history_age` | `20`, `3600` | Mention channels: the earlier channel messages that go along with a mention as context, at most this many and this old. |
| `game` | none | `se1` or `se2` if the channel is about one game. |
| `prompt` | `""` | Extra instructions appended to the system prompt for this channel. |

## `[[knowledge.se1]]`, `[[knowledge.se2]]`

Knowledge mounted for conversations about that game. Each source is a link in the conversation's work folder:

| Key | Meaning |
|---|---|
| `link` | Relative path in the work folder: `knowledge/skills`, `knowledge/books` (the prompt refers to these), or e.g. `.se-dev` for prepared data the skills expect in the home folder, which is the work folder in the sandbox. |
| `path` | A folder used in place, read-only. |
| `url` | A git repository mirrored by `clangy sync-knowledge`. The host needs read access, for example SSH keys for the private book repositories. |

Each source has either `path` or `url`.
