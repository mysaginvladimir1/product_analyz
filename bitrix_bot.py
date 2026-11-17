"""Scheduled Bitrix24 bot integration based on the imbot.v2 REST API."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

import requests
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from chat_api import require_token


logger = logging.getLogger(__name__)
UTC = timezone.utc


class IntegrationError(RuntimeError):
    """A safe-to-display integration error without credentials."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("run_at must contain a UTC offset, for example +03:00")
    return value.astimezone(UTC)


def normalize_dialog_id(value: str) -> str:
    value = value.strip()
    if value.startswith("chat"):
        number = value[4:]
        if number.isdigit() and int(number) > 0:
            return f"chat{int(number)}"
    elif value.isdigit() and int(value) > 0:
        return str(int(value))
    raise ValueError("dialog_id must look like chat123 (group chat) or 123 (direct chat)")


@dataclass(frozen=True)
class BotSettings:
    webhook_url: str
    bot_token: str
    bot_id: int | None
    bot_code: str
    bot_name: str
    analyst_url: str
    analyst_token: str
    database_path: Path
    request_timeout: float
    poll_interval: float
    running_timeout: float
    max_attempts: int
    retry_delay: float
    message_prefix: str

    @classmethod
    def from_env(cls, base_dir: Path) -> "BotSettings":
        bot_id_text = os.environ.get("BITRIX24_BOT_ID", "").strip()
        try:
            bot_id = int(bot_id_text) if bot_id_text else None
        except ValueError as exc:
            raise IntegrationError("BITRIX24_BOT_ID must be an integer") from exc
        database_path = Path(
            os.environ.get("BITRIX24_DB_PATH", str(base_dir / "bitrix_bot.sqlite3"))
        )
        if not database_path.is_absolute():
            database_path = base_dir / database_path
        request_timeout = float(os.environ.get("BITRIX24_REQUEST_TIMEOUT", "60"))
        return cls(
            webhook_url=os.environ.get("BITRIX24_WEBHOOK_URL", "").strip().rstrip("/"),
            bot_token=os.environ.get("BITRIX24_BOT_TOKEN", "").strip(),
            bot_id=bot_id,
            bot_code=os.environ.get("BITRIX24_BOT_CODE", "production_analyst").strip(),
            bot_name=os.environ.get("BITRIX24_BOT_NAME", "Производственный аналитик").strip(),
            analyst_url=os.environ.get(
                "ANALYST_API_URL", "http://127.0.0.1:8000/api/analyze2"
            ).strip(),
            analyst_token=os.environ.get("ANALYST_API_TOKEN", "").strip(),
            database_path=database_path,
            request_timeout=request_timeout,
            poll_interval=float(os.environ.get("BITRIX24_POLL_INTERVAL", "5")),
            running_timeout=max(
                request_timeout * 2,
                float(os.environ.get("BITRIX24_RUNNING_TIMEOUT", "300")),
            ),
            max_attempts=max(1, int(os.environ.get("BITRIX24_MAX_ATTEMPTS", "3"))),
            retry_delay=max(1.0, float(os.environ.get("BITRIX24_RETRY_DELAY", "60"))),
            message_prefix=os.environ.get(
                "BITRIX24_MESSAGE_PREFIX", "📊 Отчёт производственного аналитика\n\n"
            ),
        )

    def validate_bitrix(self) -> None:
        parsed = urlparse(self.webhook_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise IntegrationError(
                "BITRIX24_WEBHOOK_URL must be a full HTTPS incoming-webhook URL"
            )
        if not self.bot_token:
            raise IntegrationError("BITRIX24_BOT_TOKEN is not configured")
        if not self.bot_code:
            raise IntegrationError("BITRIX24_BOT_CODE is empty")

    def validate_analyst(self) -> None:
        parsed = urlparse(self.analyst_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise IntegrationError("ANALYST_API_URL must be a full HTTP(S) URL")


class Bitrix24Client:
    def __init__(self, settings: BotSettings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()

    def call(self, method: str, payload: dict) -> object:
        self.settings.validate_bitrix()
        url = f"{self.settings.webhook_url}/{method}.json"
        try:
            response = self.session.post(
                url,
                json=payload,
                headers={"Accept": "application/json"},
                timeout=self.settings.request_timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise IntegrationError(f"Bitrix24 request failed{suffix}") from None
        try:
            data = response.json()
        except ValueError:
            raise IntegrationError("Bitrix24 returned invalid JSON") from None
        if not isinstance(data, dict):
            raise IntegrationError("Bitrix24 returned an invalid response")
        if data.get("error"):
            code = str(data.get("error"))
            description = str(data.get("error_description") or "Bitrix24 API error")
            raise IntegrationError(f"Bitrix24 {code}: {description}")
        if "result" not in data:
            raise IntegrationError("Bitrix24 response has no result field")
        return data["result"]

    def register_bot(self) -> dict:
        result = self.call(
            "imbot.v2.Bot.register",
            {
                "fields": {
                    "code": self.settings.bot_code,
                    "botToken": self.settings.bot_token,
                    "properties": {
                        "name": self.settings.bot_name,
                        "workPosition": "Автоматические производственные отчёты",
                    },
                    "type": "bot",
                    "eventMode": "fetch",
                    "isHidden": False,
                    "isReactionsEnabled": False,
                }
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("bot"), dict):
            raise IntegrationError("Bitrix24 did not return the registered bot")
        return result

    def get_bot(self, bot_id: int) -> dict:
        result = self.call(
            "imbot.v2.Bot.get",
            {
                "botId": bot_id,
                "botToken": self.settings.bot_token,
            },
        )
        if not isinstance(result, dict):
            raise IntegrationError("Bitrix24 returned invalid bot data")
        return result

    def connect_to_chat(self, bot_id: int, chat_id: int, hide_history: bool) -> None:
        self.call(
            "im.chat.user.add",
            {
                "CHAT_ID": chat_id,
                "USERS": [bot_id],
                "HIDE_HISTORY": "Y" if hide_history else "N",
            },
        )

    def send_message(self, bot_id: int, dialog_id: str, message: str) -> int | None:
        result = self.call(
            "imbot.v2.Chat.Message.send",
            {
                "botId": bot_id,
                "botToken": self.settings.bot_token,
                "dialogId": normalize_dialog_id(dialog_id),
                "fields": {"message": message, "urlPreview": False},
            },
        )
        if not isinstance(result, dict):
            raise IntegrationError("Bitrix24 returned invalid message data")
        message_id = result.get("id")
        return int(message_id) if message_id is not None else None


class AnalystClient:
    def __init__(self, settings: BotSettings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()

    def analyze(self, query: str) -> str:
        self.settings.validate_analyst()
        headers = {"Accept": "application/json"}
        if self.settings.analyst_token:
            headers["Authorization"] = f"Bearer {self.settings.analyst_token}"
        try:
            response = self.session.post(
                self.settings.analyst_url,
                json={"query": query},
                headers=headers,
                timeout=self.settings.request_timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise IntegrationError(f"Analyst request failed{suffix}") from None
        try:
            data = response.json()
        except ValueError:
            raise IntegrationError("Analyst returned invalid JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("answer"), str):
            raise IntegrationError("Analyst response must contain a string answer")
        answer = data["answer"].strip()
        if not answer:
            raise IntegrationError("Analyst returned an empty answer")
        return answer


class ScheduleStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    trigger_type TEXT NOT NULL,
                    dialog_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    run_at TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_run_at TEXT,
                    completed_at TEXT,
                    last_error TEXT,
                    message_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS schedules_due_idx
                    ON schedules(status, due_at);
                """
            )

    def set_setting(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO bot_settings(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )

    def get_setting(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM bot_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def recover_interrupted(self, stale_before: datetime) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE schedules
                   SET status = 'pending', due_at = ?,
                       last_error = 'Previous delivery was interrupted; delivery may be retried'
                   WHERE status = 'running' AND last_run_at <= ?""",
                (utc_now().isoformat(), as_utc(stale_before).isoformat()),
            )

    def create(self, dialog_id: str, query: str, run_at: datetime) -> dict:
        schedule_id = str(uuid4())
        created_at = utc_now().isoformat()
        run_at_text = as_utc(run_at).isoformat()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO schedules(
                       id, trigger_type, dialog_id, query, run_at, due_at,
                       status, attempts, created_at
                   ) VALUES (?, 'datetime', ?, ?, ?, ?, 'pending', 0, ?)""",
                (schedule_id, dialog_id, query, run_at_text, run_at_text, created_at),
            )
        return self.get(schedule_id)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row else None

    def get(self, schedule_id: str) -> dict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(schedule_id)
        return result

    def list(self, status: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM schedules"
        params: list[object] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def cancel(self, schedule_id: str) -> dict:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE schedules SET status = 'cancelled'
                   WHERE id = ? AND status IN ('pending', 'failed')""",
                (schedule_id,),
            )
        if cursor.rowcount == 0:
            current = self.get(schedule_id)
            raise ValueError(f"Schedule in status {current['status']} cannot be cancelled")
        return self.get(schedule_id)

    def claim_due(self, now: datetime) -> dict | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM schedules
                   WHERE status = 'pending' AND due_at <= ?
                   ORDER BY due_at ASC LIMIT 1""",
                (as_utc(now).isoformat(),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """UPDATE schedules
                   SET status = 'running', attempts = attempts + 1, last_run_at = ?
                   WHERE id = ?""",
                (as_utc(now).isoformat(), row["id"]),
            )
            connection.commit()
            return self.get(str(row["id"]))
        finally:
            connection.close()

    def complete(self, schedule_id: str, message_id: int | None) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE schedules
                   SET status = 'completed', completed_at = ?, last_error = NULL,
                       message_id = ?
                   WHERE id = ?""",
                (utc_now().isoformat(), message_id, schedule_id),
            )

    def fail(
        self,
        schedule_id: str,
        error: str,
        max_attempts: int,
        retry_delay: float,
    ) -> None:
        current = self.get(schedule_id)
        retry = int(current["attempts"]) < max_attempts
        status = "pending" if retry else "failed"
        due_at = (utc_now() + timedelta(seconds=retry_delay)).isoformat()
        with self._connection() as connection:
            connection.execute(
                """UPDATE schedules SET status = ?, due_at = ?, last_error = ?
                   WHERE id = ?""",
                (status, due_at, error[:2000], schedule_id),
            )


class ScheduleCreate(BaseModel):
    trigger_type: Literal["datetime"] = "datetime"
    dialog_id: str = Field(min_length=1, max_length=50)
    query: str = Field(min_length=1, max_length=5000)
    run_at: datetime


class ConnectChatRequest(BaseModel):
    chat_id: int = Field(gt=0)
    hide_history: bool = True


class DispatchRequest(BaseModel):
    dialog_id: str = Field(min_length=1, max_length=50)
    query: str = Field(min_length=1, max_length=5000)


class BitrixBotService:
    def __init__(
        self,
        settings: BotSettings,
        store: ScheduleStore | None = None,
        bitrix: Bitrix24Client | None = None,
        analyst: AnalystClient | None = None,
    ):
        self.settings = settings
        self.store = store or ScheduleStore(settings.database_path)
        self.bitrix = bitrix or Bitrix24Client(settings)
        self.analyst = analyst or AnalystClient(settings)
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    def bot_id(self) -> int:
        saved = self.store.get_setting("bot_id")
        value = int(saved) if saved else self.settings.bot_id
        if value is None:
            raise IntegrationError("Bot is not registered; call POST /api/bitrix/setup")
        return value

    def setup(self) -> dict:
        result = self.bitrix.register_bot()
        bot = result["bot"]
        bot_id = int(bot["id"])
        self.store.set_setting("bot_id", str(bot_id))
        logger.info("Bitrix24 bot registered: id=%s code=%s", bot_id, bot.get("code"))
        return {"bot": bot, "users": result.get("users", [])}

    def status(self) -> dict:
        try:
            bot_id = self.bot_id()
        except IntegrationError:
            bot_id = None
        return {
            "configured": bool(self.settings.webhook_url and self.settings.bot_token),
            "registered": bot_id is not None,
            "bot_id": bot_id,
            "bot_code": self.settings.bot_code,
            "scheduler_running": self._task is not None and not self._task.done(),
            "database_path": str(self.settings.database_path),
        }

    def connect_chat(self, chat_id: int, hide_history: bool) -> dict:
        bot_id = self.bot_id()
        self.bitrix.connect_to_chat(bot_id, chat_id, hide_history)
        return {"connected": True, "bot_id": bot_id, "dialog_id": f"chat{chat_id}"}

    def _format_answer(self, answer: str) -> str:
        prefix = self.settings.message_prefix
        maximum_answer_length = max(0, 20_000 - len(prefix))
        if len(answer) > maximum_answer_length:
            suffix = "\n\n[Ответ сокращён до лимита Битрикс24]"
            answer = answer[: max(0, maximum_answer_length - len(suffix))] + suffix
        return prefix + answer

    def _deliver(self, dialog_id: str, query: str) -> dict:
        answer = self.analyst.analyze(query)
        message = self._format_answer(answer)
        message_id = self.bitrix.send_message(self.bot_id(), dialog_id, message)
        return {"message_id": message_id, "answer": answer}

    async def dispatch(self, dialog_id: str, query: str) -> dict:
        return await asyncio.to_thread(self._deliver, dialog_id, query)

    async def run_due_once(self) -> int:
        processed = 0
        stale_before = utc_now() - timedelta(seconds=self.settings.running_timeout)
        await asyncio.to_thread(self.store.recover_interrupted, stale_before)
        while True:
            job = await asyncio.to_thread(self.store.claim_due, utc_now())
            if job is None:
                return processed
            try:
                result = await self.dispatch(job["dialog_id"], job["query"])
                await asyncio.to_thread(
                    self.store.complete, job["id"], result["message_id"]
                )
            except Exception as exc:
                safe_error = str(exc) or type(exc).__name__
                logger.exception("Scheduled Bitrix24 delivery failed: id=%s", job["id"])
                await asyncio.to_thread(
                    self.store.fail,
                    job["id"],
                    safe_error,
                    self.settings.max_attempts,
                    self.settings.retry_delay,
                )
            processed += 1

    async def _loop(self) -> None:
        while True:
            await self.run_due_once()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.poll_interval)
                self._wake.clear()
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="bitrix24-scheduler")
        logger.info("Bitrix24 scheduler started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Bitrix24 scheduler stopped")

    def wake(self) -> None:
        self._wake.set()


def install_bitrix_bot_api(
    app: FastAPI,
    base_dir: Path,
    service: BitrixBotService | None = None,
) -> BitrixBotService:
    service = service or BitrixBotService(BotSettings.from_env(base_dir))
    app.state.bitrix_bot = service
    protected = [Depends(require_token)]

    @app.get("/api/bitrix/status", dependencies=protected)
    async def bitrix_status():
        return service.status()

    @app.post("/api/bitrix/setup", dependencies=protected)
    async def bitrix_setup():
        try:
            return await asyncio.to_thread(service.setup)
        except IntegrationError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/bitrix/chats/connect", dependencies=protected)
    async def connect_chat(request: ConnectChatRequest):
        try:
            return await asyncio.to_thread(
                service.connect_chat, request.chat_id, request.hide_history
            )
        except IntegrationError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/bitrix/dispatch", dependencies=protected)
    async def dispatch_now(request: DispatchRequest):
        try:
            dialog_id = normalize_dialog_id(request.dialog_id)
            query = request.query.strip()
            if not query:
                raise ValueError("query must not be blank")
            return await service.dispatch(dialog_id, query)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except IntegrationError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/bitrix/schedules", status_code=201, dependencies=protected)
    async def create_schedule(request: ScheduleCreate):
        try:
            dialog_id = normalize_dialog_id(request.dialog_id)
            run_at = as_utc(request.run_at)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not request.query.strip():
            raise HTTPException(422, "query must not be blank")
        if run_at <= utc_now():
            raise HTTPException(422, "run_at must be in the future")
        try:
            service.bot_id()
        except IntegrationError as exc:
            raise HTTPException(409, str(exc)) from exc
        result = await asyncio.to_thread(
            service.store.create, dialog_id, request.query.strip(), run_at
        )
        service.wake()
        return result

    @app.get("/api/bitrix/schedules", dependencies=protected)
    async def list_schedules(
        status: Literal["pending", "running", "completed", "failed", "cancelled"] | None = None,
        limit: int = Query(100, ge=1, le=500),
    ):
        return {"items": await asyncio.to_thread(service.store.list, status, limit)}

    @app.get("/api/bitrix/schedules/{schedule_id}", dependencies=protected)
    async def get_schedule(schedule_id: str):
        try:
            return await asyncio.to_thread(service.store.get, schedule_id)
        except KeyError as exc:
            raise HTTPException(404, "Schedule not found") from exc

    @app.delete("/api/bitrix/schedules/{schedule_id}", dependencies=protected)
    async def cancel_schedule(schedule_id: str):
        try:
            return await asyncio.to_thread(service.store.cancel, schedule_id)
        except KeyError as exc:
            raise HTTPException(404, "Schedule not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    app.router.add_event_handler("startup", service.start)
    app.router.add_event_handler("shutdown", service.stop)
    return service
