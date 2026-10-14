# Clangy's workspace

Clangy, the support bot of the Pulsar Discord server, files tickets, notes and plans here from its support threads. Developers and their coding agents work on them. Clangy itself never does.

## Folders

- `tickets`: problem reports, one Markdown file each, like `DIS-0001.md`. Follow `tickets/Template.md`. Move a finished ticket to `tickets/completed/` and a dropped one to `tickets/canceled/`, and set its `status` to match.
- `notes`: findings, and the files that belong to tickets. A note is either `N-0001.md`, or a folder `N-0001/` with `NOTE.md` and the files.
- `plans`: ideas and requests, for example for a plugin, mod or script. A plan is either `P-0001.md`, or a folder `P-0001/` with `PLAN.md` and its files.

On the nas this is the home folder of the `clangy` user. Its other folders belong to the bot: `clangy` is the deployed working copy, replaced by every deploy, so change the code in https://github.com/CometWorks/clangy instead. `data` holds the conversations, which Clangy deletes 30 days after their last message. A ticket's files survive that, because Clangy copies them into a note when it files the ticket.

## Rules

- Ids are a prefix and the next number after the highest one in the folder, `completed` and `canceled` included. Clangy assigns them itself. When you add a document by hand, do the same. Never reuse or renumber an id, since users were told the ticket ids in Discord.
- Keep the frontmatter Clangy wrote (`id`, `thread`, `game`, `notes`, `ticket`). Add fields as needed.
- Clangy appends reports of the same problem from other threads to the open ticket as `## Report` sections, above `# Work log`.
- Record your work in the ticket's `# Work log`. Put supplementary files into a new note and add its id to the ticket's `notes`.
- Clangy reads `tickets`, `notes` and `plans` while answering in Discord, to find duplicates. Never write secrets into them, or anything from the private developer books.
- `clangy.toml` holds the Discord token and API keys. Never print, copy or commit it.
