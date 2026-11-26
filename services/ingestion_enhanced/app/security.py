"""Access control for the ingestion gateway.

Reads are open within the cluster. Uploading a datasheet needs a service key,
because an ingested parameter becomes an input to a thermal model and a
compliance decision. The caller identity is also what gets recorded against a
manually supplied value, so an unauthenticated upload would produce a parameter
nobody can attribute.

Behind the gateway the caller arrives as `x-athena-user`; that header is
trustworthy only because this service is not reachable except through the
gateway on the compose network.
"""
from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from .config import get_settings


async def require_service_key(
    request: Request,
    x_athena_api_key: Annotated[str | None, Header()] = None,
) -> str:
    s = get_settings()
    forwarded = request.headers.get("x-athena-user")
    if not s.api_key:
        if s.environment == "prod":
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="ATHENA_API_KEY is not configured and the environment is prod.")
        return forwarded or "anonymous (no service key configured)"
    if not x_athena_api_key or not hmac.compare_digest(x_athena_api_key, s.api_key):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Athena-Api-Key. An ingested parameter "
                   "becomes an input to a thermal model and a compliance decision.")
    return forwarded or request.headers.get("x-athena-caller") or "service"


ServiceCaller = Annotated[str, Depends(require_service_key)]
