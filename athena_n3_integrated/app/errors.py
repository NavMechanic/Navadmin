"""Domain refusal raised by N0 services; the API turns it into an HTTP error plus an audit event."""
from __future__ import annotations

from typing import Any


class N0Error(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}
