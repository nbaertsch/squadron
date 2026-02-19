"""Dashboard Security — Authentication for SSE and REST observability endpoints.

Security Model:
    Squadron is deployed as a single-tenant service (one instance per repo).
    Authentication is optional and controlled by environment variable.

    - SQUADRON_DASHBOARD_API_KEY: When set, all dashboard/SSE/activity endpoints
      require Bearer token authentication. When unset, endpoints are open
      (suitable for internal/trusted network deployments).

Usage:
    from squadron.dashboard_security import require_api_key, get_security_config

    @router.get("/activity/{agent_id}")
    async def get_activity(
        agent_id: str,
        authorized: bool = Depends(require_api_key)
    ):
        ...

Security Considerations:
    - API key should be a cryptographically strong random string (32+ chars)
    - Use HTTPS in production to protect the key in transit
    - For zero-trust environments, consider additional measures (IP allowlist, mTLS)
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

# Environment variable for dashboard API key
DASHBOARD_API_KEY_ENV = "SQUADRON_DASHBOARD_API_KEY"

# Security scheme for OpenAPI docs
_bearer_scheme = HTTPBearer(auto_error=False)


def get_security_config() -> dict:
    """Get current security configuration status."""
    api_key = os.environ.get(DASHBOARD_API_KEY_ENV)
    return {
        "authentication_required": api_key is not None,
        "api_key_env_var": DASHBOARD_API_KEY_ENV,
        "api_key_configured": bool(api_key),
    }


def generate_api_key() -> str:
    """Generate a cryptographically secure API key.

    Use this to generate a key for the SQUADRON_DASHBOARD_API_KEY env var.
    """
    return secrets.token_urlsafe(32)


async def require_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> bool:
    """FastAPI dependency that validates API key if configured.

    Returns True if:
    - No API key is configured (SQUADRON_DASHBOARD_API_KEY not set / None)
    - Valid API key is provided in Authorization header

    Raises HTTPException 401 if:
    - API key is configured (even if empty string) but not provided
    - API key is configured but invalid

    Note: Uses `is None` check (not falsy `not expected_key`) so that an empty-
    string key (e.g. a misconfigured secrets manager or Docker Compose env var
    without a value) still enforces authentication rather than silently bypassing
    it.  This keeps behavior consistent with get_security_config() which also
    uses `api_key is not None`.
    """
    expected_key = os.environ.get(DASHBOARD_API_KEY_ENV)

    # No authentication required — key not configured at all
    if expected_key is None:
        return True

    # Authentication required but no credentials provided
    if credentials is None:
        logger.warning(
            "Dashboard API request without credentials from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide Authorization: Bearer <api_key> header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Validate the provided key (constant-time comparison)
    if not secrets.compare_digest(credentials.credentials, expected_key):
        logger.warning(
            "Invalid dashboard API key from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return True


def validate_sse_token(token: str | None) -> bool:
    """Validate SSE stream token.

    For SSE connections, the token can be passed as a query parameter
    since EventSource API doesn't support custom headers easily.

    Args:
        token: Token from query parameter (?token=...)

    Returns:
        True if valid or no authentication required.

    Raises:
        HTTPException if authentication fails.

    Note: Uses `is None` check (not falsy `not expected_key`) consistent with
    require_api_key() — see its docstring for the rationale.
    """
    expected_key = os.environ.get(DASHBOARD_API_KEY_ENV)

    # No authentication required — key not configured at all
    if expected_key is None:
        return True

    # No token provided
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide ?token=<api_key> query parameter.",
        )

    # Validate token
    if not secrets.compare_digest(token, expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )

    return True
