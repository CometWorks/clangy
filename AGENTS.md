# Clangy

Discord support bot. Python host (`src/clangy`) runs one pi agent per answer in firejail. Read `docs/design.md` before changing behavior.

- `uv run pytest`: must pass offline; no test may reach the network.
- Format with `black` (line length 120).
- `core.py` must stay Discord-free; Discord specifics belong in `discord_bot.py`.
- Keep `prompts/system.md` and the tool definitions in `pi/clangy.ts` small and free of changing data. Their bytes are the cached prompt prefix.
- `tests/test_pi_integration.py` runs only where `pi` is installed; run it after touching the extension, runner or prompt.
