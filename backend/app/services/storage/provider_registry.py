from __future__ import annotations

from app.services.storage.base import StorageProvider
from app.services.storage.errors import StorageError


class StorageProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, StorageProvider] = {}

    def register(self, provider: StorageProvider) -> None:
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> StorageProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise StorageError(f'Storage provider not found: {provider_id}', code='PROVIDER_NOT_FOUND', provider_id=provider_id)
        return provider

    def has(self, provider_id: str) -> bool:
        return provider_id in self._providers

    def all(self) -> list[StorageProvider]:
        return list(self._providers.values())
