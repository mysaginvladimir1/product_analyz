"""Rotating application and HTTP logs. Use a separate LOG_DIR per process."""
import json
import logging
import os
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4
from http import HTTPStatus

STATUS_DESCRIPTIONS = {
    200: "Успешно", 201: "Ресурс создан", 202: "Запрос принят в обработку",
    204: "Успешно, без содержимого", 301: "Постоянное перенаправление",
    302: "Временное перенаправление", 303: "Ответ доступен по другому адресу",
    304: "Ресурс не изменился", 307: "Временное перенаправление с сохранением метода",
    308: "Постоянное перенаправление с сохранением метода",
    400: "Некорректный запрос", 401: "Требуется авторизация",
    403: "Доступ запрещён", 404: "Ресурс не найден", 405: "Метод не разрешён",
    408: "Истекло время ожидания запроса", 409: "Конфликт с текущим состоянием ресурса",
    413: "Слишком большой размер запроса", 415: "Неподдерживаемый тип содержимого",
    422: "Не удалось обработать содержимое запроса", 429: "Превышен лимит запросов",
    500: "Внутренняя ошибка сервера", 501: "Функциональность не реализована",
    502: "Некорректный ответ вышестоящего сервера", 503: "Сервис временно недоступен",
    504: "Истекло время ожидания вышестоящего сервера",
}


def status_description(status):
    if status is None:
        return "HTTP-ответ не начат"
    if status in STATUS_DESCRIPTIONS:
        return STATUS_DESCRIPTIONS[status]
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Нестандартный HTTP-код"

request_id = ContextVar("request_id", default="-")


class RequestContext(logging.Filter):
    def filter(self, record):
        record.request_id = request_id.get()
        return True


class AccessFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            **record.http_event,
        }, ensure_ascii=False)


def configure_logging():
    directory = Path(os.environ.get("LOG_DIR", "logs")).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    max_bytes = int(os.environ.get("LOG_MAX_BYTES", "10485760"))
    backups = int(os.environ.get("LOG_BACKUP_COUNT", "5"))
    if max_bytes <= 0 or backups < 1:
        raise ValueError("LOG_MAX_BYTES must be positive and LOG_BACKUP_COUNT >= 1")

    def file_handler(name, formatter):
        handler = RotatingFileHandler(directory / name, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.addFilter(RequestContext())
        return handler

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s")
    root = logging.getLogger()
    # Replace only handlers owned by this module, preserving hosting/test handlers.
    for handler in list(root.handlers):
        if getattr(handler, "analyst_owned", False):
            root.removeHandler(handler)
            handler.close()
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(RequestContext())
    for handler in (file_handler("analyst.log", fmt), console):
        handler.analyst_owned = True
        root.addHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

    access = logging.getLogger("http_access")
    for handler in list(access.handlers):
        access.removeHandler(handler)
        handler.close()
    access.addHandler(file_handler("access.log", AccessFormatter()))
    access.setLevel(logging.INFO)
    access.propagate = False
    # Our access journal excludes query strings; Uvicorn's default does not.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return directory


class AccessLogMiddleware:
    """Wrap the entire app, including FastAPI's 500 error handler."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = uuid4().hex
        token = request_id.set(rid)
        started = time.perf_counter()
        status = None
        completed = False
        error = None

        async def tracked_send(message):
            nonlocal status, completed
            if message["type"] == "http.response.start":
                status = message["status"]
                message = dict(message)
                message["headers"] = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"] + [(b"x-request-id", rid.encode())]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True

        try:
            await self.app(scope, receive, tracked_send)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            event = {
                "request_id": rid,
                "method": scope.get("method"),
                "path": scope.get("path", "")[:2048],
                "status": status,
                "status_description": status_description(status),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "response_complete": completed,
            }
            if error:
                event["error_type"] = error
            level = logging.ERROR if error or (status and status >= 500) else logging.WARNING if status and status >= 400 else logging.INFO
            try:
                logging.getLogger("http_access").log(level, "HTTP request", extra={"http_event": event})
            finally:
                request_id.reset(token)
