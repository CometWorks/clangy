# Configuration

Clangy reads a TOML file (`--config`, default `config.toml`). `config.example.toml` shows a complete one. Durations are in seconds.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | required | Conversations, store and knowledge live here. |
| `token_env` | `DISCORD_TOKEN` | Environment variable holding the bot token. |
| `run_deadline` | `600` | Hard limit of a run after it leaves the queue. |
| `attachment_max_bytes` | 25 MiB | Larger attachments aren't downloaded; the agent gets a note instead. |
| `answer_max_bytes` | 10 MiB | Total size of the files attached to one answer. Keep it within the server's upload limit. Each file is limited to 5 MB regardless. |
| `progress_interval` | `10` | Minimum time between progress message edits. |
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
| `memory_limit` | 4 GiB | Address space limit (`--rlimit-as`). |

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
| `game` | none | `se1` or `se2` if the channel is about one game. |
| `prompt` | `""` | Extra instructions appended to the system prompt for this channel. |

## `[[knowledge.se1]]`, `[[knowledge.se2]]`

Repositories for `clangy sync-knowledge`. `mount` is `skills` or `books`, which becomes `knowledge/skills` or `knowledge/books` in the sandbox. The host needs read access, for example SSH keys or a credential helper for the private book repositories.
