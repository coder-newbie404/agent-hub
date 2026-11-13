DISCORD_LIMIT = 2000  # hard cap on one Discord message
FENCE = "```"
CLOSE = "\n" + FENCE
REOPEN = FENCE + "\n"


def _split_oversized(block: str, limit: int) -> list[str]:
    lines, out, cur = block.split("\n"), [], ""
    for ln in lines:
        cand = ln if not cur else cur + "\n" + ln
        if len(cand) <= limit:
            cur = cand
            continue
        if cur:
            out.append(cur)
        while len(ln) > limit:
            out.append(ln[:limit])
            ln = ln[limit:]
        cur = ln
    if cur:
        out.append(cur)
    return out


def _fits(content: str, limit: int) -> bool:
    # content left with an open fence gets a CLOSE suffix appended on flush
    reserve = len(CLOSE) if content.count(FENCE) % 2 else 0
    return len(content) + reserve <= limit


def _restart(piece: str, fence_open: bool, limit: int) -> list[str]:
    if fence_open and _fits(REOPEN + piece, limit):
        return [REOPEN + piece]
    if _fits(piece, limit):
        return [piece]
    floor = limit - len(CLOSE)
    return _split_oversized(piece, floor) if floor >= 1 else [piece]


def chunk_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    if not text.strip():
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for block in text.split("\n\n"):
        pieces = _split_oversized(block, limit) if len(block) > limit else [block]
        queue = list(reversed(pieces))
        while queue:
            piece = queue.pop()
            fence_open = buf.count(FENCE) % 2 == 1
            candidate = piece if not buf else buf + "\n\n" + piece
            if _fits(candidate, limit):
                buf = candidate
                continue
            if buf:
                chunks.append(buf + (CLOSE if fence_open else ""))
                buf = ""
            queue.extend(reversed(_restart(piece, fence_open, limit)))
    if buf:
        chunks.append(buf + (CLOSE if buf.count(FENCE) % 2 == 1 else ""))
    return chunks
