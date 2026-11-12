"""Provider configuration and transport, independent of analytics prompts."""
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from logging_setup import status_description

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    protocol: str
    url: str
    model: str
    key_env: str
    timeout: float
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "


def load_config():
    """Explicit config failures never silently fall back to another provider."""
    config_path = os.environ.get("AI_CONFIG")
    if config_path:
        document = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
        if not isinstance(document, dict):
            raise ValueError("AI_CONFIG must contain an object")
        name = os.environ.get("AI_PROVIDER") or document.get("active_provider")
        profiles = document.get("providers", {})
        if not isinstance(name, str) or not isinstance(profiles, dict) or name not in profiles:
            raise ValueError("Selected AI provider is missing from AI_CONFIG")
        item = profiles[name]
    else:
        name = os.environ.get("AI_PROVIDER", "gorouter")
        if name != "gorouter":
            raise ValueError("Set AI_CONFIG to configure a provider other than gorouter")
        item = {
            "protocol": "openai_chat", "url": os.environ.get("GOROUTER_API_URL", "https://gorouter.ru/api/v1/chat/completions"),
            "model": os.environ.get("GOROUTER_MODEL", "gorouter/free"),
            "api_key_env": "GOROUTER_API_KEY",
        }
    required = ("protocol", "url", "model", "api_key_env")
    if not isinstance(item, dict):
        raise ValueError("AI profile must contain an object")
    if any(not isinstance(item.get(k), str) or not item[k].strip() for k in required):
        raise ValueError("AI profile requires protocol, url, model and api_key_env")
    if item["protocol"] not in ADAPTERS:
        raise ValueError("Unsupported AI protocol: " + item["protocol"])
    url = urlparse(item["url"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("AI url must be an HTTP(S) endpoint without credentials, query or fragment")
    if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Remote AI endpoint requires HTTPS")
    timeout = float(item.get("timeout_seconds", 120))
    if not 0 < timeout <= 600:
        raise ValueError("timeout_seconds must be between 0 and 600")
    header = item.get("auth_header", "Authorization")
    prefix = item.get("auth_prefix", "Bearer ")
    if not isinstance(header, str) or not header or not isinstance(prefix, str) or any(c in header + prefix for c in "\r\n"):
        raise ValueError("Invalid AI authentication configuration")
    return ProviderConfig(name, item["protocol"], item["url"], item["model"], item["api_key_env"], timeout, header, prefix)


class OpenAIChatAdapter:
    @staticmethod
    def payload(model, messages, temperature, max_tokens):
        return {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "stream": False}

    @staticmethod
    def content(data):
        content = data["choices"][0]["message"].get("content")
        if isinstance(content, list):
            content = " ".join(part if isinstance(part, str) else part.get("text", "") for part in content)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Empty assistant content")
        # Reasoning is not the final user-facing answer.
        return content.strip()


ADAPTERS = {"openai_chat": OpenAIChatAdapter}


class AIClient:
    def __init__(self, api_key=None, model=None):
        self.config = load_config()
        self.api_key = api_key or os.environ.get(self.config.key_env)
        if not self.api_key:
            raise ValueError("Missing AI key environment variable: " + self.config.key_env)
        self.model = model or self.config.model
        self.adapter = ADAPTERS[self.config.protocol]
        logger.info("AI provider=%s model=%s protocol=%s", self.config.name, self.model, self.config.protocol)

    def complete(self, messages, temperature=0.0, max_tokens=4000):
        try:
            response = requests.post(
                self.config.url,
                headers={"Content-Type": "application/json", self.config.auth_header: self.config.auth_prefix + self.api_key},
                json=self.adapter.payload(self.model, messages, temperature, max_tokens),
                timeout=self.config.timeout,
            )
            response.raise_for_status()
            return self.adapter.content(response.json())
        except requests.exceptions.Timeout:
            reason = "таймаут AI-провайдера"
        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            reason = f"HTTP {code}: {status_description(code)}"
        except requests.exceptions.RequestException:
            reason = "ошибка подключения к AI-провайдеру"
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            reason = "некорректный ответ AI-провайдера"
        # Do not expose upstream bodies, URLs or credentials in API responses/logs.
        logger.warning("AI provider=%s failed: %s", self.config.name, reason)
        return "ОШИБКА: " + reason
