"""Environment-backed settings, validated once at startup."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Mapping

MIB = 1024 * 1024
MIN_TOKEN_LENGTH = 16
_LANG_RE = re.compile(r"^[a-z]{3}(_[A-Za-z]+)?(\+[a-z]{3}(_[A-Za-z]+)?)*$")


class ConfigError(ValueError):
    """Raised at startup when configuration is missing or invalid."""


def parse_tokens(raw: str) -> tuple[str, ...]:
    """Accept a JSON list or a comma-separated string of tokens."""
    raw = raw.strip()
    if raw.startswith("["):
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError("API_TOKENS is not valid JSON") from exc
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ConfigError("API_TOKENS JSON must be a list of strings")
    else:
        items = raw.split(",")
    return tuple(t.strip() for t in items if t.strip())


@dataclass(frozen=True)
class Settings:
    api_tokens: tuple[str, ...]
    conversion_timeout_seconds: int = 300
    max_pages: int = 200
    ocr_language: str = "eng"
    max_upload_bytes: int = 10 * MIB
    queue_size: int = 5
    retry_after_seconds: int = 30
    upload_timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if not self.api_tokens:
            raise ConfigError("API_TOKENS must contain at least one token")
        if any(len(t) < MIN_TOKEN_LENGTH for t in self.api_tokens):
            raise ConfigError(f"each API token must be at least {MIN_TOKEN_LENGTH} characters")
        for name in ("conversion_timeout_seconds", "max_pages", "max_upload_bytes", "retry_after_seconds", "upload_timeout_seconds"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name} must be positive")
        if self.queue_size < 0:
            raise ConfigError("queue_size must not be negative")
        if not _LANG_RE.match(self.ocr_language):
            raise ConfigError("OCR_LANGUAGE is not a valid Tesseract language spec")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env

        def integer(name: str, default: int) -> int:
            value = env.get(name)
            if value is None or value.strip() == "":
                return default
            try:
                return int(value)
            except ValueError as exc:
                raise ConfigError(f"{name} must be an integer") from exc

        return cls(
            api_tokens=parse_tokens(env.get("API_TOKENS", "")),
            conversion_timeout_seconds=integer("CONVERSION_TIMEOUT_SECONDS", 300),
            max_pages=integer("MAX_PAGES", 200),
            ocr_language=env.get("OCR_LANGUAGE", "eng").strip() or "eng",
            queue_size=integer("QUEUE_SIZE", 5),
            retry_after_seconds=integer("RETRY_AFTER_SECONDS", 30),
            upload_timeout_seconds=integer("UPLOAD_TIMEOUT_SECONDS", 120),
        )
