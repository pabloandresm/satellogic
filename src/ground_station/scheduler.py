import json
import logging
import threading
from collections import deque
from typing import Optional

from src.common.models import PassWindow
from settings import PASS_SCHEDULE_FILE

logger = logging.getLogger(__name__)


def load_pass_schedule(path: str = PASS_SCHEDULE_FILE) -> list[PassWindow]:
    with open(path) as f:
        raw: dict = json.load(f)
    windows = [
        PassWindow(
            pass_id=pass_id,
            satellite_id=data["satellite_id"],
            start=data["start"],
            end=data["end"],
            bandwidth_gbps=data["bandwidth_gbps"],
        )
        for pass_id, data in raw.items()
    ]
    return sorted(windows, key=lambda w: (w.start, w.pass_id))


class Scheduler:
    """
    Decides which satellite the GS should connect to at any point in simulated time.

    Algorithm:
      At each decision point (pass start, pass end, file completion), collect all
      currently visible passes and pick the satellite with the highest-priority
      (lowest priority number) pending file. Ties are broken by total pending data volume.
    """

    def __init__(self, passes: list[PassWindow]) -> None:
        self._lock = threading.Lock()
        self._pass_queue: deque[PassWindow] = deque(passes)
        # {satellite_id: {file_id: {priority, pending: [chunk_ids]}}}
        self._satellite_inventory: dict[str, dict[str, dict]] = {}

    def add_pass(self, window: PassWindow) -> None:
        """Thread-safe: allows FastAPI to inject new passes at runtime."""
        with self._lock:
            self._pass_queue.append(window)
            self._pass_queue = deque(sorted(self._pass_queue, key=lambda w: (w.start, w.pass_id)))

    def update_inventory(self, satellite_id: str, inventory: dict[str, dict]) -> None:
        """Called by downlink after receiving HELLO_ACK to refresh satellite's known pending files."""
        with self._lock:
            existing = self._satellite_inventory.get(satellite_id, {})
            for file_id, meta in inventory.items():
                if "total_chunks" not in meta and file_id in existing:
                    meta["total_chunks"] = existing[file_id].get("total_chunks")
            self._satellite_inventory[satellite_id] = inventory

    def mark_chunk_received(self, satellite_id: str, file_id: str, chunk_id: int) -> None:
        with self._lock:
            inv = self._satellite_inventory.get(satellite_id, {})
            file_inv = inv.get(file_id)
            if file_inv and chunk_id in file_inv.get("pending", []):
                file_inv["pending"].remove(chunk_id)
                if not file_inv["pending"]:
                    del inv[file_id]

    def get_all_passes(self) -> list[PassWindow]:
        with self._lock:
            return list(self._pass_queue)

    def get_visible_passes(self, sim_time: float) -> list[PassWindow]:
        with self._lock:
            return [p for p in self._pass_queue if p.start <= sim_time < p.end]

    def pick_best_satellite(self, sim_time: float) -> Optional[PassWindow]:
        """
        Returns the PassWindow for the best satellite to connect to right now,
        or None if nothing is visible or all visible satellites have no pending data.
        """
        visible = self.get_visible_passes(sim_time)
        if not visible:
            return None

        best: Optional[PassWindow] = None
        best_priority: int = 999
        best_pending_volume: float = 0.0

        for window in visible:
            sat_inv = self._satellite_inventory.get(window.satellite_id, {})
            if not sat_inv:
                continue

            # Best (lowest-numbered) priority among this satellite's pending files
            top_priority = min(meta["priority"] for meta in sat_inv.values())
            # Total pending chunks for tie-breaking
            total_pending = sum(len(meta["pending"]) for meta in sat_inv.values())

            if top_priority < best_priority or (
                top_priority == best_priority and total_pending > best_pending_volume
            ):
                best_priority = top_priority
                best_pending_volume = total_pending
                best = window

        return best

    def get_next_event_time(self, after: float) -> Optional[float]:
        """Returns the next simulated time at which a pass starts or ends."""
        with self._lock:
            times = set()
            for p in self._pass_queue:
                if p.start > after:
                    times.add(p.start)
                if p.end > after:
                    times.add(p.end)
        return min(times) if times else None

    def get_pass_by_id(self, pass_id: str) -> Optional[PassWindow]:
        with self._lock:
            for p in self._pass_queue:
                if p.pass_id == pass_id:
                    return p
        return None
