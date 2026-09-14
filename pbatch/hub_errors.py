"""Public Agent Hub errors, containing no access tokens or private payloads."""


class HubAccessError(Exception):
    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code = code
        self.status = status
