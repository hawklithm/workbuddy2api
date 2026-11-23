"""Small contract for brand-specific login-state providers."""
from typing import Protocol, TypeVar

SessionT = TypeVar('SessionT', covariant=True)


class SessionProvider(Protocol[SessionT]):
    """Load a brand-specific session without imposing a shared token format."""
    def load(self) -> SessionT: ...
