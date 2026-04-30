"""Django views for the OTel-middleware demo.

Identical to the non-OTel django-app's views so responses and business-
level log events match exactly, making side-by-side comparison in Loki
meaningful.

The OTel middleware handles refresh_context / lifecycle logging; these
handlers only add business-level log events and exercise
`set_context_field` and `logger.exception`.
"""

from __future__ import annotations

import time

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse

logger = settings.LOGGER


def index(request: HttpRequest) -> JsonResponse:
    logger.info("index hit")
    return JsonResponse({"app": "django-otel-demo", "message": "hello"})


def get_user(request: HttpRequest, user_id: int) -> JsonResponse:
    logger.set_context_field("user_id", user_id)
    logger.info("fetching user {id}", id=user_id)
    if user_id < 0:
        logger.warning("negative user id {id}", id=user_id)
        return JsonResponse({"error": "negative id"}, status=400)
    return JsonResponse({"user_id": user_id, "name": f"user-{user_id}"})


def search(request: HttpRequest) -> JsonResponse:
    q = request.GET.get("q", "")
    logger.info("search performed with {query}", query=q)
    return JsonResponse({"query": q})


def slow(request: HttpRequest) -> JsonResponse:
    logger.warning("slow endpoint entered")
    time.sleep(0.3)
    logger.info("slow endpoint complete")
    return JsonResponse({"status": "slow-ok"})


def boom(request: HttpRequest) -> HttpResponse:
    try:
        result = 1 / 0
    except ZeroDivisionError:
        logger.exception("boom endpoint exploded")
        return JsonResponse({"error": "zero division"}, status=500)
    return JsonResponse({"result": result})


def health(request: HttpRequest) -> JsonResponse:
    # Path matches the middleware's default excluded prefixes, so no
    # request_started / request_completed events are emitted.
    return JsonResponse({"status": "ok"})
