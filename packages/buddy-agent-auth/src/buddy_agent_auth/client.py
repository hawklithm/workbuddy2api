"""Compatibility alias for the CodeBuddy provider."""
import sys
from .providers.codebuddy import client as _module
sys.modules[__name__] = _module
