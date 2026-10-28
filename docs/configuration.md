# Configuration

Clangy reads a TOML file (`--config`, default `config.toml`). `config.example.toml` shows a working one; the tables below list every key, and an unknown key fails at startup. Durations are in seconds.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | required | Conversations, records and knowledge live here. |
| `workspace_dir` | `<data_dir>/workspace` | Tickets, notes and plans, kept past `retention_days`; with `tasks` only plans and the archive. See design.md, Store. |
| `tasks` | none | A Vikunja project for the tickets, see `[tasks]`. Without it tickets are files in the workspace. |
| `discord_token` | `""` | Bot token. Falls back to the `DISCORD_TOKEN` environment variable. Keep the config readable only by the bot's user. |
| `context_window` | smallest backend `context_window` | Caps the context pi may use on every backend. A conversation can move to another backend between runs, so the default keeps it within the smallest one. |
| `headers` | pi's attribution | Sent with every backend's model requests: `HTTP-Referer = "https://pi.dev"`, `X-OpenRouter-Title = "pi"`, `X-OpenRouter-Categories = "cli-agent"`. OpenRouter serves some free models, such as `thinkingmachines/inkling:free`, only to agent harnesses it lists and answers 403 otherwise. pi sends this itself only when it talks to OpenRouter directly; here it talks to the proxy. A backend's `headers` override single keys. |
| `run_deadline` | `600` | Hard limit of a run after it leaves the queue. |
| `attachment_max_bytes` | 25 MiB | Larger attachments aren't downloaded; the agent gets a note instead. |
| `answer_max_bytes` | 10 MiB | Total size of the files attached to one answer. Keep it within the server's upload limit. Each file is limited to 5 MB regardless. |
| `progress_interval` | `60` | How often the progress message is checked; it's edited only when its text changes. |
| `health_interval` | `30` | How often to check failed llama.cpp backends and retry the conversations waiting for a backend. |
| `max_retries` | `5` | Automatic retries after waiting for a backend, counted from the last mention or answer. |
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

## `[tasks]`

Tickets and notes go to a Vikunja project instead of the workspace's `tickets/` and `notes/` folders. Plans stay in the workspace. See design.md, Store.

| Key | Default | Meaning |
|---|---|---|
| `url` | required | The Vikunja server, e.g. `https://tasks.cometworks.se`. |
| `project` | required | Id of the project that holds the tickets. The nas: 8, Support. |
| `api_key` | none | An API key with access to that project only. Keep the config readable only by the bot's user. |
| `api_key_env` | `TASKS_PASS` | Variable with the key, used when `api_key` is unset. If the service's environment lacks it, Clangy reads it from an interactive bash, since `~/.bashrc` exports it only there. The key never enters the sandbox. |
| `canceled_label` | `cancelled` | A done task with this label is canceled, otherwise completed. |
| `note_label` | `note` | Marks the tasks that are notes. |
| `notes_bucket` | `Notes` | The Kanban bucket new notes are moved into. Matched without case and punctuation; create it on the board first. |
| `first_number` | `1` | The lowest DIS number a new ticket gets, e.g. to continue the workspace's numbers in an empty project. |
| `max_file_bytes` | 20 MB | The server's attachment limit (Vikunja's `files.maxsize`). Larger files, after gzipping, can't be kept with a ticket or note, and the tool call fails. |
| `gzip_min_bytes` | 100 KiB | Text files (no NUL byte in the first 8 KiB) from this size on are attached gzipped (`info.log.gz`) when that makes them at least 30% smaller. Images stay as they are, so Vikunja still previews them. |
| `timeout` | `30` | Seconds per API request. |
| `refresh_interval` | `300` | How often the sandbox's copy of the tickets and notes is read again. Clangy's own writes refresh it right away. |

## `[[backends]]`

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Unique name. Conversations remember the backend of their last run by name; after a rename they are placed again like new ones. |
| `url` | required | Base URL including `/v1`: OpenAI compatible, or `https://api.anthropic.com/v1` with `kind = "anthropic"`. |
| `model` | required | Model id sent to the backend. |
| `api_key` | none | The API key. Keep the config readable only by the bot's user. |
| `api_key_env` | none | Environment variable with the API key, used when `api_key` is unset. The key never enters the sandbox. |
| `parallel` | `1` | Runs at once. For llama.cpp, match the server's `--parallel` (slots). |
| `priority` | `1` | Lower is preferred. A backend gets runs only while every more preferred one has failed or is fully busy (`parallel` runs). `deploy/clangy.toml`: the llama.cpp backend `polaris` (`qwen38v-Q5`) 5, Claude Sonnet 5.5 6, the paid `openai/gpt-6-luna` 8 and `deepseek/deepseek-v4.1-flash` 9 (cheaper per answer first). |
| `kind` | `openai` | `llamacpp` enables slot pinning with `id_slot`, and a failed server gets runs again once its health check passes. Other kinds come back after `cooldown`. `anthropic`: pi talks the Anthropic Messages API, see Claude below. |
| `slot_save` | `false` | llama.cpp: save and restore slot KV caches when conversations swap slots. Needs `--slot-save-path` on the server. |
| `context_window`, `max_tokens` | `131072`, `4096` | Passed to pi. The top-level `context_window` caps the former. |
| `images` | `false` | The model takes images: pi sends a read image to it. Without it pi leaves images out of the request and the model gets a note that it can't see them. Turn it on only for models that take images, like `qwen38v-Q5` on a llama.cpp server started with its vision projector (`--mmproj`; `GET /props` shows `modalities.vision`), `openai/gpt-6-luna` and `deepseek/deepseek-v4.1-flash` (OpenRouter's `input_modalities`), and Claude. |
| `thinking` | `off` | pi thinking level: `off`, `minimal`, `low`, `medium`, `high`. With `compat.thinkingFormat` set, `off` is sent to the server explicitly; Qwen otherwise thinks by default. An OpenRouter `url` sets `thinkingFormat = "openrouter"`: the level becomes OpenRouter's `reasoning` effort, and `off` sends `effort = "none"`. |
| `compat` | `{}` | pi `compat` options for the model, e.g. `thinkingFormat = "qwen-chat-template"`. See pi's `docs/models.md`. |
| `prompt_tps`, `gen_tps` | `300`, `40` | Starting speeds for the ETA. Measured speeds replace them (llama.cpp reports them; cloud APIs don't, so their values stay). |
| `extra_body` | `{}` | Merged into every model request, after `model`. Carries OpenRouter's `models` and `provider`. A `reasoning` key is refused; set `thinking` instead. |
| `headers` | `{}` | Sent with every model request, over the top-level `headers`, key by key. |
| `refusal_fallback` | `false` | Takes over a run whose model refused it, right away and with the same turn, like the llama.cpp backend for Claude's refusals. See Claude below. |
| `first_byte_timeout` | none | A request fails if no data arrives for this long. Only for cloud APIs: llama.cpp sends nothing while it reads a long prompt. Keep-alive comments don't count as data. |
| `stall_timeout` | none | A request fails if its stream stops for this long after the first data. |
| `cooldown` | `60` | Kinds other than `llamacpp`: how long a failed backend gets no runs, longer if it sent `Retry-After`. Doubles with each further failure in a row; a successful run resets it. |
| `max_cooldown` | `1800` | The longest cooldown after failures in a row. |

A request fails on a connection error, HTTP 401, 402, 403, 404, 408, 429 or 5xx (the backend can't serve now; a 400 means the request itself was rejected and isn't one), an error inside the stream (an event with `error`, such as Anthropic's `overloaded_error`, or `finish_reason` `error`), or one of the timeouts. The run is then repeated on another backend right away, see design.md, Backends.

### Claude

`kind = "anthropic"` with `url = "https://api.anthropic.com/v1"` uses Claude through the Anthropic Messages API instead of an OpenAI compatible one, so pi marks the conversation for Anthropic's prompt cache and keeps Claude's signed thinking between turns. `config.example.toml` and `deploy/clangy.toml` have a commented `claude-sonnet` backend with `claude-sonnet-5-5`. Notes:

- The key (`sk-ant-...`) goes into `api_key` or the variable named by `api_key_env`. The proxy sends it as `x-api-key`; pi only ever sees a placeholder. pi's own `anthropic-version` header is passed on, and its `anthropic-beta` values are added to the backend's.
- pi can't tell the model from its id (`clangy`), so the backend sets `compat.forceAdaptiveThinking`: `thinking` levels become adaptive thinking with that effort (`minimal` and `low` are both `low`), and `off` sends `thinking = {type = "disabled"}`. Claude Sonnet 5 accepts that; Claude Sonnet 5.5 and Opus 5.5 answer 400, so give them a level other than `off`. Sonnet 5.5's levels are recalibrated; `medium` is the starting point for multi-step tool use.
- Claude Sonnet 5.5 checks that the history before a thinking block is unchanged, and on accounts created since 2026-08-31 a changed history is a 400. Clangy changes it: `replace_answer` puts the answer as posted into the session. `compat.supportsMidConvoEffort = true` makes pi send adaptive thinking with `block_binding.prefix_mismatch_behavior = "drop_block"` (and its betas), so the API drops the affected blocks instead. It also sends the effort as mid-conversation system messages. Leave it out for Claude Sonnet 5, which supports neither.
- A refusal (`stop_reason` `refusal`, with a category such as `cyber`; plugin and game-code questions can look like that to the classifier) is no backend failure. The run is repeated right away on the most preferred available backend with `refusal_fallback = true`, the nas's polaris, and the refused turn is dropped. The next run goes back to Claude. Without an available fallback the run fails, and the user is asked to mention Clangy again. The metrics record the category in `refusal`.
- Anthropic's own server-side fallback to another Claude model would also work: `extra_body = { fallbacks = "default" }` with `headers = { "anthropic-beta" = "server-side-fallback-2026-07-01" }`. pi's `anthropic-beta` values are added to the configured one. The templates leave it out, so refusals go to the local model.
- `temperature` is never sent (`compat.supportsTemperature = false`); the API refuses it since 2026-10-21.
- The usage in the metrics counts cache reads and writes as prompt tokens, with the reads as `cached_tokens`. The API reports no speeds, so `prompt_tps` and `gen_tps` stay as configured.
- A conversation moving to or from Claude loses the other model's reasoning: pi drops thinking it didn't get from the same API and model. Answers, tool calls and tool results move unchanged.

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
| `link` | Relative path in the work folder: `knowledge/skills`, `knowledge/books` (the prompt refers to these), another folder under `knowledge/` such as `knowledge/sdk` (its skills are listed like the others), or e.g. `.se-dev` for prepared data the skills expect in the home folder, which is the work folder in the sandbox. |
| `path` | A folder used in place, read-only. |
| `url` | A git repository mirrored by `clangy sync-knowledge`. The host needs read access, for example SSH keys for the private book repositories. |

Each source has either `path` or `url`.

## `[[mounts]]`

Sources like the knowledge ones (`link` plus `path` or `url`), but linked into every conversation whatever the game. The nas mounts `~/repos` as `repos`, the ecosystem and plugin repositories the prompt refers to. A mount linked under `knowledge/` is listed with the game's skills.
