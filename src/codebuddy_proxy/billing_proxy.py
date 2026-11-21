"""Compatibility alias for DailyCheckin's authenticated billing transport."""
import sys
from daily_checkin import workbuddy_billing as _module
sys.modules[__name__] = _module
