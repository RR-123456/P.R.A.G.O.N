# Re-export — the real implementation is features/feature/weather_report.py.
# Named weather_action there (tool id is "weather_report").
from features.feature.weather_report import weather_action
__all__ = ["weather_action"]
