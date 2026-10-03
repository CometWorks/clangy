"""Summary of the per-run timing records in the conversations' metrics.jsonl files."""

import json
from pathlib import Path
from statistics import median, quantiles

from clangy.proxy import speed_samples


def load(data_dir: Path) -> list[dict]:
    records = []
    for path in sorted(data_dir.glob("conversations/*/metrics.jsonl")):
        records += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return records


def _spread(values: list[float], unit: str) -> str:
    if not values:
        return "-"
    p90 = quantiles(values, n=10)[-1] if len(values) > 1 else values[0]
    return f"median {median(values):.1f}{unit}, p90 {p90:.1f}{unit}, max {max(values):.1f}{unit}"


def summarize(records: list[dict]) -> str:
    ok = [r for r in records if r.get("ok")]
    requests = [q for r in ok for q in r.get("requests", [])]
    speeds = [speed_samples(q.get("timings", {})) for q in requests]
    prompt = sum(r.get("prompt_tokens", 0) for r in ok)
    cached = sum(r.get("cached_tokens", 0) for r in ok)
    errors = [r["total_seconds"] - r["predicted_seconds"] for r in ok if "predicted_seconds" in r]
    lines = [
        f"Runs: {len(records)}, answered {len(ok)}, timed out {sum(r.get('timed_out', False) for r in records)}, "
        f"backend lost {sum(r.get('backend_failed', False) for r in records)}",
        f"Time to answer: {_spread([r['total_seconds'] for r in ok if 'total_seconds' in r], ' s')}",
        f"Queue wait: {_spread([r['queue_seconds'] for r in ok if 'queue_seconds' in r], ' s')}",
        f"Model turns per answer: {_spread([r['steps'] for r in ok if 'steps' in r], '')}",
        f"Time to first token: {_spread([q['first_token_seconds'] for q in requests if 'first_token_seconds' in q], ' s')}",
        f"Prompt speed: {_spread([p for p, _ in speeds if p], ' tok/s')}",
        f"Generation speed: {_spread([g for _, g in speeds if g], ' tok/s')}",
        f"Prompt tokens: {prompt}, cached {cached} ({cached / prompt:.0%})" if prompt else "Prompt tokens: -",
        f"ETA error (actual - predicted): {_spread(errors, ' s')}",
    ]
    return "\n".join(lines)
