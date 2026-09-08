"""Build-time safeguards for portable Windows executables."""

from __future__ import annotations

from pathlib import PurePath
from typing import Iterable, TypeVar


BinaryEntry = TypeVar("BinaryEntry", bound=tuple)

_WINDOWS_API_FORWARDER_PREFIXES = ("api-ms-win-", "ext-ms-win-")
_WINDOWS_SYSTEM_RUNTIME_NAMES = {"ucrtbase.dll"}
_PRIVATE_RUNTIME_MARKERS = ("/.cache/codex-runtimes/",)


def is_nonportable_binary(destination: str, source: str) -> bool:
    """Return whether a discovered DLL belongs to Windows or a private shell."""

    name = PurePath(destination.replace("\\", "/")).name.casefold()
    normalized_source = source.replace("\\", "/").casefold()
    return (
        name.startswith(_WINDOWS_API_FORWARDER_PREFIXES)
        or name in _WINDOWS_SYSTEM_RUNTIME_NAMES
        or any(marker in normalized_source for marker in _PRIVATE_RUNTIME_MARKERS)
    )


def filter_portable_binaries(entries: Iterable[BinaryEntry]) -> list[BinaryEntry]:
    """Remove host-specific DLLs accidentally discovered through the build PATH."""

    return [
        entry
        for entry in entries
        if len(entry) < 2
        or not is_nonportable_binary(str(entry[0]), str(entry[1]))
    ]
