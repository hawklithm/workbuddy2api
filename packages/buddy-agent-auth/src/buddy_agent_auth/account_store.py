"""Compatibility alias for the CodeBuddy provider."""
import sys
from .providers.codebuddy import account_store as _module
sys.modules[__name__] = _module
