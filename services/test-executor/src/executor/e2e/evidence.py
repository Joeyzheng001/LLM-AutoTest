"""Immutable, complete evidence per run/case; redact secrets instead of truncating."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET = re.compile(r"password|passwd|token|authorization|cookie|secret|api.?key|credential", re.I)


def now() -> str:
    return datetime.now(UTC).isoformat()


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "<redacted>" if SECRET.search(str(k)) else redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(x, secrets) for x in value]
    if isinstance(value, str):
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            value = value.replace(secret, "<redacted>")
    return value


def safe_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.netloc.rsplit("@", 1)[-1]
    query = urlencode(
        [
            (k, "<redacted>" if SECRET.search(k) else v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, host, parsed.path, query, parsed.fragment))


class Evidence:
    def __init__(
        self,
        run_id: str,
        case_id: str,
        writer: Callable[[str, bytes], str],
        secrets: tuple[str, ...] = (),
    ):
        self.prefix = f"evidence/e2e/{run_id}/{case_id}"
        self.writer = writer
        self.secrets = secrets
        self.files: list[str] = []
        self.errors: list[str] = []

    def write(self, name: str, value: Any) -> str | None:
        try:
            content = (
                value
                if isinstance(value, bytes)
                else json.dumps(redact(value, self.secrets), ensure_ascii=False, indent=2).encode()
            )
            ref = self.writer(f"{self.prefix}/{name}", content)
            if ref not in self.files:
                self.files.append(ref)
            return ref
        except Exception as exc:
            self.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return None

    async def screenshot(self, page: Any, name: str) -> str | None:
        try:
            # Mask password fields even when the browser has not visually hidden
            # them; caller-configured sensitive regions are supported too.
            masks = [
                frame.locator('input[type="password"], [data-autotest-sensitive]')
                for frame in page.frames
            ]
            content = await page.screenshot(full_page=True, mask=masks, timeout=10000)
            return self.write(f"{name}.png", content)
        except Exception as exc:
            self.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return None

    async def observe(self, page: Any, name: str) -> dict[str, Any]:
        from executor.e2e.explorer import observe

        try:
            observation = await observe(page)
        except Exception as exc:
            self.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            observation = {"frames": [], "error": str(exc)}
        observation["timestamp"] = now()
        return {"snapshot_ref": self.write(f"{name}.json", observation), "observation": observation}
