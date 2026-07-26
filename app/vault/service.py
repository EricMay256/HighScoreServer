"""Application-service transaction boundary for vault use cases."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.vault.db import VaultPoolObserver, acquire_vault_connection


class VaultTransactionService:
    """Own transactions while repositories remain connection-injected."""

    def __init__(
        self,
        engine: AsyncEngine,
        observer: VaultPoolObserver | None = None,
    ) -> None:
        self._engine = engine
        self._observer = observer

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with acquire_vault_connection(
            self._engine,
            self._observer,
        ) as connection:
            async with connection.begin():
                yield connection
