BASE_PORT: int = 9000                             # base port for satellite communication
DASHBOARD_PORT: int = 8000                        # port for FastAPI dashboard communication
POST_SIMULATION_IDLE_SECONDS: int = 30           # keep dashboard alive after sim ends (set 0 to disable)
TIME_SCALE: float = 100.0                         # 1 simulated second = 1/TIME_SCALE real seconds
MIN_CHUNK_COMPLETION_TO_HOLD: float = 0.8        # if 80% of chunk is completed, then do not switch to higher priority satellite
PACKET_DROP_PROBABILITY: float = 0.05            # probability of packet drop (satellite sends DROP instead of CHUNK_DATA)
NOISE_PROBABILITY: float = 0.02                  # probability of bit-noise corrupting a chunk in transit (detected via CRC mismatch)
CHUNK_SIZE_MB: float = 100.0                     # chunk size in MB
CHUNK_SIZE_GB: float = CHUNK_SIZE_MB / 1000      # SI: 1 GB = 1000 MB; transfer time = CHUNK_SIZE_GB * 8 / bandwidth_gbps

CONFIG_DIR: str = "config"                                      # directory for configuration files
PASS_SCHEDULE_FILE: str = f"{CONFIG_DIR}/pass_schedule.json"    # file containing satellite pass schedule
REPORT_OUTPUT_FILE: str = "report.json"                         # file containing simulation report

SOCKET_BUFFER_SIZE: int = 4096                                  # buffer size for socket communication
SATELLITE_STARTUP_DELAY: float = 1.0                            # seconds to wait for satellites to bind before GS starts
