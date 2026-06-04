import json
from collections import defaultdict
from typing import Any

from src.common.models import DownloadEvent, EventType, PassWindow
from settings import CHUNK_SIZE_GB, REPORT_OUTPUT_FILE


class Reporter:
    """Builds the final JSON report from the event log and pass schedule."""

    def __init__(
        self,
        passes: list[PassWindow],
        satellite_file_info: dict[str, dict[str, dict]],
    ) -> None:
        # satellite_file_info: {satellite_id: {file_id: {priority, total_chunks}}}
        self._passes = {p.pass_id: p for p in passes}
        self._satellite_file_info = satellite_file_info

    def generate(
        self,
        events: list[DownloadEvent],
        confirmed: dict[str, dict[str, set[int]]],
    ) -> dict[str, Any]:
        """
        confirmed: {satellite_id: {file_id: {completed chunk_ids}}}
        Returns the report dict and writes it to REPORT_OUTPUT_FILE.
        """
        # Group chunk-received events by pass
        chunks_by_pass: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        for event in events:
            if event.event_type == EventType.CHUNK_RECEIVED and event.file_id and event.chunk_id:
                chunks_by_pass[event.pass_id][event.file_id].append(event.chunk_id)

        report: dict[str, Any] = {}

        for pass_id, window in self._passes.items():
            sat_id = window.satellite_id
            completed_in_pass = chunks_by_pass.get(pass_id, {})
            file_info = self._satellite_file_info.get(sat_id, {})

            total_gb_by_priority: dict[str, float] = defaultdict(float)
            files_report: dict[str, dict] = {}

            for file_id, chunk_ids in completed_in_pass.items():
                priority = file_info.get(file_id, {}).get("priority", 0)
                gb = len(chunk_ids) * CHUNK_SIZE_GB
                total_gb_by_priority[str(priority)] += gb

                all_confirmed = sorted(confirmed.get(sat_id, {}).get(file_id, set()))
                total_chunks = file_info.get(file_id, {}).get("total_chunks", max(all_confirmed, default=0))
                all_chunk_ids = set(range(1, total_chunks + 1))
                pending = sorted(all_chunk_ids - set(all_confirmed))

                files_report[file_id] = {
                    "completed": all_confirmed,
                    "pending": pending,
                }

            report[pass_id] = {
                "satellite_id": sat_id,
                "total_gb": {k: round(v, 4) for k, v in total_gb_by_priority.items()},
                "files": files_report,
            }

        with open(REPORT_OUTPUT_FILE, "w") as f:
            json.dump(report, f, indent=2)

        return report
