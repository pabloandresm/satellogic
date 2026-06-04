import threading
from src.common.models import DownloadEvent, EventType
from typing import Optional


class EventLog:
    """Thread-safe append-only log of simulation events. Used by the reporter and FastAPI dashboard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[DownloadEvent] = []

    def record(
        self,
        sim_time: float,
        pass_id: str,
        satellite_id: str,
        event_type: EventType,
        file_id: Optional[str] = None,
        chunk_id: Optional[int] = None,
        details: Optional[str] = None,
    ) -> None:
        event = DownloadEvent(
            sim_time=sim_time,
            pass_id=pass_id,
            satellite_id=satellite_id,
            event_type=event_type,
            file_id=file_id,
            chunk_id=chunk_id,
            details=details,
        )
        with self._lock:
            self._events.append(event)

    def all(self) -> list[DownloadEvent]:
        with self._lock:
            return list(self._events)

    def for_pass(self, pass_id: str) -> list[DownloadEvent]:
        with self._lock:
            return [e for e in self._events if e.pass_id == pass_id]
