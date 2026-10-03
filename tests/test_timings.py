import json

from clangy.timings import load, summarize


def test_summary(tmp_path):
    folder = tmp_path / "conversations" / "1"
    folder.mkdir(parents=True)
    runs = [
        {"ok": True, "total_seconds": 30.0, "predicted_seconds": 20.0, "queue_seconds": 0.0, "steps": 2,
         "prompt_tokens": 1000, "cached_tokens": 900,
         "requests": [{"first_token_seconds": 3.0, "prompt_tps": 340.0, "gen_tps": 60.0}]},
        {"ok": True, "total_seconds": 50.0, "predicted_seconds": 60.0, "queue_seconds": 10.0, "steps": 4,
         "prompt_tokens": 1000, "cached_tokens": 700, "requests": []},
        {"ok": False, "timed_out": True, "requests": []},
    ]  # fmt: skip
    (folder / "metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))
    summary = summarize(load(tmp_path))
    assert "Runs: 3, answered 2, timed out 1, backend lost 0" in summary
    assert "Time to answer: median 40.0 s" in summary
    assert "Prompt tokens: 2000, cached 1600 (80%)" in summary
    assert "Prompt speed: median 340.0 tok/s, p90 340.0 tok/s" in summary
    assert "ETA error (actual - predicted): median 0.0 s" in summary
