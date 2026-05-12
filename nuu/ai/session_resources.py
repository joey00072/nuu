"""
Global registry for session-scoped cleanup callbacks. Used to manage lifecycle
of resources (HTTP clients, temp files) that must be torn down when a session
ends.

Owns: _cleanups set, register_session_resource_cleanup().
Delegates to: caller-provided cleanup functions.

Depends on: standard library only (collections.abc)
"""

from collections.abc import Callable


_cleanups: set[Callable[[], None]] = set()


def register_session_resource_cleanup(
    cleanup: Callable[[], None],
) -> Callable[[], None]:
    _cleanups.add(cleanup)
    return lambda: _cleanups.discard(cleanup)


def cleanup_session_resources() -> None:
    errors: list[str] = []
    for fn in list(_cleanups):
        try:
            fn()
        except Exception as e:
            errors.append(f"{fn}: {e}")
    if errors:
        raise Exception("Failed to cleanup session resources:\n" + "\n".join(errors))
