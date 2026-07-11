#!/usr/bin/env python3
"""Small fail-closed client for the TencentDB Agent Memory HTTP Gateway."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


class BackendError(RuntimeError):
    """A configuration, transport, or protocol error from the memory backend."""


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        try:
            return all(ipaddress.ip_address(item[4][0]).is_loopback for item in socket.getaddrinfo(hostname, None))
        except (OSError, ValueError):
            return False


@dataclass(frozen=True)
class TencentDBBackend:
    base_url: str
    api_key: str | None = None
    timeout: float = 2.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise BackendError("TencentDB Gateway URL must be an absolute http(s) URL")
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
            raise BackendError("Plain HTTP is allowed only for a loopback TencentDB Gateway")
        if not _is_loopback(parsed.hostname) and not self.api_key:
            raise BackendError("A bearer API key is required for a non-loopback TencentDB Gateway")
        if self.timeout <= 0 or self.timeout > 30:
            raise BackendError("TencentDB Gateway timeout must be between 0 and 30 seconds")

    @classmethod
    def from_env(cls) -> "TencentDBBackend | None":
        if os.environ.get("AGENT_MEMORY_BACKEND", "").strip().lower() != "tencentdb":
            return None
        base_url = os.environ.get("AGENT_MEMORY_TENCENT_URL", "").strip()
        if not base_url:
            raise BackendError("AGENT_MEMORY_TENCENT_URL is required when AGENT_MEMORY_BACKEND=tencentdb")
        try:
            timeout = float(os.environ.get("AGENT_MEMORY_BACKEND_TIMEOUT", "2"))
        except ValueError as error:
            raise BackendError("AGENT_MEMORY_BACKEND_TIMEOUT must be numeric") from error
        return cls(
            base_url=base_url.rstrip("/") + "/",
            api_key=os.environ.get("AGENT_MEMORY_TENCENT_API_KEY") or None,
            timeout=timeout,
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(urljoin(self.base_url, path.lstrip("/")), data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            detail = error.read(500).decode("utf-8", errors="replace")
            raise BackendError(f"TencentDB Gateway HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise BackendError(f"TencentDB Gateway unavailable: {error}") from error
        try:
            result = json.loads(raw) if raw else {}
        except json.JSONDecodeError as error:
            raise BackendError("TencentDB Gateway returned invalid JSON") from error
        if not isinstance(result, dict):
            raise BackendError("TencentDB Gateway returned a non-object response")
        return result

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def recall(self, query: str, session_key: str, user_id: str) -> str:
        response = self._request("POST", "/recall", {"query": query, "session_key": session_key, "user_id": user_id})
        return str(response.get("context") or "")

    def capture(self, user_content: str, assistant_content: str, session_key: str, session_id: str, user_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/capture",
            {
                "user_content": user_content,
                "assistant_content": assistant_content,
                "session_key": session_key,
                "session_id": session_id,
                "user_id": user_id,
            },
        )

    def search_memories(self, query: str, limit: int = 5) -> dict[str, Any]:
        return self._request("POST", "/search/memories", {"query": query, "limit": limit})

    def search_conversations(self, query: str, session_key: str, limit: int = 5) -> dict[str, Any]:
        return self._request("POST", "/search/conversations", {"query": query, "session_key": session_key, "limit": limit})

    def end_session(self, session_key: str, user_id: str) -> dict[str, Any]:
        return self._request("POST", "/session/end", {"session_key": session_key, "user_id": user_id})
