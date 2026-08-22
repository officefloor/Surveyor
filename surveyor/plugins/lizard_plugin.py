"""Default multi-language plugin backed by lizard.

lizard gives per-function cyclomatic complexity, NLOC and line ranges across many
languages, and class-qualifies method names as ``Class::method`` where it can. We
use that qualifier as the cohesion container; free functions fall back to file
scope (container == "").
"""
from __future__ import annotations

import lizard

from .base import LanguagePlugin, Unit, register


def _container(name: str) -> tuple[str, str]:
    """Split a lizard name into (container, short_name).

    "Owner::addOwner" -> ("Owner", "addOwner"); "helper" -> ("", "helper").
    Nested "A::B::m" -> container "A::B". The full name stays the identity, so the
    return short_name is informational only.
    """
    if "::" in name:
        head, _, tail = name.rpartition("::")
        return head, tail
    return "", name


class LizardPlugin(LanguagePlugin):
    name = "lizard"
    # Claim nothing explicitly; registered as the default fallback so it handles
    # every extension config.LANG_BY_EXT allows. A language-specific plugin can
    # override a given extension by registering itself for it.
    extensions = ()

    def parse(self, blob: bytes, path: str) -> list[Unit]:
        src = blob.decode("utf-8", errors="replace")
        try:
            info = lizard.analyze_file.analyze_source_code(path, src)
        except Exception:
            return []
        units: list[Unit] = []
        for f in info.function_list:
            container, _short = _container(f.name)
            units.append(Unit(
                name=f.name,
                container=container,
                start_line=f.start_line,
                end_line=f.end_line,
                cc=f.cyclomatic_complexity,
                nloc=f.nloc,
            ))
        return units


register(LizardPlugin(), default=True)
