"""Compatibility alias for DailyCheckin's WorkBuddy activity service."""
import sys
from daily_checkin import workbuddy as _module
sys.modules[__name__] = _module
