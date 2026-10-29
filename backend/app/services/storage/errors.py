class StorageError(RuntimeError):
    def __init__(self, message: str, *, code: str = 'UNKNOWN', provider_id: str | None = None, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id
        self.cause = cause
