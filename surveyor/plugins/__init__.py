"""Language plugins: turn a file blob into a list of Units (name, container, line
range, CC). The default plugin wraps lizard and covers most languages; write a
plugin only where lizard is weak."""
from .base import Unit, LanguagePlugin, get_plugin, register
from . import lizard_plugin  # noqa: F401  (registers the default plugin)

__all__ = ["Unit", "LanguagePlugin", "get_plugin", "register"]
