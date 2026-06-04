import json
import socket
from enum import Enum
from typing import Any

from settings import SOCKET_BUFFER_SIZE


class MessageType(str, Enum):
    HELLO = "hello"           # GS → Sat: {pending: {file_id: [chunk_ids]}}  — chunks GS still needs; satellite infers confirmed = sent − pending
    HELLO_ACK = "hello_ack"   # Sat → GS: {inventory: {file_id: {priority, pending: [chunk_ids]}}}
    REQUEST = "request"       # GS → Sat: {file_id, chunk_id}
    CHUNK_DATA = "chunk_data" # Sat → GS: {file_id, chunk_id, size_mb, crc}
    DROP = "drop"             # Sat → GS: {file_id, chunk_id}  (simulated packet loss)
    BYE = "bye"               # GS → Sat: {}


def compress_pending(pending: list[int], total_chunks: int) -> list[int]:
    """
    Replace a consecutive tail that reaches total_chunks with the -1 sentinel.

    The sentinel means "and everything from here to the end of the file."
    Only applied when the tail genuinely reaches the file's last chunk so
    the satellite can expand unambiguously using its own total_chunks.

    Examples (total_chunks = 50):
        [3, 7, 11, 12, ..., 50] → [3, 7, 11, -1]
        [3, 7, 48]              → [3, 7, 48]        (tail does not reach 50)
        [1, 2, ..., 50]         → [1, -1]
    """
    if not pending or pending[-1] != total_chunks:
        return pending

    tail_start = len(pending) - 1
    while tail_start > 0 and pending[tail_start - 1] == pending[tail_start] - 1:
        tail_start -= 1

    if len(pending) - tail_start < 2:
        return pending  # single-element tail — nothing to save

    return pending[:tail_start] + [pending[tail_start], -1]


def expand_pending(compressed: list[int], total_chunks: int) -> list[int]:
    """
    Expand a pending list that may end with the -1 sentinel.

    Examples (total_chunks = 50):
        [3, 7, 11, -1] → [3, 7, 11, 12, ..., 50]
        [3, 7, 48]     → [3, 7, 48]               (no sentinel, unchanged)
    """
    if not compressed or compressed[-1] != -1:
        return compressed

    prefix = compressed[:-1]
    last_explicit = prefix[-1] if prefix else 0
    return prefix + list(range(last_explicit + 1, total_chunks + 1))


def send_message(sock: socket.socket, msg_type: MessageType, payload: dict[str, Any]) -> None:
    message = json.dumps({"type": msg_type.value, "payload": payload}) + "\n"
    sock.sendall(message.encode())


def recv_message(sock: socket.socket) -> dict[str, Any]:
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(SOCKET_BUFFER_SIZE)
        if not chunk:
            raise ConnectionError("Socket closed unexpectedly")
        data += chunk
    return json.loads(data.decode().strip())
