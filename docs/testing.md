# Testing

```bash
uv run pytest
```

The suite runs offline in a few seconds. `tests/conftest.py` fails any connection that isn't loopback or a unix socket, and CI (`.github/workflows/tests.yml`) runs pytest in a network namespace that has only loopback.

- `tests/fakes.py` has a scripted OpenAI compatible backend (streaming, reasoning, tool calls, usage, llama.cpp `timings` and slot endpoints, and failures: error statuses with `Retry-After`, stalls before and during the stream, errors inside the stream) and a fake Discord `Chat`. The `backend` and `backend2` fixtures start one each.
- `tests/test_core.py` drives the core through the fake chat with a fake agent that talks to the host socket like the pi extension does: triggers, queueing, splitting, timeouts, backend failures, failover and balancing, game switching, tools, files, the secret guard, restarts.
- `tests/test_files.py` covers the checks and ZIP grouping of files the agent sends.
- `tests/test_pi_integration.py` runs the real pi with Clangy's extension against the fake backend. It is skipped where pi isn't installed (CI) and takes about a minute. It also checks that a session moves between backends unchanged and that pi ends its turn when a stream breaks.

## Not covered yet

- firejail itself: the tests check the command line, not a jailed run. Run one manually once firejail is installed on the host.
- Replaying past support threads against a real model. `scripts/replay.py` replays one made-up thread against a llama.cpp server (`--url`) or the backends of a config file (`--config`, `--backend`) and prints the timings and token counts.
- A live Discord test on the test application.
