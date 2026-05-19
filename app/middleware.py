import ipaddress
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, HTMLResponse


def _parse_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            pass
    return networks


def _ip_in_networks(ip_str: str, networks: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in networks)
    except ValueError:
        return False


class ProxyResolutionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, trusted_proxies: str = ""):
        super().__init__(app)
        self._trusted = _parse_networks(trusted_proxies)

    async def dispatch(self, request: Request, call_next) -> Response:
        peer_ip = request.client.host if request.client else None
        if peer_ip and self._trusted and _ip_in_networks(peer_ip, self._trusted):
            real_ip = None
            xreal = request.headers.get("x-real-ip", "").strip()
            if xreal:
                real_ip = xreal
            else:
                xff = request.headers.get("x-forwarded-for", "")
                if xff:
                    # Leftmost entry is the original client
                    real_ip = xff.split(",")[0].strip()
            if real_ip:
                try:
                    ipaddress.ip_address(real_ip)
                    request.scope["client"] = (real_ip, 0)
                except ValueError:
                    pass
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'"
        )
        return response


_FORBIDDEN_HTML = (
    "<html><body><h1>403 Forbidden</h1>"
    "<p>Your IP address is not allowed to access this resource.</p></body></html>"
)

_ADMIN_PREFIXES = ("/admin", "/auth/login")


class AdminIPMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowlist: str = ""):
        super().__init__(app)
        self._networks = _parse_networks(allowlist)

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if any(path == p or path.startswith(p + "/") or path.startswith(p + "?")
               for p in _ADMIN_PREFIXES):
            ip = request.client.host if request.client else ""
            if not _ip_in_networks(ip, self._networks):
                return HTMLResponse(_FORBIDDEN_HTML, status_code=403)
        return await call_next(request)
