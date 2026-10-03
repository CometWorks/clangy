from clangy import text


def test_short_message_is_one_part():
    assert text.split_message("Hello.\n\nWorld.") == ["Hello.\n\nWorld."]


def test_splits_at_paragraphs():
    paragraphs = [f"{i} " + "x" * 600 for i in range(5)]
    parts = text.split_message("\n\n".join(paragraphs))
    assert all(len(p) <= text.LIMIT for p in parts)
    assert "\n\n".join(parts) == "\n\n".join(paragraphs)
    assert len(parts) == 2


def test_never_splits_inside_code_block():
    code = "```\n" + "\n\n".join(f"line {i}" for i in range(100)) + "\n```"
    message = "a" * 1500 + "\n\n" + code
    parts = text.split_message(message)
    assert parts[0] == "a" * 1500
    assert parts[1] == code


def test_oversized_code_block_is_closed_and_reopened():
    code = "```python\n" + "\n".join(f"print({i})" for i in range(400)) + "\n```"
    parts = text.split_message(code)
    assert len(parts) > 1
    for part in parts:
        assert len(part) <= text.LIMIT
        assert part.startswith("```python\n")
        assert part.endswith("```")


def test_overlong_line_is_hard_wrapped():
    parts = text.split_message("y" * 4500)
    assert [len(p) for p in parts] == [2000, 2000, 500]


def test_footer_goes_to_the_last_part():
    footer = text.footer("Clangy")
    assert text.answer_parts("Short answer.", footer) == [f"Short answer.\n\n{footer}"]
    parts = text.answer_parts("z" * 1990, footer)
    assert parts == ["z" * 1990, footer]


def test_progress_only_details_problems():
    assert text.progress(eta=1000.5, deadline=2000, ahead=0, now=900) == "⏳ Answer expected <t:1000:R>."
    assert text.progress(eta=1000, deadline=None, ahead=2, now=900) == (
        "⏳ Waiting for 2 other answers first. Answer expected <t:1000:R>."
    )
    assert text.progress(eta=1000, deadline=None, ahead=1, now=900).startswith("⏳ Waiting for 1 other answer first.")
    assert text.progress(eta=1000, deadline=2000, ahead=0, now=1100) == (
        "⏳ Taking longer than expected, deadline <t:2000:R>."
    )
