from __future__ import annotations

from ipaddress import ip_address

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.settings import settings


class LanAccessMiddleware(BaseHTTPMiddleware):
    """Allow all traffic in internet mode, but restrict LAN mode to local/private IPs."""

    async def dispatch(self, request: Request, call_next):
        if is_request_allowed(request):
            return await call_next(request)
        return PlainTextResponse("Forbidden: this service is limited to the local network.", status_code=403)


def is_request_allowed(request: Request) -> bool:
    if settings.access_mode in {"internet", "public"}:
        return True

    host = request.client.host if request.client else ""
    if not host:
        return True

    try:
        client_ip = ip_address(host)
    except ValueError:
        return False

    if client_ip.version == 6 and client_ip.ipv4_mapped is not None:
        client_ip = client_ip.ipv4_mapped

    if client_ip.is_loopback or client_ip.is_private or client_ip.is_link_local:
        return True

    for network in settings.parsed_allowed_networks():
        if client_ip in network:
            return True

    return False
