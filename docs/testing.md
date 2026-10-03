# Testing

```bash
uv run pytest
```

The suite runs offline in about a second. `tests/conftest.py` fails any connection that isn't loopback or a unix socket, and CI (`.github/workflows/tests.yml`) runs pytest in a network namespace that has only loopback.

- `tests/fakes.py` has a scripted OpenAI compatible backend (streaming, tool calls, usage, llama.cpp `timings` and slot endpoints) and a fake Discord `Chat`.
- `tests/test_core.py` drives the core through the fake chat with a fake agent that talks to the host socket like the pi extension does: triggers, queueing, splitting, timeouts, backend loss and retries, game switching, tools, files, the secret guard, restarts.
- `tests/test_files.py` covers the checks and ZIP grouping of files the agent sends.
- `tests/test_pi_integration.py` runs the real pi with Clangy's extension against the fake backend. It is skipped where pi isn't installed (CI) and takes about 10 seconds.

## Not covered yet

- firejail itself: the tests check the command line, not a jailed run. Run one manually once firejail is installed on the host.
- Replaying past support threads against a real model, and measuring latency and cached vs. uncached tokens per turn on the baseline backend. `metrics.jsonl` records what that needs.
- A live Discord test on the test application.
