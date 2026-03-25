from __future__ import annotations

import logging
from time import perf_counter
from uuid import uuid4

from fastapi import Request, Response

from app.core.request_context import reset_correlation_id, set_correlation_id


logger = logging.getLogger(__name__)


async def correlation_id_middleware(request: Request, call_next) -> Response:
    correlation_id = request.headers.get("X-Correlation-ID") or uuid4().hex
    token = set_correlation_id(correlation_id)
    started_at = perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request_failed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                "client_ip": request.client.host if request.client else None,
            },
        )
        reset_correlation_id(token)
        raise

    response.headers["X-Correlation-ID"] = correlation_id
    logger.info(
        "request_completed",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round((perf_counter() - started_at) * 1000, 2),
            "client_ip": request.client.host if request.client else None,
        },
    )
    reset_correlation_id(token)
    return response
