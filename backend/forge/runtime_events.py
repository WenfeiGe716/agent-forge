"""Forge projection seams for Codex runtime events.

Runtime processes are short-lived and may emit events from an async loop. The
recorder opens one small transaction per event so a dropped connection cannot
silently lose the prefix of a stream; the normal Event/SSE endpoints can then
replay the same records by their monotonically increasing database id.
"""

from __future__ import annotations

from .db import persist_runtime_event, session


class RuntimeEventRecorder:
    """Persist one run's normalized runtime events through a tiny callable API."""

    def __init__(self, run_id: str, *, source: str = 'codex'):
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError('runtime event recorder requires a run id')
        self.run_id = run_id
        self.source = source

    def __call__(self, runtime_event):
        with session() as db:
            return persist_runtime_event(db, self.run_id, runtime_event, source=self.source)
