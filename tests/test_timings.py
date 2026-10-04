import json

from clangy.timings import load, summarize


def test_summary(tmp_path):
    folder = tmp_path / "conversations" / "1"
    folder.mkdir(parents=True)
    runs = [
        {"ok": True, "total_seconds": 30.0, "predicted_seconds": 20.0, "queue_seconds": 0.0, "steps": 2,
         "prompt_tokens": 1000, "cached_tokens": 900,
         "requests": [
             {"first_token_seconds": 3.0,
              "timings": {"prompt_n": 900, "prompt_per_second": 340.0, "predicted_n": 80, "predicted_per_second": 60.0}},
             {"first_token_seconds": 0.5,
              "timings": {"prompt_n": 20, "prompt_per_second": 40.0, "predicted_n": 5, "predicted_per_second": 20.0}},
         ]},
        {"ok": True, "total_seconds": 50.0, "predicted_seconds": 60.0, "queue_seconds": 10.0, "steps": 4,
         "prompt_tokens": 1000, "cached_tokens": 700, "requests": []},
        {"ok": False, "timed_out": True, "requests": []},
    ]  # fmt: skip
    (folder / "metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))
    summary = summarize(load(tmp_path))
    assert "Runs: 3, answered 2, timed out 1, backend lost 0" in summary
    assert "Time to answer: median 40.0 s, p90 48.0 s, max 50.0 s" in summary, "p90 stays within the data"
    assert "Prompt tokens: 2000, cached 1600 (80%)" in summary
    assert "Prompt speed: median 340.0 tok/s, p90 340.0 tok/s" in summary
    assert "Generation speed: median 60.0 tok/s, p90 60.0 tok/s" in summary
    assert "ETA error (actual - predicted): median 0.0 s" in summary
