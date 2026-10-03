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


def progress(state: str, steps: int, elapsed: float, eta: float | None, deadline: float | None, now: float) -> str:
    """Progress message; the minute counter changes on every per-minute edit, so users see it's alive."""
    minutes = int(elapsed // 60)
    details = [f"step {steps}"] if steps > 1 else []
    details.append(f"{minutes} min so far" if minutes else "just started")
    timing = []
    if eta is not None:
        timing.append(f"answer expected {timestamp(eta)}" if eta > now else "taking longer than expected")
    if deadline is not None:
        timing.append(f"deadline {timestamp(deadline)}")
    text = f"⏳ Working on it: {state} ({', '.join(details)})."
    if timing:
        sentence = ", ".join(timing)
        text += f" {sentence[0].upper()}{sentence[1:]}."
    return text


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
