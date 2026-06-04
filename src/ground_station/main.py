import argparse
import json
import logging
import sys
import threading
import time
from typing import Any

sys.path.insert(0, ".")

from src.ground_station.downlink import DownlinkManager, SimClock
from src.ground_station.event_log import EventLog
from src.ground_station.reporter import Reporter
from src.ground_station.scheduler import Scheduler, load_pass_schedule
from settings import DASHBOARD_PORT, PASS_SCHEDULE_FILE, POST_SIMULATION_IDLE_SECONDS, REPORT_OUTPUT_FILE, TIME_SCALE

logger = logging.getLogger(__name__)


def _build_file_info(satellite_ids: list[str]) -> dict[str, dict[str, dict]]:
    """Returns {satellite_id: {file_id: {priority, total_chunks}}} from inventory configs."""
    from pathlib import Path
    import json as _json
    from settings import CONFIG_DIR

    file_info: dict[str, dict[str, dict]] = {}
    for sat_id in satellite_ids:
        path = Path(CONFIG_DIR) / f"{sat_id}_inventory.json"
        try:
            with open(path) as f:
                records = _json.load(f)
            file_info[sat_id] = {
                r["file_id"]: {"priority": r["priority"], "total_chunks": r["total_chunks"]}
                for r in records
            }
        except FileNotFoundError:
            logger.warning("Inventory not found for %s at %s", sat_id, path)
    return file_info


def _start_dashboard(
    scheduler: Scheduler,
    event_log: EventLog,
    sim_clock: SimClock,
    sim_state: dict[str, Any],
    port: int,
) -> threading.Thread:
    """Starts the FastAPI dashboard in a background daemon thread."""
    import uvicorn
    from src.dashboard.app import create_app

    app = create_app(scheduler, event_log, sim_clock, sim_state)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True, name="dashboard")
    thread.start()
    logger.info("[GS] Dashboard running at http://0.0.0.0:%d — docs at /docs", port)
    return thread


def main() -> None:
    parser = argparse.ArgumentParser(description="Ground Station process")
    parser.add_argument(
        "--satellite-ports",
        required=True,
        help='JSON string mapping satellite_id to port, e.g. \'{"satellite_1": 9001}\'',
    )
    parser.add_argument(
        "--satellite-hosts",
        default="{}",
        help='JSON mapping satellite_id to hostname, e.g. \'{"satellite_1": "satellite_1"}\'. '
             "Defaults to 127.0.0.1 for all when omitted (local mode).",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=DASHBOARD_PORT,
        help=f"Port for the FastAPI dashboard (default: {DASHBOARD_PORT})",
    )
    args = parser.parse_args()

    satellite_ports: dict[str, int] = json.loads(args.satellite_ports)
    satellite_hosts: dict[str, str] = json.loads(args.satellite_hosts)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    passes = load_pass_schedule(PASS_SCHEDULE_FILE)
    logger.info("[GS] Loaded %d passes from schedule", len(passes))

    scheduler = Scheduler(passes)
    event_log = EventLog()
    downlink = DownlinkManager(
        scheduler=scheduler,
        satellite_ports=satellite_ports,
        satellite_hosts=satellite_hosts,
        event_log=event_log,
    )

    # Pre-load satellite inventories so scheduler can make decisions before first connection
    file_info = _build_file_info(list(satellite_ports.keys()))
    for sat_id, files in file_info.items():
        inventory = {
            fid: {
                "priority": meta["priority"],
                "total_chunks": meta["total_chunks"],
                "pending": list(range(1, meta["total_chunks"] + 1)),
            }
            for fid, meta in files.items()
        }
        scheduler.update_inventory(sat_id, inventory)

    sim_clock = SimClock(start=0.0, scale=TIME_SCALE)
    sim_state: dict[str, Any] = {"is_running": True, "current_satellite": None}

    _start_dashboard(scheduler, event_log, sim_clock, sim_state, port=args.dashboard_port)

    logger.info("[GS] Starting simulation (TIME_SCALE=%.1f)", TIME_SCALE)
    downlink.run(sim_clock)
    sim_state["is_running"] = False
    logger.info("[GS] Simulation complete")

    reporter = Reporter(passes=passes, satellite_file_info=file_info)
    report = reporter.generate(events=event_log.all(), confirmed=downlink._confirmed)

    logger.info("[GS] Report written to %s", REPORT_OUTPUT_FILE)
    print(json.dumps(report, indent=2))

    if POST_SIMULATION_IDLE_SECONDS > 0:
        logger.info(
            "[GS] Dashboard staying up for %ds — http://localhost:%d/docs  (Ctrl+C to exit)",
            POST_SIMULATION_IDLE_SECONDS,
            args.dashboard_port,
        )
        time.sleep(POST_SIMULATION_IDLE_SECONDS)


if __name__ == "__main__":
    main()
