# Design

Clangy v2 is a Python host process that connects to Discord and runs one short-lived [pi](https://pi.dev) agent per answer. Each Discord thread is a conversation with its own work folder and pi session, so a follow-up resumes where the last answer ended.

## Modules

| Module | Role |
|---|---|
| `__main__.py` | The `clangy` command: `run`, `check`, `sync-knowledge`, `timings`. |
| `config.py` | The TOML config and its validation, see configuration.md. |
| `messages.py` | Discord messages as the core sees them (`IncomingMessage`, `Attachment`). |
| `discord_bot.py` | discord.py client. Converts gateway messages to `IncomingMessage` and implements the `Chat` protocol (send, edit, create thread, download, a thread's channel). The only module that knows about Discord. |
| `core.py` | Channel rules, conversations, queued messages, one run per conversation, progress messages, retries, host tools, persistence, the developers' posts from the outbox. |
| `backend.py` | Per-backend run queue (`parallel` runs at once), runs ahead, measured prompt and generation speeds, ETA, health checks and cooldowns, llama.cpp slot pinning and save/restore. |
| `proxy.py` | Per-run HTTP server on a unix socket inside the work folder: forwards chat completions to the run's backend, detects backend failures, and serves the host tools. |
| `runner.py` | Writes pi's config, builds the firejail command line, runs pi with a deadline, reads its JSON event stream, and replaces the answer in pi's session when the host shortened it. |
| `store.py` | Tickets, notes and plans in the workspace, and the records of `record` channels. |
| `tracker.py` | Tickets and notes in a Vikunja project instead, and their Markdown copy for the sandbox. |
| `knowledge.py` | The daily sync of knowledge kept in git repositories. |
| `cache.py` | The local message cache (`messages.sqlite`). |
| `timings.py` | `clangy timings`, the summary of `metrics.jsonl`. |
| `check.py` | `clangy check` and the sandbox probe that `clangy run` requires to pass. |
| `files.py` | Files the agent sends: validation, at-most-once sending, ZIP grouping. |
| `text.py` | Discord texts: answer splitting, cutting copied turns from answers, footer, progress and error messages. |
| `pi/clangy.ts` | pi extension loaded in the sandbox: replaces the system prompt, bridges the host and proxy sockets, defines the host tools. |
| `sandbox-bin/` | `rg`, `fd` and `find` wrappers that follow links. |
| `workspace/` | `pyproject.toml` and `uv.lock` of the sandboxes' shared Python environment, see Sandbox. |
| `prompts/system.md`, `guide/*.md` | The system prompt, and the guide files the agent reads on demand. |
| `templates/` | The workspace's `AGENTS.md` and ticket template, written when missing. |

## A run

1. A trigger (see below) posts a progress message right away and starts the conversation's worker. A trigger during a run sets a flag, and the worker runs again afterwards.
2. The worker picks the backend (see Backends) and waits for a slot on it. The progress message shows the ETA (see Progress and ETA).
3. The host snapshots the pi session file, refreshes the work folder links (`attachments`, `store/*`, `guide`, the game's knowledge), writes pi's `models.json` and `settings.json`, and opens the host socket.
4. pi runs in firejail with `--mode json --print --session <file>`. The prompt is a `[host]` timestamp line, then every queued message as `Name: text` plus attachment notes, and `[reactions: 🙏 1]` when it has reactions. Custom emojis in the text become `:name:`. In forums, notes give the post's title and tags when the model hasn't seen them yet or they changed: the title is often the question itself, and the tags name the product, like Magnetar for a dedicated server.
5. On success the queued messages are dropped, the answer replaces the progress message, and parts over 2000 characters go into follow-up messages. Every answer ends with the fixed mention reminder. Some models write the conversation out again before their answer. When an answer starts with one of Clangy's earlier answers (40 characters or more), the host cuts that and any further copied answers and `Name: text` lines that follow, and puts the shortened answer into the pi session too, since later answers would copy the copy. An answer that is only a copy is posted as it is.
6. On a timeout, backend failure or other failure the session file is restored from the snapshot, so partial output never reaches the next run, and the queued messages stay queued.

### When Clangy responds

- A new forum post in a `support` or `record` channel starts a conversation. It gets an answer only if `initial_response` is on.
- In a `mention` channel, a message mentioning Clangy starts a thread from that message. The thread is the conversation, and Clangy never writes in the channel itself. The channel's recent messages before the mention go along as context, attachments included, since people often write the question across several messages or post a screenshot first. The prompt puts them in a block marked "for reference only" before the mention: the mention is the task, and the earlier messages are used only where it refers to them.
- Inside known conversations, every message is queued, and only mentions trigger a run.
- With `initial_delay`, the first answer to a new forum thread waits until the thread has been quiet that long; every message, edit or deletion restarts the wait, and nothing is posted meanwhile. A mention cancels the wait and is answered right away. A wait cut short by a restart is dropped.
- A mention in a thread Clangy doesn't know yet (started before it ran, or by someone else in a text channel) starts a conversation in that thread, with the whole thread as context, read from Discord. A thread is one conversation, and its first post is often the request itself. Clangy's own answers in it come along as `Clangy (you): text`, without the footer; its progress and error messages and v1's answers (which have no footer) don't.
- Without context Clangy doesn't answer: if the history can't be read, it logs an error and stays silent.
- Nothing is answered after the fact: messages posted while Clangy was down are context for later mentions, never triggers. After every gateway (re)connect, the first message in a known thread first reads all of the thread from Discord and queues ahead of that message what the conversation doesn't have yet: what was posted while Clangy was down, and, as earlier messages for reference, older messages it never read, such as the start of a thread that an older version joined on a mention without reading it.
- `record` channels also append every message to `<data_dir>/records/<thread>.md` (`store/records/` in the sandbox), without inference, so other conversations can find requests later. A record expires `retention_days` after its last message (see Retention).

### Context and the message cache

A mention in a thread Clangy doesn't know yet reads all of the thread from Discord. In a channel, a mention's context reaches back to midnight UTC, but at least `history_hours` (4), and at most `history_messages` (100) messages. Reading that from Discord is slow, so Clangy keeps every message of its channels and their threads in `<data_dir>/messages.sqlite` as it arrives, applies edits and deletions to it, and prunes it after two days, or after `retention_days` if that's shorter. On every gateway (re)connect it catches up the mention channels' context windows from Discord, one request each, since a restart or a dropped session loses events. A mention reads its context from the cache and only asks Discord for what the cache doesn't cover, and caches that too. Attachments of older messages may have expired links; they become a note instead of a file.

A message that the mention or its context replies to comes along even if it is older, one level deep: Discord sends it with the reply. Replies show it in the prompt as `Name (reply to Other): text`. A thread started from a channel message opens with an empty message that points to that message, so the same rule brings the thread's first message along. The cache keeps the id of the message a reply points to, and caches that message too, without overwriting a row it already has. A new cache schema (`user_version`) drops the old table, and the catch-up fills it again.

### Edits and deletions

Clangy remembers the author, text and time of every message in its conversations, its own answers included (`known` in `state.json`), until the conversation expires (see Retention). An edit or deletion of a message still waiting for the next run updates or removes it in the queue. For a message the model has already seen, or one the running answer is working from, the next run gets a `[host]` note: the new text, or the deleted text with an instruction to disregard it. A running answer is never interrupted. Edits don't trigger answers, even if they add a mention. Clangy ignores edits of its own messages and edits that only add link previews. In `record` channels edits and deletions are logged to the record too.

Reactions work the same way. Clangy counts them per message from the reaction events. A queued message shows its current counts. A new reaction to a message the model has already seen becomes a `[host]` note naming who reacted, and a removed one only changes the count. Reactions don't trigger answers or restart the `initial_delay` wait, and aren't logged to records or kept in the message cache.

## Sandbox

pi runs as `firejail ... -- env -i <vars> pi ...`:

- `--net=none`: the sandbox has only its own loopback. The pi extension listens on `127.0.0.1:$CLANGY_PORT` and pipes connections to the host socket in the work folder. That socket is the only way out.
- The environment is replaced. The Discord token and backend API keys stay on the host. The proxy adds the API key to forwarded requests.
- The work folder is the only writable path. The attachments, the store folders, the guide, the `[[mounts]]` (the nas: `~/repos` and the skills' prepared data), the conversation's knowledge snapshot, the shared Python environment, the package and the configured `sandbox.read_only` paths (node, pi, uv's Pythons) are read-only. `sandbox.env` adds environment variables, e.g. to keep uv offline for the skills' scripts.
- No capabilities, no root, seccomp, private `/dev` and `/tmp`.
- No per-sandbox memory limit: firejail's `--rlimit-as` breaks Node.js, whose WebAssembly reserves large address ranges. The systemd unit caps the whole service with `MemoryMax` and `TasksMax` instead.

With `sandbox.proxy` set, commands in the sandbox can also reach an HTTP proxy with a domain allowlist (squid on the host). firejail can't filter by domain, so the sandbox keeps `--net=none`: the host relays a second unix socket in the work folder (`.clangy/proxy.sock`) to the proxy, the extension bridges a loopback port to it, and `HTTP_PROXY`, `HTTPS_PROXY` and their lowercase forms point at that port, with `NO_PROXY` for loopback. curl, wget, git, Python's urllib and requests, and uv honor these. Downloading files users link to stays forbidden by the prompt; the proxy is for looking things up, e.g. plugin sources on GitHub.

`clangy check` and `clangy run` test the proxy from inside the sandbox: an unreachable proxy is only reported, a proxy that lets `example.com` through stops the start.

All sandboxes share one Python environment, mounted read-only with its `bin/` first on the `PATH`, so `python3` has numpy, matplotlib and pydantic. Python finds the venv from the `pyvenv.cfg` next to the interpreter, so nothing else needs activating, and the sandbox needs neither uv nor the project files for it. The host builds it on first use with `uv sync --frozen --compile-bytecode` from `workspace/pyproject.toml` and its `uv.lock`, in `<data_dir>/python/<hash>/.venv`:

- A venv holds absolute paths (`pyvenv.cfg`, script shebangs), so it can't be moved after the build. firejail mounts it at the same path, so they stay valid. The folder is named after a hash of the lock and the interpreter's real path, so a changed lock (after a deploy) or a new Python version (after an upgrade) gets a new folder, and answers already running on the old one go on. A `.complete` marker tells a finished build from one cut short, which is built again. Old folders aren't removed.
- Bytecode is compiled at build time, since Python can't write `__pycache__` into a read-only mount and would otherwise compile numpy and matplotlib on every run.
- uv may hardlink the files from its cache: the sandboxes can't write to them, and the cache isn't visible inside.
- A failed build is logged, and answers go ahead without the environment. `clangy check` builds it and reports its path.

To change the packages, edit `workspace/pyproject.toml`, run `uv lock` there, and deploy.

The prompt also offers `pandoc` and the archive tools (`tar`, `gzip`, `xz`, `bzip2`, `zstd`, `zip`, `unzip`, `7z`) from the host's `/usr/bin`.

`sandbox.firejail = false` runs pi without the sandbox. Use it only for development.

## Prompt caching

A follow-up should cost only its new tokens. What keeps the prefix byte-identical:

- pi appends `Current date:`, the working directory and any `AGENTS.md` it finds to the system prompt. The extension's `before_agent_start` handler replaces the whole system prompt with `prompts/system.md` plus the channel's `prompt`.
- Tool definitions are static, and pi always gets the same `--tools` list.
- Changing data (the time, the game version, attachment notes) goes into the user turn as `[host]` lines, never into the system prompt.
- pi's own retries are off. Compaction stays at pi's default, which only triggers near the context limit.
- A conversation stays on the backend of its last run unless that one fails or is clearly slower, see Backends.
- pi sees the same model id (`clangy`) on every backend, and the proxy sets the real one. pi turns the reasoning of another model's turns into plain text, so a different id per backend would change the session's earlier turns whenever a conversation moves.
- On llama.cpp backends (`kind = "llamacpp"`) each running conversation is pinned to a server slot with `id_slot`. With `slot_save = true`, a conversation that loses its slot to another one gets its KV cache saved (`POST /slots/N?action=save`) and restored when it comes back. This needs `--slot-save-path` on the server, and every request to that server must go through Clangy.

`tests/test_pi_integration.py` checks with the real pi that the system prompt matches the file and that each request extends the previous one byte for byte.

One known gap with thinking on: Qwen chat templates drop the reasoning of earlier turns, so after a thinking turn the server re-processes the tokens from the last assistant message on. Everything before that stays cached.

`metrics.jsonl` (see Metrics) records cached and processed tokens of every request. Use it to confirm caching on a long thread.

## Progress and ETA

The progress message only says when the answer is expected, as a Discord timestamp (`<t:unix:R>`) that clients count down on their own: `⏳ Answer expected in 1 minute.` It adds details only when something is off: `⏳ Waiting for 2 other answers first.` while queued behind other conversations, `⏳ Taking longer than expected, deadline in 8 minutes.` once the ETA has passed, and `(retry 1/5)` on a run after waiting for a backend. The host checks it every `progress_interval` and edits it only when the text changes.

ETA = runs ahead × average run time ÷ `parallel` + max(prompt tokens ÷ prompt speed + 500 ÷ generation speed, average run time). Runs with tool calls take many model turns, so the measured average run time of successful runs is the floor. The prompt token estimate is the new text ÷ 4, plus 3000 on a conversation's first run. Speeds start from the config and follow the backend's reported `timings` (llama.cpp) as a moving average. Requests that processed fewer than 500 prompt tokens don't count, since they measure overhead more than speed.

Every run's timing and token counts go into `metrics.jsonl`, see Metrics.

The deadline (`run_deadline`) starts when the run leaves the queue. pi is killed when it passes.

## Backends

Backends are used by priority, and a conversation moves between them only when needed. Switching is kept rare because a backend that hasn't seen a conversation has to read all of it: tens of thousands of tokens, a minute or more on polaris. OpenRouter caches prompts too (`cached_tokens` in its usage), so staying put pays off there as well.

Each backend has a `priority`, lower is preferred. A backend is free when it would start the run without queueing. Before each run the host picks the backend:

- New conversation, its backend was removed from the config, or its backend failed: the most preferred free backend, the lowest ETA (see Progress and ETA) among equals. If none is free, the lowest ETA of all, ties by priority.
- Existing conversation whose backend works: it moves to a more preferred backend that is free (reason `priority`). Otherwise it stays, unless another backend's ETA for the whole conversation (the last answer's prompt and completion tokens plus the new text) plus `switch_margin` beats the current backend's ETA for the new text alone (`balance`). Only backends of the same priority count for that, and less preferred ones too while the conversation's backend is fully busy. In practice it moves down when its backend is busy and another one is idle, and back up as soon as the preferred one has room.
- Remaining ties go to the backend listed first.

With the nas priorities, conversations run on OpenRouter's free models, move to polaris while those are rate limited or both slots are busy, and to the paid models (the cheaper one first) only when polaris fails or is busy too. Moving back up is cheap: OpenRouter reads a 10k-token conversation in about a second.

The proxy flags a backend failure on a connection error, HTTP 401, 402, 403, 404, 408, 429 or 5xx, an error event inside a stream that started with HTTP 200 (OpenRouter does this when the provider fails), no data within `first_byte_timeout`, or a stream that stops for `stall_timeout`. After a failure it refuses the run's further requests, and on a broken stream it sends pi an error event, so pi ends the turn with an error instead of taking a cut-off answer as complete. The run's output is dropped (session restored, messages kept).

The failed backend is marked unavailable and the run starts again right away on another one. Users see no warning, only the new ETA in the progress message. Each failure moves the run on at most once per other backend; when no backend is available, or that many failed, the progress message says the backend was lost and the conversation waits. A llama.cpp server comes back when its health check (`GET /v1/models`, every `health_interval`, and right away when a run finds no backend) answers. Other kinds come back after `cooldown`, or the `Retry-After` they sent: OpenRouter's model list answers even while every free model is rate limited, so it proves nothing. The cooldown doubles with each further failure in a row, up to `max_cooldown`, and a successful run resets it, so a free model that stays overloaded or rate limited for hours doesn't cost every question a failed run. Every `health_interval` the waiting conversations run again once any backend is available.

Only runs after such a wait are retries. They count up to `max_retries` from the last mention or answer. After the last failure Clangy asks the user to mention it, and there are no more automatic retries. The conversations to retry come from the persisted state, so this also works across restarts.

A llama.cpp slot owned by a conversation that moved away is simply taken over by the next one.

A restart during a run turns that run's progress message into a failure note. The run isn't repeated.

## Game version and knowledge

A channel can fix the game (`game = "se1"`). Otherwise the agent settles it from the conversation and calls `set_game`: a game a user names, SE2 when an executable, path or log is SE2's, else SE1 by default, which it mentions in a few words so users can correct it. It asks only when the hints contradict each other, and calls `set_game` again if a later message shows the other game. The host records it and links `knowledge/` to the matching tree, and the tool's reply tells the model the game and lists its skills, so the run goes on with the knowledge in place. If the run fails, its session is restored, so the same text goes into the next run's prompt as a `[host]` note.

When the game is known, the `set_game` reply (or the `[host]` note of a channel's fixed game) lists the skills under `knowledge/`: every `SKILL.md` up to two folders deep in the game's knowledge and in the mounts linked there, with the name and description from its frontmatter and its path, like pi's own skill list. `hidden_skills` leaves some out, such as the plugin review skill, which posts to GitHub. The model is told to read the matching `SKILL.md` and follow it. pi's own skill loading isn't used, because Clangy replaces pi's system prompt to keep it cache-stable, and the list depends on the game.

A mount is linked from the first run, before the game is known.

The writing rules are a short version of the [humanizer](https://github.com/blader/humanizer) skill in the system prompt itself. The free models skipped reading a writing skill in about half of the answers that needed it, and rules in the prompt apply to every answer.

The knowledge is linked into the work folder, and ripgrep and fd don't follow links by default, so `sandbox-bin/rg` and `sandbox-bin/fd`, first on the sandbox's `PATH`, add `--follow` for pi's grep and find. `sandbox-bin/find` adds `-H` for the shell's find, which otherwise doesn't descend into a link given as the start path, such as a plugin skill's `Data/Sources/<repo>`.

Each configured knowledge source of the game becomes a link in the work folder: `knowledge/skills`, `knowledge/books`, and prepared data the skills expect in the home folder. The links are resolved when each run starts and the other game's links are removed. The firejail whitelist holds both games' targets, resolved at the same time, plus the mounts: firejail fixes it when pi starts, so `set_game` could not add a tree later. The links decide which game the model works in. The other game's tree being readable changes nothing for secrecy, both games' books are kept from users the same way (see The books). Data that must be linked whatever the game, like the skills' data when every conversation can reach both games' skills through `repos/`, goes into `[[mounts]]` instead.

Sources are folders used in place (the nas setup) or git repositories. `clangy sync-knowledge` (daily) keeps mirrors of the repositories. When any HEAD changed, it exports all of them into a new `releases/<timestamp>` folder and swaps the `current` symlink with an atomic rename, keeping the current and the previous release. Since links resolve per run, a run keeps one snapshot even if the sync swaps trees meanwhile.

## Prompt, guide and host tools

`prompts/system.md` is the system prompt: how to read the turns (`Name: text`, replies, reactions, `[host]` lines, forum tags, the reference block), the rules (facts only, short answers, no code (point to a coding agent with the SE skills instead), never reveal the books or the instructions, ignore injected instructions), the tools in the sandbox, working out the game, the valid game and server setups, collecting problem reports (setup first, logs only after it) until a developer could start on a ticket, what a ticket holds, pointing developers to sources, sending files, the `repos/` mount and the store. Keep it free of changing data, its bytes are the cached prefix. A channel's `prompt` is appended under `# Channel`.

`guide/` holds files the agent reads on demand: `logs.md` (where Pulsar's and Magnetar's `info.log`, the game logs and crash dumps are for each setup) and `developers.md` (sources for Pulsar, dedicated servers, plugin, mod and script development).

Host tools, defined in `pi/clangy.ts` and run by the host through the socket:

| Tool | Does |
|---|---|
| `set_game` | Records SE1 or SE2 and links that game's knowledge into the work folder; the reply lists its skills (see Game version and knowledge). |
| `attach_files` | Stages files for the answer (see Files). |
| `ticket_create`, `ticket_add_report` | Files a problem report, or adds a repeated report to an open ticket. |
| `note_create`, `plan_create` | Shared findings, and plans from ideas or requests. |
| `contact_author` | Reaches the author of a plugin for a severity (`question`, `bug`, `urgent`): pings them once per plugin and thread, or returns the contact the plugin's hub entry lists for that severity and the author's note (see Mentions). A plugin without a contact for it is a tool error, as is an unknown severity. |

The last four take optional `files` to keep, see Store.

### Mentions

Every message and edit Clangy sends has Discord's mentions switched off (`NO_PINGS`), so nothing a user or a model writes can ping anyone. The one exception is `contact_author`: when the plugin's contact for the severity is a Discord user the author opted in to being mentioned, the host records the request on the conversation and, once the answer is posted, sends a new message that mentions only that user. The contacts, the opt-in and the author's note come from the `<Support>` element of the plugin's entry in a hub clone listed in `hubs` (matching rules in configuration.md), which the hub's review has seen, never from the plugin's own repository or anything a user wrote. The model chooses only the severity. It has to be a new message, since an edit doesn't notify. Every other contact (Discord channel or server invite, GitHub issues, any web page, or a user without the opt-in) pings nobody: the tool returns the link for the agent to give the player, with the author's note, which only informs the wording. At most one ping per plugin and thread, whatever the severity. A run that fails posts no ping, and a retry may ask again.

A tool error goes back to the model as a failed tool call. `runner.HOST_TOOLS` must list the same names, since pi 1.x applies `--tools` to extension tools too; a test checks this.

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

`files/` is outside the sandbox's writable area, so everything received and sent stays even if the agent deletes its own copies, until the conversation expires (see Retention). Files a ticket needs are copied into the workspace, or attached to its task in Vikunja, see Store.

## Store

Tickets, notes and plans are for the developers, so they live in a workspace laid out like the developers' own (`workspace_dir`, the `clangy` home folder on the nas) and outlive the conversations. With `[tasks]` the tickets and notes are in Vikunja instead, see Tickets and notes in Vikunja below:

- `AGENTS.md`: the rules for agents working in the workspace.
- `tickets/DIS-0001.md`, with `Template.md`, and `completed/` and `canceled/` for the tickets developers have finished.
- `notes/N-0001.md`, or `notes/N-0001/` with `NOTE.md` and files. The same for `plans/` with `PLAN.md`.

The host writes `AGENTS.md` and the template from `templates/` when they are missing, and never touches them after that, so they can be edited on the host.

Records of `record` channels hold message text and expire with the conversations, so they stay in `<data_dir>/records`.

Without `[tasks]` (see Tickets and notes in Vikunja below), the agent reads `tickets/`, `notes/`, `plans/` and the records through read-only links under `store/` and searches them with grep. Nothing else of the workspace is visible in the sandbox. Writes go through host tools, which assign numbers with exclusive file creation (tickets count `completed/` and `canceled/` too), validate the title (at most 120 characters) and body (at most 20000), and add the thread link and game to the frontmatter. A ticket's body goes under `# Description`, followed by an empty `# Work log` for the developer.

`files` lists attachments (`attachments/...`) and files the agent wrote, each with what it shows. Hidden files and anything else outside the work folder are refused, as are files over `attachment_max_bytes`. A ticket's files are copied into a new note, `N-0001/` with `NOTE.md` listing each file, what it shows and who posted it when; the ticket's `notes:` frontmatter names the note. A note or plan with files gets its own folder for them.

`ticket_add_report` adds a repeated report of the same problem to an open ticket as a `## Report` section above `# Work log`, its files in a new note linked the same way. A report on a completed or canceled ticket is refused: if the problem came back, the agent files a new ticket naming the old one.

### Tickets and notes in Vikunja

With `[tasks]` configured, tickets and notes live in a Vikunja project (on the nas: Support) instead of `tickets/` and `notes/`, so developers work on them there. Plans and records stay in the workspace. The host calls the API in a thread, with an API key that reaches only that project.

- A ticket is a task titled `DIS-0001: <title>`. The host assigns the number: one more than the highest in the project, done tasks included, but at least `first_number`. Users were told the ids, so they are never reused.
- Its description starts with a list of the frontmatter fields (`created`, `thread`, `game`, `notes`), followed by the body as HTML from Markdown. The game is also a label.
- Files are uploaded as the task's attachments and listed in the description or report. Text files (logs, Markdown) from `gzip_min_bytes` on that gzip to at most 70% of their size are attached gzipped as `<name>.gz`; binary files such as images stay as they are. A file over `max_file_bytes` (the server's limit) even then is refused like one over `attachment_max_bytes`, before anything is written.
- A note is a task titled `N-0001: <title>` with the `note_label`, moved into the Kanban board's `notes_bucket`, so notes stay apart from the tickets but can be linked and hold attachments. Its number follows the highest in the project and in the workspace's `notes/`. It's linked (`related`) to every ticket its text names.
- `ticket_add_report` adds a `Report` comment, with the thread and time, to an open task. On a done task it's refused; a done task with the `canceled_label` is canceled, otherwise completed.
- A post from the outbox adds a `Work log` comment.

The sandbox's `store/tickets` links to `<data_dir>/tickets` and `store/notes` to `<data_dir>/notes`, a Markdown copy of the project the host writes: open tickets at the top, done ones in `completed/` and `canceled/`, notes in their own folder, each with the frontmatter (with the `related` documents), the description, the attachment names and the comments. The host rewrites it at startup, every `refresh_interval`, and after each of its own writes, file by file, since a running sandbox has the folder mounted. If Vikunja can't be read, the old copy stays.

`scripts/migrate-tickets-to-vikunja.py` copied the workspace's tickets and notes in once, see deployment.md. The workspace's `tickets/` and `notes/` folders stay as they were, an archive.

The workspace has a git history, so a deleted or overwritten document can be brought back. Its repository is `.history.git` in the workspace root, outside anything the sandbox can see, and only the host commits to it. Every write by a host tool is a commit by Clangy that names the Discord user who asked and the thread. Edits made by hand on the host go into a separate "Changes made outside Clangy" commit before the next write, and at startup. Only `AGENTS.md`, `tickets/`, `notes/` and `plans/` are tracked, since the workspace may be a home folder. If the repository has an `origin` remote, every commit is pushed there in the background. A failed commit or push is logged and doesn't stop the write. To bring a file back: `git --git-dir=.history.git --work-tree=. checkout <commit> -- <path>`.

## Posts from the developers

Developers answer a reporter through Clangy, which is in the thread anyway: to tell them a ticket is fixed and which release has the fix, or to ask for something a ticket is missing. They queue the message in `<data_dir>/outbox/`, and the running bot looks there every 10 seconds. Nothing is posted on its own, closing a ticket included.

A queued message is a Markdown file, `<name>.md`, with the thread in its frontmatter and the text below it:

```
---
to: DIS-0017
---

The fix is in Bugfixes 1.7.0, which Pulsar downloads on its next start.
```

`to:` is a ticket, open or closed, whose `thread:` names the thread, or a thread id. Clangy posts only to a thread it has a conversation, record or ticket of, and only if Discord says the thread is in one of its channels. The text has to fit one Discord message with the mention footer, which every post ends with like an answer. Write the file under another name first and rename it, so the bot never reads half of it.

The name is the post's key. Before posting, Clangy creates `sent/<name>.md` with exclusive file creation, then posts, then adds the time and the message link to it. A name that is already in `sent/` is refused, so queuing the same file again never posts twice, and neither does a restart in the middle of a post: its `sent/` file stays without a `message:`, and the thread shows whether it went out. A refused or failed message moves to `failed/<name>.md` with an `error:` line. If posting failed, the `sent/` file is removed again, so the message can be fixed and queued under the same name.

A post shows up where it matters:

- Each ticket of the thread gets a work log line with the message link, as a history commit.
- If the conversation is still kept, the post joins its known messages as Clangy's own, and the next run gets a `[host]` note with the text and the ticket. A reporter who answers with a mention gets an answer as usual, and the model can add what they say to the ticket with `ticket_add_report`.
- In a thread whose conversation expired, the post has the footer, so a later mention reads it back as one of Clangy's answers (see When Clangy responds).

## Metrics

Every run, answered or not, appends one JSON line to its conversation's `metrics.jsonl`. JSONL keeps it append-only and easy to load into pandas or DuckDB later. `clangy timings` summarizes all of them.

| Field | Meaning |
|---|---|
| `time`, `thread_id`, `channel_id`, `game`, `backend`, `model`, `run` | When and where. `model` is the configured one; `run` counts the conversation's answered runs. |
| `answered_by` | The models that actually answered the run's requests, from the stream's `model` field. With OpenRouter's `models` list these can differ from `model`, even within one run. |
| `backend_switch` | Present when the run moved the conversation: `from` (null for a new conversation) and `reason` (`new`, `failure`, `priority` or `balance`). |
| `ok`, `timed_out`, `backend_failed`, `error` | Outcome. `error` holds pi's error or exit status. |
| `answer_chars`, `tools`, `steps` | Answer length, the tools called in order, model turns. |
| `predicted_seconds`, `queue_seconds`, `run_seconds`, `total_seconds` | The ETA at trigger time, the wait for the backend, the run itself, and trigger to answer. |
| `prompt_estimate` | The prompt token estimate behind the ETA. |
| `prompt_tokens`, `cached_tokens`, `completion_tokens` | Sums over the run's requests. |
| `requests` | One entry per backend request: `time`, `slot`, `status`, `model`, `seconds`, `first_token_seconds`, `prompt_tokens`, `cached_tokens`, `completion_tokens`, the backend's raw `timings` (llama.cpp: `prompt_n`, `prompt_ms`, `prompt_per_second`, `predicted_n`, `predicted_per_second`, `cache_n`, ...), and `error` if it failed. |

`prompt_tokens` is the whole prompt; `prompt_tokens - cached_tokens` is what the backend actually processed.

## Backend connections

The proxy opens a new connection for every backend request. llama.cpp closes idle keep-alive connections after 5 seconds, and a request racing that close would look like a lost backend.

The host socket is bound through `/proc/self/fd/<dir fd>/host.sock` and pi connects to it relative to its working directory, because unix socket paths are limited to 107 bytes.

## Persisted state

`<data_dir>/messages.sqlite` caches the messages of Clangy's channels (see Context and the message cache). `<data_dir>/python/<hash>/` holds the sandboxes' Python environment (see Sandbox). `<data_dir>/outbox/` holds the developers' posts, queued, `sent/` and `failed/` (see Posts from the developers).

`<data_dir>/conversations/<thread id>/`:

- `state.json`: channel, backend and the conversation's size after its last answer (for the switch rule), game, forum title and tags, status, queued messages, pending `[host]` notes, the conversation's known messages with their reactions, progress message id, retry, failover and answer counts, the files already sent. Host only.
- `files/`: `received/` attachments, `sent/<run>/` files sent in answers, `outbox/` while a run is staging files. Host only.
- `work/`: the sandbox's folder: the files the agent writes (`out/`), the `attachments`, `store`, `guide` and knowledge links, `.pi/` (session, its snapshot `session.bak` from before the current run, system prompt, pi config) and, during a run, `.clangy/host.sock` and `.clangy/proxy.sock`.
- `metrics.jsonl`: per-request token and timing stats.

If a conversation has answers but its pi session file is gone, for example because it was deleted by hand, Clangy notices on startup and queues all of its known messages as earlier messages for reference, Clangy's answers as `Clangy (you)`, with the game's knowledge note and, at the next message, the forum title and tags. The next run starts a new session that has the whole conversation again. Answers posted before Clangy kept its own answers in `known` are read back from the thread.

### Retention

At startup, before loading the conversations, Clangy deletes what it kept of conversations nobody touched for `retention_days` (30): everything in the conversation's folder except `metrics.jsonl`, which holds no message text and keeps `clangy timings` complete. A conversation's age is the modification time of its `state.json`, which every message, edit and answer rewrites. A folder without `state.json` goes by its own modification time. `state.json` is deleted last, so a cleanup cut short is finished on the next start. Records (`<data_dir>/records/<thread>.md`) go by their modification time too. The message cache is pruned at the same time.

The service restarts daily (see deployment.md), so nothing is kept more than a day past the limit. A mention in an expired thread starts a new conversation, with the whole thread read from Discord, Clangy's earlier answers included (see When Clangy responds). Tickets, notes and plans are in the workspace, with copies of their files, so they stay. Copies outside `data_dir` are not covered: llama.cpp slot saves on the backend host (`--slot-save-path`) and the service's journal.
