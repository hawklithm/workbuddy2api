"""Compatibility alias for shared account storage."""
import sys
from buddy_agent_auth import account_store as _module
sys.modules[__name__] = _module
