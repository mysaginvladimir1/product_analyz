"""Stateless API for the trusted orchestrator."""
import asyncio
import os
import secrets
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, StrictInt
from dotenv import load_dotenv
load_dotenv()

security = HTTPBearer(auto_error=False)


def require_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    expected = os.environ.get("ANALYST_API_TOKEN")
    if not expected:
        raise HTTPException(503, "Service authentication is not configured")
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(401, "Invalid service token", headers={"WWW-Authenticate": "Bearer"})


class Message(BaseModel):
    messageUUID: UUID
    author: Literal["user", "llm"]
    message: str = Field(min_length=1, max_length=20000)


class ChatRequest(BaseModel):
    userGitLabID: StrictInt = Field(gt=0)
    messages: list[Message] = Field(min_length=1, max_length=100)


class ChatResponse(BaseModel):
    userGitLabID: int
    replyToMessageUUID: UUID
    message: Message


def install_chat_api(app, state, model, process_query, shift_duration):
    @app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(require_token)])
    async def chat(request: ChatRequest):
        if request.messages[-1].author != "user":
            raise HTTPException(422, "Last message must have author=user")
        if any(not item.message.strip() for item in request.messages):
            raise HTTPException(422, "Messages must not be blank")
        if len({item.messageUUID for item in request.messages}) != len(request.messages):
            raise HTTPException(422, "Duplicate messageUUID in history")
        if sum(len(item.message) for item in request.messages) > 100000:
            raise HTTPException(413, "History is too large")
        if state.get('source', 'excel') == 'excel' and state["df"].empty:
            raise HTTPException(503, "Данные не загружены")
        history = [
            {"role": "user" if item.author == "user" else "assistant", "content": item.message}
            for item in request.messages[:-1]
        ]
        last = request.messages[-1]
        answer, _ = await asyncio.to_thread(
            process_query, state["df"], last.message, model, shift_duration, history
        )
        if answer.lower().startswith(("ошибка", "неизвестная метрика")):
            raise HTTPException(502, "Не удалось обработать запрос аналитики")
        return ChatResponse(
            userGitLabID=request.userGitLabID,
            replyToMessageUUID=last.messageUUID,
            message=Message(messageUUID=uuid4(), author="llm", message=answer),
        )
