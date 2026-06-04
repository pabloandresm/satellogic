import logging
import math
import socket
import time
import zlib
from typing import Optional

from src.common.models import EventType, PassWindow
from src.common.protocol import MessageType, compress_pending, recv_message, send_message
from src.ground_station.event_log import EventLog
from src.ground_station.scheduler import Scheduler
from settings import (
    CHUNK_SIZE_GB,
    MIN_CHUNK_COMPLETION_TO_HOLD,
    TIME_SCALE,
)

logger = logging.getLogger(__name__)


def _effective_bandwidth(window: PassWindow, sim_time: float) -> float:
    """Dynamic bandwidth: sine-wave degradation, peaking at zenith (midpoint of pass)."""
    duration = window.end - window.start
    if duration <= 0:
        return window.bandwidth_gbps
    elapsed = sim_time - window.start
    factor = 0.2 + 0.8 * math.sin(math.pi * elapsed / duration)
    return window.bandwidth_gbps * max(factor, 0.2)


def _chunk_transfer_time(bandwidth_gbps: float) -> float:
    """Simulated seconds to transfer one 100 MB chunk at the given bandwidth (Gbps)."""
    return (CHUNK_SIZE_GB * 8) / bandwidth_gbps  # GB → Gbit, then divide by Gbps


class DownlinkManager:
    """
    Manages the active TCP connection to a satellite during a pass window.
    Responsible for requesting chunks, handling drops/retries, and respecting
    the MIN_CHUNK_COMPLETION_TO_HOLD threshold before switching satellites.
    """

    def __init__(
        self,
        scheduler: Scheduler,
        satellite_ports: dict[str, int],
        event_log: EventLog,
        satellite_hosts: dict[str, str] | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._satellite_ports = satellite_ports
        self._satellite_hosts = satellite_hosts or {}
        self._event_log = event_log

        # GS-side inventory of confirmed chunks: {satellite_id: {file_id: set(chunk_ids)}}
        self._confirmed: dict[str, dict[str, set[int]]] = {}

        # Chunks that failed (DROP or CRC error) in the primary phase, queued for
        # deferred retry at the end of the current pass or at the start of the next.
        # {satellite_id: {file_id: [chunk_id, ...]}}  — ordered by chunk_id
        self._pending_retries: dict[str, dict[str, list[int]]] = {}

        self._current_pass: Optional[PassWindow] = None
        self._conn: Optional[socket.socket] = None

    # ------------------------------------------------------------------
    # Public interface used by the GS main loop
    # ------------------------------------------------------------------

    def _pick_feasible_satellite(self, sim_time: float) -> Optional[PassWindow]:
        """
        Returns the highest-priority satellite that is both visible and able to
        complete at least one chunk before its pass ends.  Satellites with an
        empty inventory or insufficient remaining pass time are skipped so the
        caller never wastes time on a satellite that cannot do useful work right now.
        """
        best: Optional[PassWindow] = None
        best_priority: int = 999
        best_pending_volume: float = 0.0

        for window in self._scheduler.get_visible_passes(sim_time):
            sat_inv = self._scheduler._satellite_inventory.get(window.satellite_id, {})
            if not sat_inv:
                continue

            bw = _effective_bandwidth(window, sim_time)
            if _chunk_transfer_time(bw) > window.end - sim_time:
                continue  # not enough time for even one chunk in this pass

            top_priority = min(meta["priority"] for meta in sat_inv.values())
            total_pending = sum(len(meta["pending"]) for meta in sat_inv.values())

            if top_priority < best_priority or (
                top_priority == best_priority and total_pending > best_pending_volume
            ):
                best_priority = top_priority
                best_pending_volume = total_pending
                best = window

        return best

    def run(self, sim_clock: "SimClock") -> None:
        """
        Main downlink loop. Advances through the schedule driven by sim_clock,
        connecting/switching satellites as needed.
        """
        while True:
            sim_time = sim_clock.now()
            best = self._pick_feasible_satellite(sim_time)

            if best is None:
                # No visible satellite can do useful work right now — jump to next event
                next_event = self._scheduler.get_next_event_time(sim_time)
                if next_event is None:
                    break
                sim_clock.advance_to(next_event)
                continue

            if self._current_pass is None or best.satellite_id != self._current_pass.satellite_id:
                self._switch_to(best, sim_clock)

            if self._conn is None:
                # Switch failed (satellite unreachable) — skip
                next_event = self._scheduler.get_next_event_time(sim_time)
                if next_event is None:
                    break
                sim_clock.advance_to(next_event)
                continue

            transferred = self._transfer_next_chunk(sim_clock)
            if not transferred:
                # This satellite is done or can no longer fit a chunk; re-evaluate
                # all visible satellites without wasting the remaining pass time.
                self._disconnect(sim_clock.now())

        self._disconnect(sim_clock.now())

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _switch_to(self, window: PassWindow, sim_clock: "SimClock") -> None:
        if self._current_pass is not None:
            logger.info(
                "[GS] Switching from %s to %s at t=%.1f",
                self._current_pass.satellite_id,
                window.satellite_id,
                sim_clock.now(),
            )
            self._event_log.record(
                sim_time=sim_clock.now(),
                pass_id=window.pass_id,
                satellite_id=window.satellite_id,
                event_type=EventType.SATELLITE_SWITCHED,
                details=f"switched from {self._current_pass.satellite_id}",
            )
            self._disconnect(sim_clock.now())

        self._connect(window, sim_clock)

    def _connect(self, window: PassWindow, sim_clock: "SimClock") -> None:
        port = self._satellite_ports.get(window.satellite_id)
        if port is None:
            logger.error("[GS] No port known for %s", window.satellite_id)
            return

        host = self._satellite_hosts.get(window.satellite_id, "127.0.0.1")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
            self._conn = sock
            self._current_pass = window
        except OSError as exc:
            logger.error("[GS] Cannot connect to %s: %s", window.satellite_id, exc)
            self._conn = None
            self._current_pass = None
            return

        # Tell the satellite what we still need. It infers confirmed =
        # everything it sent that is NOT in this list, and frees that memory.
        # Consecutive tails that reach the file's end are compressed with -1.
        sat_inv = self._scheduler._satellite_inventory.get(window.satellite_id, {})
        pending_for_sat = {
            file_id: compress_pending(list(meta["pending"]), meta.get("total_chunks") or 0)
            for file_id, meta in sat_inv.items()
            if meta["pending"]
        }
        send_message(sock, MessageType.HELLO, {"pending": pending_for_sat})
        response = recv_message(sock)
        # The satellite already filters its HELLO-ACK to only the chunks that
        # match our pending list, so no further filtering is needed here.
        inventory = response["payload"]["inventory"]

        self._scheduler.update_inventory(window.satellite_id, inventory)

        # Clear the carry-over retry queue for this satellite.  The chunks that
        # failed in the previous pass are already at the front of the freshly
        # received inventory (they have lower chunk IDs than the never-attempted
        # ones), so _pick_next_primary_chunk will reach them first without any
        # special handling.  Keeping them in _pending_retries would cause them
        # to be skipped during the primary phase and pushed to the very end —
        # the opposite of what we want.
        self._pending_retries.pop(window.satellite_id, None)

        self._event_log.record(
            sim_time=sim_clock.now(),
            pass_id=window.pass_id,
            satellite_id=window.satellite_id,
            event_type=EventType.PASS_STARTED,
        )
        logger.info(
            "[GS] Connected to %s (pass %s), %d files pending",
            window.satellite_id,
            window.pass_id,
            len(inventory),
        )

    def _disconnect(self, sim_time: float) -> None:
        if self._conn is not None:
            try:
                send_message(self._conn, MessageType.BYE, {})
            except OSError:
                pass
            self._conn.close()
            self._conn = None

        if self._current_pass is not None:
            self._event_log.record(
                sim_time=sim_time,
                pass_id=self._current_pass.pass_id,
                satellite_id=self._current_pass.satellite_id,
                event_type=EventType.PASS_ENDED,
            )
            self._current_pass = None

    # ------------------------------------------------------------------
    # Chunk transfer
    # ------------------------------------------------------------------

    def _transfer_next_chunk(self, sim_clock: "SimClock") -> bool:
        """
        One chunk attempt per call — no internal retry loop.

        Primary phase: picks the highest-priority chunk not yet in the retry
        queue.  On failure the chunk is deferred; the GS moves on to the next.

        Retry phase: once all primary chunks are exhausted, works through the
        deferred queue in order.  Each chunk gets one retry attempt.  A second
        failure leaves the chunk in the scheduler's pending list so the next
        pass (via HELLO-ACK) will include it again.

        Returns False only when there is genuinely nothing more to do right now.
        """
        if self._conn is None or self._current_pass is None:
            return False

        window = self._current_pass
        sim_time = sim_clock.now()
        sat_id = window.satellite_id

        if sim_time >= window.end:
            return False

        sat_inv = self._scheduler._satellite_inventory.get(sat_id, {})

        # --- Decide what to attempt next ---
        file_id, chunk_id = self._pick_next_primary_chunk(sat_inv, sat_id)
        is_retry = False

        if file_id is None:
            file_id, chunk_id = self._pick_next_retry_chunk(sat_id, sat_inv)
            is_retry = True

        if file_id is None:
            return False

        bw = _effective_bandwidth(window, sim_time)
        transfer_time = _chunk_transfer_time(bw)

        if transfer_time > window.end - sim_time:
            return False

        # Mid-chunk preemption check — applies to both primary and retry attempts
        better = self._find_better_satellite_during(sim_time, sim_time + transfer_time, window)
        if better is not None:
            chunk_completion_at_switch = (better.start - sim_time) / transfer_time
            if chunk_completion_at_switch < MIN_CHUNK_COMPLETION_TO_HOLD:
                sim_clock.advance_to(better.start)
                return True

        if is_retry:
            self._event_log.record(
                sim_time=sim_clock.now(),
                pass_id=window.pass_id,
                satellite_id=sat_id,
                event_type=EventType.CHUNK_RETRIED,
                file_id=file_id,
                chunk_id=chunk_id,
            )

        outcome = self._attempt_chunk(file_id, chunk_id)

        if outcome == "success":
            sim_clock.sleep(transfer_time)
            self._record_chunk_received(file_id, chunk_id, sim_clock.now())
            if is_retry:
                self._pending_retries[sat_id][file_id].remove(chunk_id)
        else:
            event_type = (
                EventType.CHUNK_DROPPED if outcome == "dropped" else EventType.CHUNK_CRC_ERROR
            )
            self._event_log.record(
                sim_time=sim_clock.now(),
                pass_id=window.pass_id,
                satellite_id=sat_id,
                event_type=event_type,
                file_id=file_id,
                chunk_id=chunk_id,
            )
            logger.debug("[GS] Chunk %s:%d %s", file_id, chunk_id, outcome)
            if not is_retry:
                # Defer: will be retried after all primary chunks are done
                self._pending_retries.setdefault(sat_id, {}).setdefault(file_id, []).append(chunk_id)
            else:
                # Retry also failed: remove from retry queue; chunk stays in
                # scheduler pending and will resurface via HELLO-ACK next pass
                self._pending_retries[sat_id][file_id].remove(chunk_id)
                logger.debug("[GS] Chunk %s:%d retry failed — carrying to next pass", file_id, chunk_id)

        return True

    def _pick_next_primary_chunk(self, sat_inv: dict, sat_id: str) -> tuple[Optional[str], Optional[int]]:
        """Highest-priority chunk not already sitting in the deferred retry queue."""
        deferred = self._pending_retries.get(sat_id, {})
        best_file: Optional[str] = None
        best_priority = 999
        for file_id, meta in sat_inv.items():
            deferred_set = set(deferred.get(file_id, []))
            available = [c for c in meta["pending"] if c not in deferred_set]
            if available and meta["priority"] < best_priority:
                best_priority = meta["priority"]
                best_file = file_id
        if best_file is None:
            return None, None
        deferred_set = set(deferred.get(best_file, []))
        available = [c for c in sat_inv[best_file]["pending"] if c not in deferred_set]
        return best_file, available[0]

    def _pick_next_retry_chunk(self, sat_id: str, sat_inv: dict) -> tuple[Optional[str], Optional[int]]:
        """First chunk from the deferred retry queue, respecting file priority order."""
        retries = self._pending_retries.get(sat_id, {})
        best_file: Optional[str] = None
        best_priority = 999
        for file_id, chunks in retries.items():
            if not chunks:
                continue
            priority = sat_inv.get(file_id, {}).get("priority", 999)
            if priority < best_priority:
                best_priority = priority
                best_file = file_id
        if best_file is None:
            return None, None
        return best_file, self._pending_retries[sat_id][best_file][0]

    def _attempt_chunk(self, file_id: str, chunk_id: int) -> str:
        """
        Single REQUEST/response cycle.
        Returns 'success', 'dropped', or 'crc_error'.
        """
        if self._conn is None:
            return "dropped"
        send_message(self._conn, MessageType.REQUEST, {"file_id": file_id, "chunk_id": chunk_id})
        response = recv_message(self._conn)
        msg_type = MessageType(response["type"])
        if msg_type == MessageType.CHUNK_DATA:
            expected_crc = zlib.crc32(f"{file_id}:{chunk_id}".encode())
            if response["payload"].get("crc") != expected_crc:
                return "crc_error"
            return "success"
        return "dropped"

    def _find_better_satellite_during(
        self, start: float, end: float, current_window: PassWindow
    ) -> Optional[PassWindow]:
        """Returns a PassWindow that starts during [start, end] with higher priority than current."""
        current_inv = self._scheduler._satellite_inventory.get(current_window.satellite_id, {})
        current_best_priority = (
            min(m["priority"] for m in current_inv.values()) if current_inv else 999
        )
        for window in self._scheduler.get_all_passes():
            if window.satellite_id == current_window.satellite_id:
                continue
            if not (start <= window.start < end):
                continue
            sat_inv = self._scheduler._satellite_inventory.get(window.satellite_id, {})
            if not sat_inv:
                continue
            # Don't preempt for a pass too short to complete even one chunk.
            # At pass open elapsed=0, factor=0.2 (worst case bandwidth).
            if _chunk_transfer_time(_effective_bandwidth(window, window.start)) > (window.end - window.start):
                continue
            candidate_priority = min(m["priority"] for m in sat_inv.values())
            if candidate_priority < current_best_priority:
                return window
        return None

    def _do_transfer(
        self, file_id: str, chunk_id: int, transfer_time: float, sim_clock: "SimClock"
    ) -> None:
        if self._conn is None or self._current_pass is None:
            return

        while True:
            send_message(self._conn, MessageType.REQUEST, {"file_id": file_id, "chunk_id": chunk_id})
            response = recv_message(self._conn)
            msg_type = MessageType(response["type"])

            if msg_type == MessageType.CHUNK_DATA:
                expected_crc = zlib.crc32(f"{file_id}:{chunk_id}".encode())
                if response["payload"].get("crc") != expected_crc:
                    self._event_log.record(
                        sim_time=sim_clock.now(),
                        pass_id=self._current_pass.pass_id,
                        satellite_id=self._current_pass.satellite_id,
                        event_type=EventType.CHUNK_CRC_ERROR,
                        file_id=file_id,
                        chunk_id=chunk_id,
                    )
                    logger.debug("[GS] CRC error on chunk %s:%d, retrying", file_id, chunk_id)
                    self._event_log.record(
                        sim_time=sim_clock.now(),
                        pass_id=self._current_pass.pass_id,
                        satellite_id=self._current_pass.satellite_id,
                        event_type=EventType.CHUNK_RETRIED,
                        file_id=file_id,
                        chunk_id=chunk_id,
                    )
                    continue
                sim_clock.sleep(transfer_time)
                self._record_chunk_received(file_id, chunk_id, sim_clock.now())
                break
            elif msg_type == MessageType.DROP:
                self._event_log.record(
                    sim_time=sim_clock.now(),
                    pass_id=self._current_pass.pass_id,
                    satellite_id=self._current_pass.satellite_id,
                    event_type=EventType.CHUNK_DROPPED,
                    file_id=file_id,
                    chunk_id=chunk_id,
                )
                logger.debug("[GS] Chunk %s:%d dropped, retrying", file_id, chunk_id)
                self._event_log.record(
                    sim_time=sim_clock.now(),
                    pass_id=self._current_pass.pass_id,
                    satellite_id=self._current_pass.satellite_id,
                    event_type=EventType.CHUNK_RETRIED,
                    file_id=file_id,
                    chunk_id=chunk_id,
                )

    def _record_chunk_received(self, file_id: str, chunk_id: int, sim_time: float) -> None:
        sat_id = self._current_pass.satellite_id  # type: ignore[union-attr]

        self._confirmed.setdefault(sat_id, {}).setdefault(file_id, set()).add(chunk_id)
        self._scheduler.mark_chunk_received(sat_id, file_id, chunk_id)

        self._event_log.record(
            sim_time=sim_time,
            pass_id=self._current_pass.pass_id,  # type: ignore[union-attr]
            satellite_id=sat_id,
            event_type=EventType.CHUNK_RECEIVED,
            file_id=file_id,
            chunk_id=chunk_id,
        )

        # Check if entire file is now confirmed
        sat_inv = self._scheduler._satellite_inventory.get(sat_id, {})
        if file_id not in sat_inv:
            self._event_log.record(
                sim_time=sim_time,
                pass_id=self._current_pass.pass_id,  # type: ignore[union-attr]
                satellite_id=sat_id,
                event_type=EventType.FILE_COMPLETED,
                file_id=file_id,
            )
            logger.info("[GS] File %s fully received from %s", file_id, sat_id)


class SimClock:
    """Accelerated simulation clock. Sleeping advances both sim time and real time."""

    def __init__(self, start: float = 0.0, scale: float = TIME_SCALE) -> None:
        self._sim_time = start
        self._scale = scale
        self._real_start = time.monotonic()

    def now(self) -> float:
        return self._sim_time

    def sleep(self, sim_seconds: float) -> None:
        self._sim_time += sim_seconds
        time.sleep(sim_seconds / self._scale)

    def advance_to(self, sim_time: float) -> None:
        delta = sim_time - self._sim_time
        if delta > 0:
            self.sleep(delta)
