from __future__ import annotations

import logging
import os
import time
from typing import Any, Mapping, Sequence

import httpx

from .choice import ChoicePolicy

logger = logging.getLogger(__name__)


class TypeSafeTransport:
    """Sends choice questions to TypeSafe's SystemOne endpoint (JEV)."""

    name = "JEV"
    supports_images = False

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = "https://api.typesafe.ai/v1/systemone",
        timeout_s: float = 25,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise ValueError("Set TYPESAFE_API_KEY or pass api_key=...")
        self.model = model or os.environ.get("TYPESAFE_MODEL", "jev-latest")
        self.base_url = base_url
        self.client = client or httpx.Client(http2=True, timeout=timeout_s)

    def ask(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
        *,
        images: Sequence[bytes] = (),
    ) -> Mapping[str, Any]:
        if images:
            raise ValueError("JEV does not accept images; no action executed")
        return self._post({"model": self.model, "state": state, "questions": questions})

    def _post(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        for attempt in range(3):
            try:
                response = self.client.post(
                    self.base_url,
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            except httpx.HTTPError as exc:
                logger.warning("JEV request failed attempt=%d: %s", attempt, exc)
                raise RuntimeError("JEV connection failed; no action executed") from exc
            if response.status_code in {429, 503, 529} and attempt < 2:
                logger.debug("JEV rate-limited status=%d attempt=%d", response.status_code, attempt)
                time.sleep(0.5 * (2**attempt))
                continue
            if response.is_error:
                logger.warning("JEV error status=%d", response.status_code)
                if _provider_error_type(response) == "max_tokens_exceeded":
                    raise RuntimeError(
                        "JEV provider returned HTTP "
                        f"{response.status_code} (max_tokens_exceeded); request exceeds the provider's "
                        "token limit. Reduce the observed context or subtask size; no action executed"
                    )
                raise RuntimeError(f"JEV provider returned HTTP {response.status_code}; no action executed")
            return response.json()
        raise RuntimeError("JEV provider unavailable")


class TypeSafeJevPolicy(ChoicePolicy):
    """ChoicePolicy backed by TypeSafe JEV. Equivalent to `ChoicePolicy(TypeSafeTransport(...))`."""

    transport: TypeSafeTransport

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = "https://api.typesafe.ai/v1/systemone",
        timeout_s: float = 25,
        max_candidates: int = 240,
        client: httpx.Client | None = None,
        invalid_retries: int = 1,
    ) -> None:
        super().__init__(
            TypeSafeTransport(api_key=api_key, model=model, base_url=base_url, timeout_s=timeout_s, client=client),
            max_candidates=max_candidates,
            invalid_retries=invalid_retries,
        )


def _provider_error_type(response: httpx.Response) -> str | None:
    """Read only the structured error code, never echo arbitrary response text."""
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail.get("error_type") if isinstance(detail, dict) else None
