"""Safe authentication recovery hints shared by remote terminal views."""

from .hub_client import HubError


def auth_hint(error: Exception) -> str:
    if isinstance(error, HubError) and error.status == 401:
        return "Sign in with pi-batch fleet auth login in another terminal, then refresh."
    if isinstance(error, HubError) and error.status == 403:
        return "Access denied; check the current account's Hub permissions."
    return ""


def error_status(context: str, error: Exception) -> str:
    hint = auth_hint(error)
    return f"{context}. {hint}" if hint else f"{context} ({type(error).__name__})"
