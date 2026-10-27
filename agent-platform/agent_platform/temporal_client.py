"""One Temporal client per process, with the platform's converter and codec."""

from __future__ import annotations

import asyncio
from typing import Optional

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client

from . import settings as platform_settings
from .codec import data_converter

_client: Optional[Client] = None
_lock = asyncio.Lock()


async def connect(address: Optional[str] = None, namespace: Optional[str] = None) -> Client:
    settings = platform_settings.get()
    return await Client.connect(
        address or settings.temporal_address,
        namespace=namespace or settings.temporal_namespace,
        data_converter=data_converter(),
        plugins=[PydanticAIPlugin()],
    )


async def get() -> Client:
    """Connected on first use, so the API starts before Temporal is ready."""
    global _client
    if _client is not None:
        return _client
    async with _lock:
        if _client is None:
            _client = await connect()
    return _client


def use(client: Client) -> None:
    """Tests: hand the API an already-connected client."""
    global _client
    _client = client
