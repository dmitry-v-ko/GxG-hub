"""Client of the Telegram service's admin API (M13): the operator connects the pilot's Telegram account from the ui.

Private network only. Every call carries a service JWT (`iss` = `approval-ui`, `aud` = the Telegram service, ≤ 60 s,
signed with `GXG_UI_SVC_KEY`) and the signed-in operator in `X-GxG-Operator`. Request bodies hold the phone, the
login code and the 2FA password: they are sent once and never logged, stored or put into an error text.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_kit import svc_jwt

ISSUER = "approval-ui"
DEFAULT_AUDIENCE = "telegram"
CONNECTION = "tg-pilot"
TIMEOUT_S = 15
MESSAGE_MAX = 200
# What a person reads when the API gave no text of its own.
BY_STATUS = {
    400: "Telegram-сервис отклонил запрос",
    401: "Telegram-сервис не принял подпись ui (ключ approval-ui)",
    403: "Telegram-сервис не разрешает этой операции",
    404: "Telegram-сервис не знает такого подключения",
    409: "Операция сейчас невозможна: шаг входа не совпадает с состоянием сервиса",
    429: "Telegram просит подождать: слишком много попыток",
}


class Client(Protocol):
    """An httpx-style client with `base_url` (TestClient in tests)."""

    def request(self, method: str, url: str, *, headers: dict[str, str], json: Any, timeout: float) -> Any: ...


class Unavailable(Exception):
    """The Telegram service did not answer."""


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


class TelegramAdmin:
    def __init__(
        self,
        client: Client,
        key: Ed25519PrivateKey,
        audience: str = DEFAULT_AUDIENCE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client, self.key, self.audience, self.clock = client, key, audience, clock

    def call(self, method: str, rest: str, operator: str, body: dict[str, Any] | None = None) -> Any:
        """The JSON answer; ApiError for a refusal, Unavailable when there is no answer at all."""
        token = svc_jwt.sign(self.key, iss=ISSUER, aud=self.audience, now=self.clock)
        headers = {"Authorization": f"Bearer {token}", "X-GxG-Operator": operator}
        try:
            r = self.client.request(
                method, f"/internal/v1/admin/{CONNECTION}/{rest}", headers=headers, json=body, timeout=TIMEOUT_S
            )
        except Exception:  # connection refused, timeout: nothing here may carry the request body
            raise Unavailable from None
        status = int(r.status_code)
        if status >= 400:
            raise ApiError(status, _message(r))
        try:
            return r.json()
        except ValueError:
            raise ApiError(502, "Telegram-сервис ответил не JSON'ом") from None


def _message(r: Any) -> str:
    status = int(r.status_code)
    try:
        doc = r.json()
        text = doc.get("message") or doc.get("detail") if isinstance(doc, dict) else None
    except ValueError:
        text = None
    if isinstance(text, str) and text.strip():
        return text.strip()[:MESSAGE_MAX]
    return BY_STATUS.get(status) or f"Telegram-сервис ответил ошибкой {status}"
