"""Discord message texts: answer splitting, footer, progress and error indications."""

LIMIT = 2000


def _is_fence(line: str) -> bool:
    return line.lstrip().startswith("```")


def _units(text: str) -> list[str]:
    """Splits text into paragraphs, keeping each code block in one piece."""
    units: list[str] = []
    current: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if _is_fence(line):
            in_code = not in_code
        if not line.strip() and not in_code:
            if current:
                units.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        units.append("\n".join(current))
    return units


def _split_unit(unit: str, limit: int) -> list[str]:
    """Splits an oversized paragraph at line boundaries, closing and reopening code fences."""
    lines = unit.split("\n")
    fence = lines[0].strip() if _is_fence(lines[0]) else None
    reserve = len("\n```") if fence else 0
    width = limit - reserve - len(fence) - 1 if fence else limit
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(line[i : i + width] for i in range(0, max(len(line), 1), width))
    pieces: list[str] = []
    current: list[str] = []
    for line in wrapped:
        if current and len("\n".join(current + [line])) + reserve > limit:
            pieces.append("\n".join(current) + ("\n```" if fence else ""))
            current = [fence] if fence else []
        current.append(line)
    pieces.append("\n".join(current))
    return pieces


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """Splits text into Discord sized parts at paragraph or line boundaries, never inside a code block."""
    parts: list[str] = []
    current = ""
    for unit in _units(text.strip()):
        for piece in _split_unit(unit, limit) if len(unit) > limit else [unit]:
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
                continue
            parts.append(current)
            current = piece
    if current:
        parts.append(current)
    return parts


def answer_parts(answer: str, footer: str) -> list[str]:
    parts = split_message(answer) or [""]
    if len(parts[-1]) + 2 + len(footer) <= LIMIT:
        parts[-1] = f"{parts[-1]}\n\n{footer}".strip()
    else:
        parts.append(footer)
    return parts


def footer(bot_name: str) -> str:
    return f"-# Mention @{bot_name} when you want a reply, I won't respond otherwise."


def timestamp(unix: float) -> str:
    return f"<t:{int(unix)}:R>"


def progress(eta: float, deadline: float | None, ahead: int, now: float) -> str:
    """Progress message: just the expected time, with details only when something is off."""
    if eta <= now:
        late = f"⏳ Taking longer than expected"
        return f"{late}, deadline {timestamp(deadline)}." if deadline is not None else f"{late}."
    if ahead:
        others = "answer" if ahead == 1 else "answers"
        return f"⏳ Waiting for {ahead} other {others} first. Answer expected {timestamp(eta)}."
    return f"⏳ Answer expected {timestamp(eta)}."


def timed_out(bot_name: str) -> str:
    return f"❌ Sorry, I ran out of time on this one. Mention @{bot_name} to try again."


def failed(bot_name: str) -> str:
    return f"❌ Sorry, something went wrong. Mention @{bot_name} to try again."


def backend_lost(retry: int, max_retries: int) -> str:
    return (
        "⚠️ Lost the connection to my AI backend. "
        f"I'll try again automatically once it's back (retry {retry}/{max_retries})."
    )


def backend_gave_up(bot_name: str) -> str:
    return f"❌ My AI backend is still unreachable. Mention @{bot_name} to resume the conversation."


def refused() -> str:
    return "Sorry, I can't share that."
