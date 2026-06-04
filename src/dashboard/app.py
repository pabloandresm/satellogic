from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.common.models import EventType, PassWindow
from src.ground_station.downlink import SimClock
from src.ground_station.event_log import EventLog
from src.ground_station.scheduler import Scheduler


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class PassIn(BaseModel):
    satellite_id: str
    start: int
    end: int
    bandwidth_gbps: float


class PassOut(BaseModel):
    pass_id: str
    satellite_id: str
    start: int
    end: int
    bandwidth_gbps: float


class EventOut(BaseModel):
    sim_time: float
    pass_id: str
    satellite_id: str
    event_type: str
    file_id: Optional[str] = None
    chunk_id: Optional[int] = None
    details: Optional[str] = None


class PassTimelineEntry(BaseModel):
    satellite_id: str
    start: int
    end: int
    bandwidth_gbps: float
    chunks_received: int
    files_active: list[str]
    events: list[EventOut]


class SimStatusOut(BaseModel):
    sim_time: float
    is_running: bool
    current_satellite: Optional[str]
    total_chunks_received: int
    total_chunks_dropped: int


# ---------------------------------------------------------------------------
# App factory — receives live references to GS internals
# ---------------------------------------------------------------------------

def create_app(
    scheduler: Scheduler,
    event_log: EventLog,
    sim_clock: SimClock,
    sim_state: dict[str, Any],
) -> FastAPI:
    """
    sim_state is a mutable dict shared with the GS main loop:
        {"is_running": bool, "current_satellite": str | None}
    """
    app = FastAPI(
        title="Satellite Downlink Dashboard",
        description="Live view of the ground station downlink scheduler",
    )

    # -------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # -------------------------------------------------------------------
    # Simulation status
    # -------------------------------------------------------------------

    @app.get("/status", response_model=SimStatusOut, tags=["simulation"])
    def get_status() -> SimStatusOut:
        events = event_log.all()
        total_received = sum(1 for e in events if e.event_type == EventType.CHUNK_RECEIVED)
        total_dropped = sum(1 for e in events if e.event_type == EventType.CHUNK_DROPPED)

        # Derive current satellite from the last PASS_STARTED / PASS_ENDED pair
        current_satellite: Optional[str] = None
        if sim_state.get("is_running"):
            active: dict[str, bool] = {}
            for e in events:
                if e.event_type == EventType.PASS_STARTED:
                    active[e.satellite_id] = True
                elif e.event_type == EventType.PASS_ENDED:
                    active[e.satellite_id] = False
            current_satellite = next(
                (sat for sat, is_active in reversed(list(active.items())) if is_active), None
            )

        return SimStatusOut(
            sim_time=round(sim_clock.now(), 2),
            is_running=sim_state.get("is_running", True),
            current_satellite=current_satellite,
            total_chunks_received=total_received,
            total_chunks_dropped=total_dropped,
        )

    # -------------------------------------------------------------------
    # Pass schedule
    # -------------------------------------------------------------------

    @app.get("/passes", response_model=list[PassOut], tags=["schedule"])
    def list_passes() -> list[PassOut]:
        return [
            PassOut(
                pass_id=p.pass_id,
                satellite_id=p.satellite_id,
                start=p.start,
                end=p.end,
                bandwidth_gbps=p.bandwidth_gbps,
            )
            for p in scheduler.get_all_passes()
        ]

    @app.post("/passes", response_model=dict[str, list[str]], tags=["schedule"])
    def add_passes(new_passes: dict[str, PassIn]) -> dict[str, list[str]]:
        """Inject new pass windows into the running scheduler."""
        added: list[str] = []
        for pass_id, data in new_passes.items():
            # Reject duplicates
            if scheduler.get_pass_by_id(pass_id) is not None:
                raise HTTPException(status_code=409, detail=f"Pass '{pass_id}' already exists")
            window = PassWindow(
                pass_id=pass_id,
                satellite_id=data.satellite_id,
                start=data.start,
                end=data.end,
                bandwidth_gbps=data.bandwidth_gbps,
            )
            scheduler.add_pass(window)
            added.append(pass_id)
        return {"added": added}

    # -------------------------------------------------------------------
    # Events
    # -------------------------------------------------------------------

    @app.get("/events", response_model=list[EventOut], tags=["events"])
    def list_events(
        satellite_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> list[EventOut]:
        events = event_log.all()
        if satellite_id:
            events = [e for e in events if e.satellite_id == satellite_id]
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return [
            EventOut(
                sim_time=round(e.sim_time, 2),
                pass_id=e.pass_id,
                satellite_id=e.satellite_id,
                event_type=e.event_type.value,
                file_id=e.file_id,
                chunk_id=e.chunk_id,
                details=e.details,
            )
            for e in events
        ]

    # -------------------------------------------------------------------
    # Timeline — main visualization endpoint
    # -------------------------------------------------------------------

    @app.get("/schedule/timeline", response_model=dict[str, PassTimelineEntry], tags=["schedule"])
    def get_timeline() -> dict[str, PassTimelineEntry]:
        """
        Returns each pass enriched with its download events — designed for
        frontend timeline / Gantt-chart visualization.
        """
        result: dict[str, PassTimelineEntry] = {}
        for p in scheduler.get_all_passes():
            pass_events = event_log.for_pass(p.pass_id)
            chunks_received = sum(
                1 for e in pass_events if e.event_type == EventType.CHUNK_RECEIVED
            )
            files_active = sorted({e.file_id for e in pass_events if e.file_id})
            result[p.pass_id] = PassTimelineEntry(
                satellite_id=p.satellite_id,
                start=p.start,
                end=p.end,
                bandwidth_gbps=p.bandwidth_gbps,
                chunks_received=chunks_received,
                files_active=files_active,
                events=[
                    EventOut(
                        sim_time=round(e.sim_time, 2),
                        pass_id=e.pass_id,
                        satellite_id=e.satellite_id,
                        event_type=e.event_type.value,
                        file_id=e.file_id,
                        chunk_id=e.chunk_id,
                        details=e.details,
                    )
                    for e in pass_events
                    if e.event_type != EventType.CHUNK_RECEIVED  # exclude per-chunk noise
                ],
            )
        return result

    return app
