"""exit-gate: can this AI coding session be closed without losing anything? Plus `scratch`, the one-off script bin."""
from .core import (SESSION, SYSTEM, Config, ConfigError, Ctx, Result, cached_line, mark_start, register_check,
                   run_gate, session_start, write_cache, write_note)
from .scratch import Bin

__version__ = "0.1.0"
__all__ = ["SESSION", "SYSTEM", "Config", "ConfigError", "Ctx", "Result", "Bin", "cached_line", "mark_start",
           "register_check", "run_gate", "session_start", "write_cache", "write_note"]
