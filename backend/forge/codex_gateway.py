"""Resolve Forge gateway admission independently of upstream credentials."""
from urllib.parse import urlsplit

from .codex_task import CodexTaskError
from .config import settings


def _endpoint(value: str) -> tuple | None:
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or '?' in value or '#' in value):
            return None
        return (parsed.scheme, parsed.hostname.lower(),
                parsed.port or (443 if parsed.scheme == 'https' else 80),
                parsed.path.rstrip('/'))
    except (ValueError, AttributeError):
        return None


def gateway_token_for(base_url: str) -> str | None:
    """Only the explicitly configured gateway may receive its admission token.

    Loopback addresses alone are not proof of gateway identity. Match the full
    endpoint, including port and path, so direct providers retain their auth.
    """
    configured = settings.opencodex_gateway_url
    if not configured:
        return None
    endpoint = _endpoint(configured)
    if endpoint is None:
        raise CodexTaskError('FORGE_OPENCODEX_GATEWAY_URL 无效')
    if _endpoint(base_url) != endpoint:
        return None
    token = settings.opencodex_api_auth_token.strip()
    if not token or len(token) > 4096 or any(ord(ch) < 32 or ord(ch) == 127 for ch in token):
        raise CodexTaskError('OpenCodex 网关缺少有效的 FORGE_OPENCODEX_API_AUTH_TOKEN')
    return token
