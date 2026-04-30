from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import Request


async def read_form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: str(values[-1]) if values else "" for key, values in parsed.items()}

