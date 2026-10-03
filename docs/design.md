# Design

Clangy v2 is a Python host process that connects to Discord and runs one short-lived [pi](https://pi.dev) agent per answer. Each Discord thread is a conversation with its own work folder and pi session, so a follow-up resumes where the last answer ended.

## Modules

| Module | Role |
|---|---|
| `discord_bot.py` | discord.py client. Converts gateway messages to `IncomingMessage` and implements the `Chat` protocol (send, edit, create thread, download). The only module that knows about Discord. |
| `core.py` | Channel rules, conversations, queued messages, one run per conversation, progress messages, retries, host tools, persistence. |
| `backend.py` | Per-backend run queue (`parallel` runs at once), queue positions, measured prompt and generation speeds, health checks, llama.cpp slot pinning and save/restore. |
| `proxy.py` | Per-run HTTP server on a unix socket inside the work folder: forwards chat completions to the pinned backend and serves the host tools. |
| `runner.py` | Writes pi's config, builds the firejail command line, runs pi with a deadline and reads its JSON event stream. |
| `store.py` | Tickets, notes, plans and records in Clangy's own store. |
| `knowledge.py` | The daily sync of knowledge kept in git repositories. |
| `check.py` | `clangy check` and the sandbox probe that `clangy run` requires to pass. |
| `files.py` | Files the agent sends: validation, at-most-once sending, ZIP grouping. |
| `text.py` | Discord texts: answer splitting, footer, progress and error messages. |
| `pi/clangy.ts` | pi extension loaded in the sandbox. |
| `prompts/system.md`, `guide/*.md` | The system prompt, and the guide files the agent reads on demand. |

## A run

1. A trigger (see below) posts a progress message right away and starts the conversation's worker. A trigger during a run sets a flag, and the worker runs again afterwards.
2. The worker waits for a slot on the conversation's backend. The progress message shows the queue position and an ETA.
3. The host snapshots the pi session file, refreshes the work folder links (`attachments`, `store`, `guide`, the game's knowledge), writes pi's `models.json` and `settings.json`, and opens the host socket.
4. pi runs in firejail with `--mode json --print --session <file>`. The prompt is a `[host]` timestamp line, then every queued message as `Name: text` plus attachment notes.
5. On success the queued messages are dropped, the answer replaces the progress message, and parts over 2000 characters go into follow-up messages. Every answer ends with the fixed mention reminder.
6. On a timeout, backend loss or other failure the session file is restored from the snapshot, so partial output never reaches the next run, and the queued messages stay queued.

### When Clangy responds

- A new forum post in a `support` or `record` channel starts a conversation. It gets an answer only if `initial_response` is on.
- In a `mention` channel, a message mentioning Clangy starts a thread from that message. The thread is the conversation, and Clangy never writes in the channel itself.
- Inside known conversations, every message is queued, and only mentions trigger a run.
- Threads Clangy doesn't know (started while it was down, or by v1) are ignored. Nothing is caught up after a restart.
- `record` channels also append every message to `store/records/<thread>.md`, without inference, so other conversations can find requests later.

## Sandbox

pi runs as `firejail ... -- env -i <vars> pi ...`:

- `--net=none`: the sandbox has only its own loopback. The pi extension listens on `127.0.0.1:$CLANGY_PORT` and pipes connections to the host socket in the work folder. That socket is the only way out.
- The environment is replaced. The Discord token and backend API keys stay on the host. The proxy adds the API key to forwarded requests.
- The work folder is the only writable path. The store, the guide, the conversation's knowledge snapshot, the package and the configured `sandbox.read_only` paths (node and pi) are read-only.
- No capabilities, no root, seccomp, private `/dev` and `/tmp`.
- No per-sandbox memory limit: firejail's `--rlimit-as` breaks Node.js, whose WebAssembly reserves large address ranges. The systemd unit caps the whole service with `MemoryMax` and `TasksMax` instead.

The allowlist the ticket mentions is just the pinned backend for now. The agent must not download anything, so nothing else is reachable. Add a CONNECT allowlist to `proxy.py` when a second host is needed.

`sandbox.firejail = false` runs pi without the sandbox. Use it only for development.

## Prompt caching

A follow-up should cost only its new tokens. What keeps the prefix byte-identical:

- pi appends `Current date:`, the working directory and any `AGENTS.md` it finds to the system prompt. The extension's `before_agent_start` handler replaces the whole system prompt with `prompts/system.md` plus the channel's `prompt`.
- Tool definitions are static, and pi always gets the same `--tools` list.
- Changing data (the time, the game version, attachment notes) goes into the user turn as `[host]` lines, never into the system prompt.
- pi's own retries are off. Compaction stays at pi's default, which only triggers near the context limit.
- A conversation is pinned to its backend for life. New conversations are assigned round-robin.
- On llama.cpp backends (`kind = "llamacpp"`) each running conversation is pinned to a server slot with `id_slot`. With `slot_save = true`, a conversation that loses its slot to another one gets its KV cache saved (`POST /slots/N?action=save`) and restored when it comes back. This needs `--slot-save-path` on the server, and every request to that server must go through Clangy.

`tests/test_pi_integration.py` checks with the real pi that the system prompt matches the file and that each request extends the previous one byte for byte.

One known gap: Qwen chat templates drop the reasoning of earlier turns, so after a thinking turn the server re-processes the tokens from the last assistant message on. Everything before that stays cached.

`metrics.jsonl` (see Metrics) records cached and processed tokens of every request. Use it to confirm caching on a long thread.

## Progress and ETA

The host edits the progress message in place every `progress_interval` (a minute), so users can tell the run is still going. Example: `⏳ Working on it: looking things up (step 3, 2 min so far). Answer expected in 1 minute, deadline in 8 minutes.` The step is the model turn, and the minute counter changes on every edit. Once the ETA has passed it says "taking longer than expected". ETA and deadline are Discord timestamps (`<t:unix:R>`), which clients count down on their own. States come from pi's event stream: reading, thinking, writing, looking things up, analyzing, preparing files, filing a ticket.

ETA = queue position × average run time ÷ `parallel` + max(prompt tokens ÷ prompt speed + 500 ÷ generation speed, average run time). Runs with tool calls take many model turns, so the measured average run time of successful runs is the floor. The prompt token estimate is the new text ÷ 4, plus 3000 on a conversation's first run. Speeds start from the config and follow the backend's reported `timings` (llama.cpp) as a moving average. Requests that processed fewer than 500 prompt tokens don't count, since they measure overhead more than speed.

Every run's timing and token counts go into `metrics.jsonl`, see Metrics.

The deadline (`run_deadline`) starts when the run leaves the queue. pi is killed when it passes.

## Backend loss

The proxy flags a run when the backend connection fails or the backend answers with a 5xx status. The run's output is dropped (session restored, messages kept) and the progress message turns into "retry 0/3". Every `health_interval` the host checks the backends of such conversations with `GET /v1/models`. Once one answers, its conversations run again, up to `max_retries` times, and the same message shows "retry N/3". After the last failure it asks the user to mention Clangy, and there are no more automatic retries. A mention resets the count.

The conversations to retry come from the persisted state, so this also works across restarts.

A restart during a run turns that run's progress message into a failure note. The run isn't repeated.

## Game version and knowledge

A channel can fix the game (`game = "se1"`). Otherwise the agent asks when it isn't clear and calls `set_game`. The host records it, adds a `[host]` note, and immediately runs pi again in the same session with `knowledge/` linked to the matching tree. The text of the run that called `set_game` is discarded.

Each configured knowledge source of the game becomes a link in the work folder: `knowledge/skills`, `knowledge/books`, and the prepared data the skills expect in the home folder (`.se-dev`, `.se2-dev`). The links are resolved when each run starts and the other game's links are removed. The firejail whitelist contains only that game's targets.

Sources are folders used in place (the nas setup) or git repositories. `clangy sync-knowledge` (daily) keeps mirrors of the repositories. When any HEAD changed, it exports all of them into a new `releases/<timestamp>` folder and swaps the `current` symlink with an atomic rename, keeping the current and the previous release. Since links resolve per run, a run keeps one snapshot even if the sync swaps trees meanwhile.

## The books

The agent may read `knowledge/books` but must never mention it. The system prompt says so, and the host also withholds any answer containing one of `secret_markers` (by default the book repository names and `knowledge/books`), replying "Sorry, I can't share that." instead.

## Files

Received: the host downloads message attachments (up to `attachment_max_bytes`) into the conversation's `files/received/`, which the sandbox sees read-only as `attachments/`. The agent can't fetch linked files; the prompt tells it to ask for an attachment instead.

Sent: the agent writes files in its work folder (by convention `out/`) and calls `attach_files` once per group of related files. The host:

- accepts only regular files inside the work folder, not hidden ones (so `.pi/` with the session and system prompt can't leak) and not symlinks pointing outside;
- rejects files over 5 MB, files containing a `secret_markers` entry, and files already sent in an earlier answer (by work folder path, so a changed version needs a new name);
- zips a group of more than 3 files into one archive and sends smaller groups as they are, so only related files end up together;
- keeps an answer within Discord's 10 attachments and `answer_max_bytes` in total;
- stages the files in `files/outbox/`. If the run succeeds they move to `files/sent/<run number>/` and go out with the last part of the answer. If it fails they are dropped and can be sent again.

`files/` is outside the sandbox's writable area, so everything received and sent stays even if the agent deletes its own copies. Tickets point at it with a `files:` frontmatter entry.

## Store

`<data_dir>/store` holds `tickets/T-0001.md`, `notes/N-0001.md`, `plans/P-0001.md` and `records/<thread>.md`. The agent reads it through the read-only `store` link and searches it with grep. Writes go through host tools, which assign numbers with exclusive file creation, validate the title and body, and add the thread link, game and the conversation's `files` folder to the frontmatter. `ticket_add_report` appends a repeated report to an existing ticket.

## Metrics

Every run, answered or not, appends one JSON line to its conversation's `metrics.jsonl`. JSONL keeps it append-only and easy to load into pandas or DuckDB later. `clangy timings` summarizes all of them.

| Field | Meaning |
|---|---|
| `time`, `thread_id`, `channel_id`, `game`, `backend`, `model`, `run` | When and where. `run` counts the conversation's answered runs. |
| `ok`, `timed_out`, `backend_failed`, `error` | Outcome. `error` holds pi's error or exit status. |
| `answer_chars`, `tools`, `steps` | Answer length, the tools called in order, model turns. |
| `predicted_seconds`, `queue_seconds`, `run_seconds`, `total_seconds` | The ETA at trigger time, the wait for the backend, the run itself, and trigger to answer. |
| `prompt_estimate` | The prompt token estimate behind the ETA. |
| `prompt_tokens`, `cached_tokens`, `completion_tokens` | Sums over the run's requests. |
| `requests` | One entry per backend request: `time`, `slot`, `status`, `seconds`, `first_token_seconds`, `prompt_tokens`, `cached_tokens`, `completion_tokens`, the backend's raw `timings` (llama.cpp: `prompt_n`, `prompt_ms`, `prompt_per_second`, `predicted_n`, `predicted_per_second`, `cache_n`, ...), and `error` if it failed. |

`prompt_tokens` is the whole prompt; `prompt_tokens - cached_tokens` is what the backend actually processed.

## Backend connections

The proxy opens a new connection for every backend request. llama.cpp closes idle keep-alive connections after 5 seconds, and a request racing that close would look like a lost backend.

The host socket is bound through `/proc/self/fd/<dir fd>/host.sock` and pi connects to it relative to its working directory, because unix socket paths are limited to 107 bytes.

## Persisted state

`<data_dir>/conversations/<thread id>/`:

- `state.json`: channel, backend, game, status, queued messages, progress message id, retry count. Host only.
- `files/`: `received/` attachments, `sent/<run>/` files sent in answers, `outbox/` while a run is staging files. Host only.
- `work/`: the sandbox's folder: the files the agent writes (`out/`), the `attachments`, `store`, `guide` and knowledge links, `.pi/` (session, system prompt, pi config) and `.clangy/host.sock` during a run.
- `metrics.jsonl`: per-request token and timing stats.
