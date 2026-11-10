"""
auth.py

Minimal admin authentication for AutoPod Cloud's state-changing API
endpoints (container start/stop/restart/remove, image pull/push/remove/tag,
Docker Hub login/logout, rollback).

Design (documented in ARCHITECTURE.md Section 17 and
CURRENT_STATUS_AND_IMPLEMENTATION_PLAN.md, item 6.3): a single shared secret,
`ADMIN_API_KEY`, provided via the `X-Admin-Key` request header. This is
appropriate for a single-team, non-multi-tenant tool -- NOT a general-purpose
auth system. Read-only monitoring endpoints intentionally remain
unauthenticated.

If `ADMIN_API_KEY` is not set in the environment at all, authentication is
disabled and a loud warning is logged once at startup -- this keeps local
development frictionless while making it unmistakable that a production
deployment without this variable set is unprotected.
"""

import os
import hmac
import logging
from functools import wraps
from flask import request, jsonify

logger = logging.getLogger('autopod')

_ADMIN_KEY = os.environ.get("ADMIN_API_KEY", "").strip()

if not _ADMIN_KEY:
    logger.warning(
        "ADMIN_API_KEY is not set -- all admin/state-changing API endpoints "
        "are UNAUTHENTICATED. Set ADMIN_API_KEY before deploying anywhere "
        "reachable outside your own machine."
    )


def admin_key_configured():
    return bool(_ADMIN_KEY)


def check_admin_key(req=None):
    """Return True if the request carries a valid X-Admin-Key header.

    If no ADMIN_API_KEY is configured at all, this returns True (auth
    disabled -- local dev convenience, loudly logged above).
    Query parameter authentication has been removed to prevent leaking
    secrets through URLs, proxy logs, or browser histories.
    """
    if not _ADMIN_KEY:
        return True
    req = req or request
    headers = getattr(req, "headers", {})
    supplied = headers.get("X-Admin-Key", "")
    return hmac.compare_digest(supplied, _ADMIN_KEY)


def require_admin_key(f):
    """Decorator: reject the request with 401 unless a valid X-Admin-Key
    header is present (or no ADMIN_API_KEY is configured at all)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.method == 'OPTIONS':
            return f(*args, **kwargs)
        if not check_admin_key(request):
            logger.warning(f"Rejected unauthenticated request to {request.path}")
            return jsonify({"success": False, "error": "Unauthorized: missing or invalid X-Admin-Key"}), 401
        return f(*args, **kwargs)
    return wrapper
