import logging
import random
import socket
import zlib

from src.common.models import ChunkStatus
from src.common.protocol import MessageType, expand_pending, recv_message, send_message
from src.satellite.file_manager import FileManager
from settings import NOISE_PROBABILITY, PACKET_DROP_PROBABILITY

logger = logging.getLogger(__name__)


class SatelliteServer:
    """TCP socket server that handles one GS connection at a time."""

    def __init__(self, satellite_id: str, port: int) -> None:
        self._file_manager = FileManager(satellite_id)
        self._port = port
        self._satellite_id = satellite_id

    def serve(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock.bind(("0.0.0.0", self._port))
            server_sock.listen(1)
            logger.info("[%s] Listening on port %d", self._satellite_id, self._port)

            while True:
                conn, addr = server_sock.accept()
                logger.info("[%s] GS connected from %s", self._satellite_id, addr)
                try:
                    self._handle_session(conn)
                except (ConnectionError, OSError) as exc:
                    logger.debug("[%s] Session ended: %s", self._satellite_id, exc)
                finally:
                    conn.close()

    def _handle_session(self, conn: socket.socket) -> None:
        while True:
            msg = recv_message(conn)
            msg_type = MessageType(msg["type"])
            payload = msg["payload"]

            if msg_type == MessageType.HELLO:
                self._handle_hello(conn, payload)
            elif msg_type == MessageType.REQUEST:
                self._handle_request(conn, payload)
            elif msg_type == MessageType.BYE:
                logger.info("[%s] Received BYE, closing session", self._satellite_id)
                break
            else:
                logger.warning("[%s] Unknown message type: %s", self._satellite_id, msg_type)

    def _handle_hello(self, conn: socket.socket, payload: dict) -> None:
        raw_pending: dict[str, list[int]] = payload.get("pending", {})

        # Expand any compressed pending lists (-1 sentinel → full range).
        pending: dict[str, list[int]] = {
            file_id: expand_pending(ids, self._file_manager.get_total_chunks(file_id))
            for file_id, ids in raw_pending.items()
        }

        # Free any chunk we already sent that the GS no longer needs —
        # its absence from the pending list means it was received successfully.
        self._file_manager.free_non_pending_chunks(pending)

        for file_id in self._file_manager.get_pending_inventory():
            if self._file_manager.is_file_fully_sent(file_id):
                logger.info("[%s] File %s fully confirmed by GS", self._satellite_id, file_id)

        # Return only the chunks the GS asked for and that we still have.
        full_inventory = self._file_manager.get_pending_inventory()
        inventory = {}
        for file_id, needed_ids in pending.items():
            if file_id not in full_inventory:
                continue
            needed_set = set(needed_ids)
            available = [c for c in full_inventory[file_id]["pending"] if c in needed_set]
            if available:
                inventory[file_id] = {"priority": full_inventory[file_id]["priority"], "pending": available}

        send_message(conn, MessageType.HELLO_ACK, {"inventory": inventory})
        logger.debug("[%s] HELLO_ACK sent, pending files: %s", self._satellite_id, list(inventory.keys()))

    def _handle_request(self, conn: socket.socket, payload: dict) -> None:
        file_id: str = payload["file_id"]
        chunk_id: int = payload["chunk_id"]

        chunk = self._file_manager.get_chunk(file_id, chunk_id)

        if chunk is None or chunk.status == ChunkStatus.DELETED:
            logger.warning("[%s] Requested missing/deleted chunk %s:%d", self._satellite_id, file_id, chunk_id)
            send_message(conn, MessageType.DROP, {"file_id": file_id, "chunk_id": chunk_id})
            return

        if chunk.status == ChunkStatus.COMPLETED:
            # GS is retrying this chunk — it implicitly confirms all chunks that
            # came before it in the sequence, so we can free that memory now.
            self._file_manager.free_chunks_before(file_id, chunk_id)
            logger.debug("[%s] Retry for %s:%d — freed chunks before it", self._satellite_id, file_id, chunk_id)

        if random.random() < PACKET_DROP_PROBABILITY:
            logger.debug("[%s] DROP simulated for chunk %s:%d", self._satellite_id, file_id, chunk_id)
            send_message(conn, MessageType.DROP, {"file_id": file_id, "chunk_id": chunk_id})
            return

        crc = zlib.crc32(f"{file_id}:{chunk_id}".encode())
        #if random.random() < NOISE_PROBABILITY:
        #    crc ^= 0xFFFFFFFF  # simulate bit-noise: guaranteed CRC mismatch at GS
        #    logger.debug("[%s] Noise injected for chunk %s:%d", self._satellite_id, file_id, chunk_id)

        self._file_manager.mark_chunk_completed(file_id, chunk_id)
        send_message(
            conn,
            MessageType.CHUNK_DATA,
            {"file_id": file_id, "chunk_id": chunk_id, "size_mb": chunk.size_mb, "crc": crc},
        )
        logger.debug("[%s] Sent chunk %s:%d", self._satellite_id, file_id, chunk_id)
