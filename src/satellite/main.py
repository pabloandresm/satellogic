import argparse
import logging
import sys

sys.path.insert(0, ".")

from src.satellite.server import SatelliteServer


def main() -> None:
    parser = argparse.ArgumentParser(description="Satellite process")
    parser.add_argument("--id", required=True, help="Satellite ID (e.g. satellite_1)")
    parser.add_argument("--port", required=True, type=int, help="TCP port to listen on")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [%(levelname)s] %(message)s",
    )

    server = SatelliteServer(satellite_id=args.id, port=args.port)
    server.serve()


if __name__ == "__main__":
    main()
