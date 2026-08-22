"""Plugin interface + registry.

A plugin parses one file version (a git blob) into Units. Parsing is a pure
function of the blob bytes, so results are cached by blob OID upstream — a plugin
never needs to worry about caching or git.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    """One function/method in one version of a file."""
    name: str          # identity within the file, e.g. "Owner::addOwner" or "helper"
    container: str      # cohesion scope for WMC_other: class name, or "" = file scope
    start_line: int     # 1-based, inclusive
    end_line: int
    cc: int             # cyclomatic complexity
    nloc: int


class LanguagePlugin:
    name: str = "base"
    # extensions this plugin claims, e.g. (".java",). Empty = "default fallback".
    extensions: tuple[str, ...] = ()

    def parse(self, blob: bytes, path: str) -> list[Unit]:
        raise NotImplementedError


# extension -> plugin. A plugin registered under "" is the default fallback.
_REGISTRY: dict[str, LanguagePlugin] = {}


def register(plugin: LanguagePlugin, *, default: bool = False) -> None:
    for ext in plugin.extensions:
        _REGISTRY[ext] = plugin
    if default:
        _REGISTRY[""] = plugin


def get_plugin(ext: str) -> LanguagePlugin | None:
    return _REGISTRY.get(ext) or _REGISTRY.get("")
