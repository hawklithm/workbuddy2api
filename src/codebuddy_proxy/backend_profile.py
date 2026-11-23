"""Compatibility alias for the shared authentication backend profiles."""
import sys
from buddy_agent_auth import backend_profile as _module
sys.modules[__name__] = _module
