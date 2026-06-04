import json
from pathlib import Path

from src.common.models import Chunk, ChunkStatus, FileRecord
from settings import CHUNK_SIZE_MB, CONFIG_DIR


class FileManager:
    """Manages the satellite's file and chunk inventory loaded from JSON."""

    def __init__(self, satellite_id: str) -> None:
        self._satellite_id = satellite_id
        self._files: dict[str, FileRecord] = {}
        self._load(Path(CONFIG_DIR) / f"{satellite_id}_inventory.json")

    def _load(self, path: Path) -> None:
        with open(path) as f:
            raw: list[dict] = json.load(f)

        for entry in raw:
            file_id = entry["file_id"]
            priority = entry["priority"]
            total_chunks = entry["total_chunks"]
            chunks = [
                Chunk(
                    file_id=file_id,
                    chunk_id=i,
                    size_mb=CHUNK_SIZE_MB,
                    status=ChunkStatus(entry.get("chunk_statuses", {}).get(str(i), ChunkStatus.PENDING)),
                )
                for i in range(1, total_chunks + 1)
            ]
            self._files[file_id] = FileRecord(
                file_id=file_id,
                priority=priority,
                total_chunks=total_chunks,
                chunks=chunks,
            )

    def get_pending_inventory(self) -> dict[str, dict]:
        """
        Returns {file_id: {priority, pending: [chunk_ids]}} for files with untransmitted
        or sent-but-unconfirmed chunks (PENDING or COMPLETED status).
        """
        result = {}
        for file_id, record in self._files.items():
            pending = [
                c.chunk_id for c in record.chunks
                if c.status in (ChunkStatus.PENDING, ChunkStatus.COMPLETED)
            ]
            if pending:
                result[file_id] = {"priority": record.priority, "pending": pending}
        return result

    def free_non_pending_chunks(self, pending: dict[str, list[int]]) -> None:
        """
        Free all COMPLETED chunks the GS no longer needs.

        The GS HELLO now carries the list of chunks it still wants (pending),
        not the list of chunks it has already received (confirmed).  Any chunk
        this satellite has sent (COMPLETED) that is absent from the pending list
        was received successfully by the GS — it can be freed now.
        """
        for file_id, record in self._files.items():
            needed = set(pending.get(file_id, []))
            for chunk in record.chunks:
                if chunk.chunk_id not in needed and chunk.status == ChunkStatus.COMPLETED:
                    chunk.status = ChunkStatus.DELETED

    def get_total_chunks(self, file_id: str) -> int:
        record = self._files.get(file_id)
        return record.total_chunks if record else 0

    def get_chunk(self, file_id: str, chunk_id: int) -> Chunk | None:
        record = self._files.get(file_id)
        if not record:
            return None
        for chunk in record.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk
        return None

    def mark_chunk_completed(self, file_id: str, chunk_id: int) -> None:
        chunk = self.get_chunk(file_id, chunk_id)
        if chunk and chunk.status == ChunkStatus.PENDING:
            chunk.status = ChunkStatus.COMPLETED

    def free_chunks_before(self, file_id: str, chunk_id: int) -> None:
        """
        Mark all COMPLETED chunks with id < chunk_id as DELETED.
        Called when the GS retries chunk N: by asking for N again the GS
        implicitly confirms it received everything before N, so the satellite
        can release that memory now rather than waiting for the next HELLO.
        """
        record = self._files.get(file_id)
        if not record:
            return
        for chunk in record.chunks:
            if chunk.chunk_id < chunk_id and chunk.status == ChunkStatus.COMPLETED:
                chunk.status = ChunkStatus.DELETED

    def is_file_fully_sent(self, file_id: str) -> bool:
        """True only when all chunks have been confirmed by GS (all DELETED)."""
        record = self._files.get(file_id)
        if not record:
            return False
        return all(c.status == ChunkStatus.DELETED for c in record.chunks)

    def delete_file_chunks(self, file_id: str) -> None:
        record = self._files.get(file_id)
        if record:
            for chunk in record.chunks:
                chunk.status = ChunkStatus.DELETED

    @property
    def satellite_id(self) -> str:
        return self._satellite_id
