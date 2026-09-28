class ServiceError(Exception):
    """Expected, user-facing failure. `code` maps to an i18n key `err.<code>`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NotFound(ServiceError):
    """Also used for records owned by someone else: never reveal their existence."""

    def __init__(self) -> None:
        super().__init__("not_found")


class Conflict(ServiceError):
    """Optimistic concurrency failure or an already-resolved action."""

    def __init__(self, code: str = "conflict") -> None:
        super().__init__(code)
