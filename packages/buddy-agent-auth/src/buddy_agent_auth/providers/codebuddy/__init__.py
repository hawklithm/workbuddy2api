"""CodeBuddy and WorkBuddy account session source."""
from .account_store import AccountStore
from .client import CodeBuddyClient, CodeBuddyError


class CodeBuddyProvider:
    def __init__(self, store: AccountStore, account_id: str):
        self.store = store
        self.account_id = account_id

    def load(self) -> CodeBuddyClient:
        client = self.store.get_client(self.account_id)
        client.ensure_authenticated(open_browser=False, allow_login=False)
        return client


__all__ = ['AccountStore', 'CodeBuddyClient', 'CodeBuddyError', 'CodeBuddyProvider']
