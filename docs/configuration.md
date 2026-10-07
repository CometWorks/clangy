# Configuration

Clangy reads a TOML file (`--config`, default `config.toml`). `config.example.toml` shows a working one; the tables below list every key, and an unknown key fails at startup. Durations are in seconds.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | required | Conversations, records and knowledge live here. |
| `workspace_dir` | `<data_dir>/workspace` | Tickets, notes and plans, kept past `retention_days`. See design.md, Store. |
| `discord_token` | `""` | Bot token. Falls back to the `DISCORD_TOKEN` environment variable. Keep the config readable only by the bot's user. |
| `context_window` | smallest backend `context_window` | Caps the context pi may use on every backend. A conversation can move to another backend between runs, so the default keeps it within the smallest one. |
| `headers` | pi's attribution | Sent with every backend's chat completion requests: `HTTP-Referer = "https://pi.dev"`, `X-OpenRouter-Title = "pi"`, `X-OpenRouter-Categories = "cli-agent"`. OpenRouter serves some free models, such as `thinkingmachines/inkling:free`, only to agent harnesses it lists and answers 403 otherwise. pi sends this itself only when it talks to OpenRouter directly; here it talks to the proxy. A backend's `headers` override single keys. |
| `run_deadline` | `600` | Hard limit of a run after it leaves the queue. |
| `attachment_max_bytes` | 25 MiB | Larger attachments aren't downloaded; the agent gets a note instead. |
| `answer_max_bytes` | 10 MiB | Total size of the files attached to one answer. Keep it within the server's upload limit. Each file is limited to 5 MB regardless. |
| `progress_interval` | `60` | How often the progress message is checked; it's edited only when its text changes. |
| `health_interval` | `30` | How often to check failed llama.cpp backends and retry the conversations waiting for a backend. |
| `max_retries` | `3` | Automatic retries after a backend failure, counted from the last mention. |
| `switch_margin` | `60` | A conversation moves to another backend only if that one answers sooner by this much, even after reading the whole conversation again. See design.md, Backends. |
| `retention_days` | `30` | Conversations (all but `metrics.jsonl`) and records not touched for this long are deleted at startup. The message cache keeps two days at most. See design.md, Retention. |
| `secret_markers` | book names, `knowledge/books` | Answers containing any of these (case-insensitive) are withheld. |
| `hidden_skills` | none | Skill names left out of the skill list the model gets, e.g. ones that need tools the sandbox lacks. Their files stay readable. |

## `[sandbox]`

| Key | Default | Meaning |
|---|---|---|
| `firejail` | `true` | Set `false` only for development. |
| `pi_command` | `["pi"]` | Command that starts pi. |
| `path` | `/usr/local/bin:/usr/bin:/bin` | `PATH` inside the sandbox. Must reach `node`, `pi` and `python3`. |
| `read_only` | `[]` | Extra host paths the sandbox may read, typically the node installation that holds pi. |
| `env` | `{}` | Extra environment variables inside the sandbox, e.g. `UV_OFFLINE = "1"` for skills run with `uv run`. |
| `proxy` | none | `host:port` of an HTTP proxy with a domain allowlist the sandbox may use, e.g. a local squid. Unset means no network at all. |
| `uv` | `uv` | uv on the host. It builds the sandboxes' shared Python environment (see design.md, Sandbox). The systemd unit's `PATH` lacks `~/.local/bin`, so the nas gives the full path. |
| `python` | `python3` | Interpreter of that environment. It must be readable in the sandbox. |

## `[[backends]]`

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Unique name. Conversations remember the backend of their last run by name; after a rename they are placed again like new ones. |
| `url` | required | OpenAI compatible base URL including `/v1`. |
| `model` | required | Model id sent to the backend. |
| `api_key` | none | The API key. Keep the config readable only by the bot's user. |
| `api_key_env` | none | Environment variable with the API key, used when `api_key` is unset. The key never enters the sandbox. |
| `parallel` | `1` | Runs at once. For llama.cpp, match the server's `--parallel` (slots). |
| `priority` | `1` | Lower is preferred. A backend gets runs only while every more preferred one has failed or is fully busy (`parallel` runs). The nas: OpenRouter free models 1, polaris 5, the paid `openai/gpt-6-luna` 8 and `deepseek/deepseek-v4.1-flash` 9 (cheaper per answer first). |
| `kind` | `openai` | `llamacpp` enables slot pinning with `id_slot`, and a failed server gets runs again once its health check passes. Other kinds come back after `cooldown`. |
| `slot_save` | `false` | llama.cpp: save and restore slot KV caches when conversations swap slots. Needs `--slot-save-path` on the server. |
| `context_window`, `max_tokens` | `131072`, `4096` | Passed to pi. The top-level `context_window` caps the former. |
| `thinking` | `off` | pi thinking level: `off`, `minimal`, `low`, `medium`, `high`. With `compat.thinkingFormat` set, `off` is sent to the server explicitly; Qwen otherwise thinks by default. An OpenRouter `url` sets `thinkingFormat = "openrouter"`: the level becomes OpenRouter's `reasoning` effort, and `off` sends `effort = "none"`. |
| `compat` | `{}` | pi `compat` options for the model, e.g. `thinkingFormat = "qwen-chat-template"`. See pi's `docs/models.md`. |
| `prompt_tps`, `gen_tps` | `300`, `40` | Starting speeds for the ETA. Measured speeds replace them (llama.cpp reports them; cloud APIs don't, so their values stay). |
| `extra_body` | `{}` | Merged into every chat completion request, after `model`. Carries OpenRouter's `models` and `provider`. A `reasoning` key is refused; set `thinking` instead. |
| `headers` | `{}` | Sent with every chat completion request, over the top-level `headers`, key by key. |
| `first_byte_timeout` | none | A request fails if no data arrives for this long. Only for cloud APIs: llama.cpp sends nothing while it reads a long prompt. Keep-alive comments don't count as data. |
| `stall_timeout` | none | A request fails if its stream stops for this long after the first data. |
| `cooldown` | `60` | Kinds other than `llamacpp`: how long a failed backend gets no runs, longer if it sent `Retry-After`. |

A request fails on a connection error, HTTP 401, 402, 403, 404, 408, 429 or 5xx (the backend can't serve now; a 400 means the request itself was rejected and isn't one), an error inside the stream (an event with `error`, or `finish_reason` `error`), or one of the timeouts. The run is then repeated on another backend right away, see design.md, Backends.

### OpenRouter free models

`config.example.toml` has a commented `openrouter-free` backend. Notes:

- Free models allow 20 requests a minute and 1000 a day per account. A run makes roughly 7 to 11 requests, hence `parallel = 2`. `GET https://openrouter.ai/api/v1/key` reports the day's use in `free_model_daily_requests`.
- `extra_body.models` lists up to 3 models (more is a 400 error). OpenRouter tries them in order within one request, so a rate-limited model is answered by the next. An invalid model id fails every request. The model that answered goes into the metrics.
- Free models are often rate limited upstream (429). `google/gemma-4-31b-it:free` was every time when tested, so it's last.
- `thinking` applies whichever model answers. Keep it at `minimal` for Qwen3.8-27B, on OpenRouter and on llama.cpp alike: without reasoning it doesn't follow the setup checklist of the system prompt reliably. Nemotron reasons briefly at `minimal` too. The paid models answer fine with `thinking = "off"`; `openai/gpt-6-luna` and `deepseek/deepseek-v4.1-flash` then use no reasoning tokens.
- `nvidia/nemotron-3-super-120b-a12b:free` works only while the account allows free-model providers to use the prompts for training. With `provider = { data_collection = "deny" }` it returns 404. Everything sent to free models may be logged and trained on; that includes the support threads and the attachments the agent reads.
- Paid models such as `openai/gpt-6-luna` and `deepseek/deepseek-v4.1-flash` need a key whose credit limit is above 0, otherwise they answer 403 "Key limit exceeded".
- The key goes into `api_key` in the config (the nas: `~/clangy.toml`, mode 600) or into the variable named by `api_key_env`.

## `[[channels]]`

Clangy acts only in listed channels. The Discord permission overwrites must match this list.

| Key | Default | Meaning |
|---|---|---|
| `id` | required | Channel id. |
| `mode` | required | `support` (forum), `record` (forum) or `mention` (text channel). |
| `initial_response` | `false` | Forums: answer the post that starts a thread. |
| `initial_delay` | `0` | Forums: the first answer waits until nobody wrote, edited or deleted anything in the new thread for this long, so people can add details and corrections. Mentions are never delayed and cancel the wait. |
| `history_messages`, `history_hours` | `100`, `4` | The earlier messages that go along with a mention in a channel as context: back to midnight UTC, but at least `history_hours`, and at most `history_messages`. A mention in a thread Clangy doesn't know yet reads all of the thread instead. |
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

## `[[mounts]]`

Sources like the knowledge ones (`link` plus `path` or `url`), but linked into every conversation whatever the game. The nas mounts `~/repos` as `repos`, the ecosystem and plugin repositories the prompt refers to. A mount linked under `knowledge/` is listed with the game's skills.
