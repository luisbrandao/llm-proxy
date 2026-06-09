"""Simple bearer-token gate for permission-restricted backends.

A request is "authorized" when it carries `Authorization: Bearer <key>` with a
key listed in AUTH_KEYS. When no keys are configured the gate is inert and every
request is treated as authorized, so the feature is strictly opt-in.
"""
from fastapi import Request

from app import config as conf


def extract_bearer(request: Request):
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return None


def is_authorized(request: Request) -> bool:
    if not conf.AUTH_KEYS:
        return True  # no keys configured -> gate disabled
    return extract_bearer(request) in conf.AUTH_KEYS


def restricted(provider_name: str) -> bool:
    p = conf.PROVIDERS_BY_NAME.get(provider_name)
    return bool(p and p.require_permission)
