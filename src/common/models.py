from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ChunkStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    DELETED = "deleted"


class EventType(str, Enum):
    PASS_STARTED = "pass_started"
    PASS_ENDED = "pass_ended"
    CHUNK_RECEIVED = "chunk_received"
    CHUNK_DROPPED = "chunk_dropped"
    CHUNK_CRC_ERROR = "chunk_crc_error"
    CHUNK_RETRIED = "chunk_retried"
    SATELLITE_SWITCHED = "satellite_switched"
    FILE_COMPLETED = "file_completed"
    PASS_SKIPPED = "pass_skipped"


@dataclass
class Chunk:
    file_id: str
    chunk_id: int
    size_mb: float
    status: ChunkStatus = ChunkStatus.PENDING


@dataclass
class FileRecord:
    file_id: str
    priority: int  # 1 (highest) to 5 (lowest)
    total_chunks: int
    chunks: list[Chunk] = field(default_factory=list)


@dataclass
class PassWindow:
    pass_id: str
    satellite_id: str
    start: int   # simulated seconds
    end: int     # simulated seconds
    bandwidth_gbps: float  # Gbps — field name and JSON Schema description are authoritative


@dataclass
class DownloadEvent:
    sim_time: float
    pass_id: str
    satellite_id: str
    event_type: EventType
    file_id: Optional[str] = None
    chunk_id: Optional[int] = None
    details: Optional[str] = None
