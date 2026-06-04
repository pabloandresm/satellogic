# Satellite Fleet Downlink Scheduler

A prototype simulation of a Mission Control Downlink Scheduler. The system manages high-volume data transfers from a fleet of LEO satellites to a single ground station, maximising the retrieval of high-priority files within overlapping, bandwidth-limited pass windows.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Quick Start — Docker](#quick-start--docker)
3. [Quick Start — Local Python](#quick-start--local-python)
4. [Configuration](#configuration)
5. [FastAPI Dashboard](#fastapi-dashboard)
6. [Design Decisions](#design-decisions)
7. [Project Structure](#project-structure)
8. [Known Spec Ambiguity](#known-spec-ambiguity)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                   Ground Station Process                          │
│                                                                   │
│   Scheduler ──► DownlinkManager ──► Reporter                    │
│       │                │                                          │
│   PassQueue        EventLog ◄──────────────────┐              │      
│  (thread-safe)   (thread-safe)                     │              │
│                       │                            │              │
│              FastAPI Dashboard Thread              │              │
│              (GET /status, /events, …)             │              │
└───────────────────────┼─────────────────────────────────┘
                 TCP Socket │ (HELLO / REQUEST / CHUNK_DATA / DROP / BYE)
         ┌─────────────┘             └─────────────┐
         ▼                                             ▼
┌─────────────────┐                     ┌─────────────────┐
│  Satellite_1       │                     │  Satellite_2       │
│  Process           │         …           │  Process           │
│  (TCP Server)      │                     │  (TCP Server)      │
│  JSON inventory    │                     │  JSON inventory    │
└─────────────────┘                     └─────────────────┘
```

Three independent OS processes communicate over TCP sockets. The number of satellite processes is determined at runtime by reading the pass schedule — no code change is needed to add a third satellite.

---

## Quick Start — Docker

### Prerequisites

- Docker Engine ≥ 24 with the Compose plugin (`docker compose version`)
- Python 3.12+ (to run the dynamic launcher/generator script)

### Run (Dynamic / Auto-Scaling Mode)

The system dynamically discovers the satellite configuration from the `/config` folder (such as the number of active satellites and their respective ports) and generates the correct `docker-compose.yml` topology on the fly.

To automatically generate/update the compose file and launch the simulation in one command:
```bash
python run.py --docker
```

Alternatively, if you prefer to generate the file and execute Docker Compose commands manually:
```bash
# 1. Regenerate docker-compose.yml based on current config
python run.py --generate-docker

# 2. Run the simulation
docker compose up --build --abort-on-container-exit
```

The ground station exits when the simulation finishes; `--abort-on-container-exit` stops the satellite containers automatically.

The final JSON report is printed to stdout (visible in the compose log) and written to `report.json` inside the `ground_station` container. To copy it out:

```bash
docker cp satellogic-ground_station-1:/app/report.json ./report.json
```

The **FastAPI dashboard** is published on port 8000 and stays up for 30 seconds after the simulation ends:

```
http://localhost:8000/docs
```

To tear down all containers and networks:

```bash
docker compose down
```

---

## Quick Start — Local Python

### Prerequisites

- Python 3.12+

### Install dependencies

```bash
pip install -r requirements.txt
```

### Run

```bash
python run.py
```

`run.py` reads `config/pass_schedule.json`, discovers all unique satellite IDs, spawns one satellite process per ID, waits for them to bind their ports, then starts the ground station process. All processes are waited on before exit.

### Customising the scenario

Edit the files in `config/` — no code changes required:

| File | Purpose |
|---|---|
| `config/pass_schedule.json` | Pass windows (satellite, start/end time, bandwidth) |
| `config/satellite_N_inventory.json` | File + chunk inventory for each satellite |

To switch schedule, set `PASS_SCHEDULE_FILE` in `settings.py`. To add a third satellite: add `satellite_3_inventory.json` and add passes for `satellite_3` to the schedule. `run.py` discovers the new satellite automatically.

---

### Pass Schedules

Three ready-made schedules are provided in `config/`. All use `bandwidth_gbps: 1.0` (1 Gbps). With 100 MB chunks, each chunk takes 0.8 s at zenith and up to 4 s near the horizon edges of a pass.

#### `pass_schedule.json` — Illustrative example from the spec

Two overlapping 10-second passes at 1 Gbps. Designed to match the spec's worked example. With the Gbps interpretation, only a fraction of each file transfers — the purpose is to demonstrate the scheduler switching satellites, not to complete the files.

| Pass | Satellite | Window | Notes |
|---|---|---|---|
| pass_1 | satellite_1 | t=0–10 | file_a (priority 3) |
| pass_2 | satellite_2 | t=5–15 | file_b (priority 1); GS switches at t=5 |

#### `better_pass_schedule.json` — Non-overlapping, guarantees full transfer

Fourteen non-overlapping 20-second passes (5-second gaps between them). Each uncontested pass yields approximately 16 chunks. No scheduling decisions are exercised — one satellite is always visible at a time. Useful as a correctness baseline.

| Passes | Satellite | Expected outcome |
|---|---|---|
| pass_s1_1 … pass_s1_7 | satellite_1 | ~16 chunks each; file_a (100 chunks) completes during pass_s1_7 |
| pass_s1_8 | satellite_1 | Empty — file_a already done |
| pass_s2_1 … pass_s2_4 | satellite_2 | file_b (50 chunks) completes during pass_s2_4 (~2 chunks) |
| pass_s2_5, pass_s2_6 | satellite_2 | Empty — file_b already done |

#### `better_overlap_pass_schedule.json` — Overlapping passes, exercises all scheduler decisions

Fourteen passes across six scenario groups. Both files complete fully. Designed to exercise every branch of the scheduling algorithm.

**Scenario A — Preemption: low-priority satellite preempted mid-pass**
`pass_s1_1` (t=0–22) starts uncontested. `pass_s2_1` (t=10–30) opens at t=10. The scheduler detects satellite_2 (file_b, priority 1) during satellite_1's next chunk transfer. Completion at switch < 80 % → chunk is abandoned, GS switches to satellite_2 immediately.

**Scenario B — Simultaneous open, priority selection**
`pass_s2_2` (t=40–60) and `pass_s1_2` (t=40–65) open at the same time. The scheduler picks satellite_2 from the very first chunk. Satellite_1 only receives the 5-second tail (t=60–65) after satellite_2's pass ends.

**Scenario C — Uncontested baseline**
`pass_s1_3` (t=75–95) and `pass_s2_3` (t=105–125) are separated by a gap. Each satellite downloads at full sine-wave throughput with no competition. Reference point for throughput numbers.

**Scenario D — Preemption + switch-back after file complete**
`pass_s1_4` (t=135–165) runs uncontested until `pass_s2_4` (t=150–170) opens. The GS switches to satellite_2 (chunk abandoned at ~12 % completion). Satellite_2 needs only its remaining ~2 chunks to finish file_b (~t=155). Once satellite_2's inventory is empty the GS disconnects and reconnects to satellite_1 — whose pass is still open until t=165 — and resumes downloading file_a.

**Scenario E — Empty pass ignored (×2)**
`pass_s2_5` (t=185–205) overlaps `pass_s1_5` (t=180–200), and `pass_s2_6` (t=250–270) overlaps `pass_s1_7` (t=245–265). In both cases file_b is already complete, so satellite_2's inventory is empty. `pick_best_satellite` skips satellite_2 and the GS serves satellite_1 uninterrupted for the full pass duration.

**Uncontested catch-up passes**
`pass_s1_6` (t=215–235) and `pass_s1_8` (t=275–295) are uncontested satellite_1 passes that allow file_a to finish. File_a completes partway through pass_s1_8.

**Scenario not covered — `MIN_CHUNK_COMPLETION_TO_HOLD` hold (≥ 80 %)**
The scheduler also handles the case where a higher-priority satellite's pass opens *during* a chunk transfer but the chunk is already ≥ 80 % complete: the GS finishes the chunk before switching rather than abandoning it. Reliably triggering this in a JSON schedule with integer-second pass start times requires the new pass to open within a very narrow window (< 0.2 s wide near zenith) after a chunk started. No pass pair in this file is guaranteed to hit that window, so this branch is exercised by the code logic but not demonstrated by any named scenario here. The threshold is configurable via `MIN_CHUNK_COMPLETION_TO_HOLD` in `settings.py`.

---

## Configuration

All tunables live in `settings.py`:

| Setting | Default | Description |
|---|---|---|
| `BASE_PORT` | `9000` | Satellite TCP ports are assigned as `BASE_PORT + 1`, `BASE_PORT + 2`, … |
| `DASHBOARD_PORT` | `8000` | FastAPI dashboard HTTP port. |
| `POST_SIMULATION_IDLE_SECONDS` | `30` | Seconds the dashboard stays up after the simulation ends. Set to `0` to disable. |
| `TIME_SCALE` | `20.0` | Simulated seconds per real second. `20` = 20× faster than real time. |
| `MIN_CHUNK_COMPLETION_TO_HOLD` | `0.8` | If a higher-priority satellite's pass opens while a chunk is in flight and the chunk is already ≥ 80 % complete, finish it before switching. Below 80 % the chunk is abandoned immediately. |
| `PACKET_DROP_PROBABILITY` | `0.05` | Probability the satellite sends `DROP` instead of `CHUNK_DATA` (simulated packet loss, 5 %). Chunk is deferred to the retry phase. |
| `NOISE_PROBABILITY` | `0.02` | Probability that in-transit bit noise corrupts a chunk (2 %). Detected via CRC mismatch at the GS; chunk is deferred to the retry phase. |
| `CHUNK_SIZE_MB` | `100.0` | Uniform chunk size in MB (SI: 1 GB = 1000 MB). |
| `CHUNK_SIZE_GB` | `0.1` | Derived from `CHUNK_SIZE_MB / 1000`. Used in transfer-time calculations: `CHUNK_SIZE_GB × 8 / bandwidth_gbps`. |
| `CONFIG_DIR` | `"config"` | Directory that holds the pass schedule and satellite inventory JSON files. |
| `PASS_SCHEDULE_FILE` | `"config/pass_schedule.json"` | Active pass schedule. Change to `better_pass_schedule.json` or `better_overlap_pass_schedule.json` to run the extended scenarios. |
| `REPORT_OUTPUT_FILE` | `"report.json"` | Path where the final download report is written. |
| `SOCKET_BUFFER_SIZE` | `4096` | TCP receive buffer size in bytes for the newline-delimited JSON protocol. |
| `SATELLITE_STARTUP_DELAY` | `1.0` | Seconds `run.py` waits after starting satellite processes before starting the GS (local mode only; Docker uses health checks). |

---

## FastAPI Dashboard

The ground station starts a FastAPI server in a background thread before the simulation begins. It is available at `http://localhost:8000` (or the configured port).

Interactive docs: `http://localhost:8000/docs`

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Liveness check |
| `/status` | GET | Live sim time, active satellite, chunk counters |
| `/passes` | GET | Full pass schedule |
| `/passes` | POST | Inject new pass windows into the running scheduler |
| `/events` | GET | All logged events (filterable by `satellite_id`, `event_type`) |
| `/schedule/timeline` | GET | Per-pass summary with events — designed for visualisation |

### Injecting a new pass at runtime

```bash
curl -X POST http://localhost:8000/passes \
  -H "Content-Type: application/json" \
  -d '{
    "pass_3": {
      "satellite_id": "satellite_1",
      "start": 20,
      "end": 30,
      "bandwidth_gbps": 0.8
    }
  }'
```

---

## Design Decisions

### IPC: TCP Sockets

**Choice:** each satellite runs a TCP socket server; the ground station is the client.

**Justification:**

TCP was chosen over the other candidates for the following reasons:

- **Natural metaphor.** A TCP connection maps directly to an active pass window: the GS *connects* when the pass opens and *disconnects* (sending `BYE`) when it ends or switches satellite. The existence of a connection IS the pass.
- **Docker-ready without extra infrastructure.** Unlike Redis Pub/Sub or gRPC, TCP requires no external broker. Each container is reachable by its service name on the shared bridge network.
- **Application-layer reliability is sufficient.** Because this is a behavioural simulation, packet loss (the 5 % drop) is implemented at the application layer — the satellite rolls a dice before responding and sends a `DROP` message instead of `CHUNK_DATA`. TCP as the transport layer is irrelevant to this simulation; the reliability we actually model is our own HELLO/NACK protocol.
- **Simpler than gRPC** for a prototype. gRPC adds protobuf compilation, stub generation, and a more complex deployment. TCP with newline-delimited JSON is transparent and debuggable with `netcat`.

UDP (or R-UDP as used in game networking) would be a more realistic analogy for a radio link. However, because the drop simulation is programmatic rather than network-level, the transport choice does not affect simulation fidelity.

### Communication Protocol

#### Message flow

```
Pass opens:
  GS → Sat:  HELLO     { pending: {file_id: [chunk_ids …, -1?]} }
  Sat → GS:  HELLO_ACK { inventory: {file_id: {priority, pending: [chunk_ids]}} }

Primary phase — first attempt per chunk, in order:
  GS → Sat:  REQUEST   { file_id, chunk_id }
  Sat → GS:  CHUNK_DATA { file_id, chunk_id, size_mb, crc }   ← success
           | DROP       { file_id, chunk_id }                  ← 5 % simulated packet loss

Retry phase — after all primary chunks are attempted:
  GS → Sat:  REQUEST   { file_id, chunk_id }   ← re-request of failed chunks, in order
  Sat → GS:  CHUNK_DATA { … }  |  DROP { … }

Pass ends / satellite switch:
  GS → Sat:  BYE
```

#### HELLO — pending list, not confirmed list

The GS sends what it **still needs** (`pending`), not what it has already received (`confirmed`). The satellite infers confirmed = everything it has sent (status `COMPLETED`) that is **absent** from the pending list, and frees that memory immediately.


**`-1` sentinel for compact encoding.** When the pending list ends in a consecutive run that reaches the file's last chunk, the run is replaced with `-1` meaning "and everything from here to the end of the file." The satellite expands this using its own `total_chunks`.

```
50-chunk file, chunks 1–2 and 4–6 and 8–10 already confirmed:
  Full form:    { file_a: [3, 7, 11, 12, 13, …, 50] }
  Compact form: { file_a: [3, 7, 11, -1] }
```

#### HELLO_ACK — filtered inventory

The satellite returns only the intersection of what it still has and what the GS asked for, so the GS does not need to filter the response.

#### CRC — noise detection

Each `CHUNK_DATA` message includes a CRC-32 field computed over the chunk's identity string (`"{file_id}:{chunk_id}"`). With probability `NOISE_PROBABILITY` the satellite corrupts the CRC before sending, simulating in-transit bit noise. The GS recomputes the expected CRC on receipt; a mismatch is treated identically to a `DROP` — the chunk is deferred for retry.

> **Simulation note:** chunks carry no actual bytes in this simulation — only metadata (file_id, chunk_id, size_mb). The CRC is therefore computed over the chunk's identity rather than real content. It correctly exercises the detection-and-retry behaviour without requiring real payload data.

#### Deferred retransmission

Failed chunks (DROP or CRC error) are **not retried immediately**. Each pass is divided into two phases.

**Primary phase.** The GS attempts each pending chunk once, in order. On failure the chunk is appended to a per-satellite, per-file retry queue and the GS moves on to the next chunk immediately.

**Retry phase.** Once the primary queue is exhausted, the GS works through the retry queue in file-priority then chunk-id order. Each chunk gets exactly one attempt:
- **Success** → confirmed, removed from the retry queue.
- **Failure** → removed from the retry queue; the chunk stays in the scheduler's pending list as a carry-over for the next pass. It will not be attempted again in this pass.

**Next pass.** At the start of every new connection the retry queue for that satellite is cleared entirely. Carry-over chunks (whether un-retried or doubly-failed) are in the scheduler's pending list with their original low chunk IDs. The HELLO sent to the satellite includes them, the HELLO-ACK confirms they still need to be transferred, and `_pick_next_primary_chunk` selects them first — not as retries, but as ordinary primary chunks that happen to appear at the front of the queue.

#### Implicit in-pass acknowledgement on retry

When the GS retries chunk N, the satellite infers that all chunks it previously sent with id < N were successfully received, and frees those from memory immediately — without waiting for the next HELLO.

### Scheduling Algorithm

**Goal:** maximise priority-weighted volume of data downloaded across all passes.

**Algorithm:** priority-based and event-driven. Before ranking visible satellites by priority, any satellite that cannot deliver a complete chunk right now is discarded — either because it has nothing left to send, or because its pass window is too close to ending to fit even one transfer. Only the survivors are ranked.

---

#### Ground station is always the initiator

The GS owns the pass schedule and knows in advance when each satellite will be visible. Satellites are passive: they run a TCP server and wait to be contacted. When a pass window opens the GS connects; when it closes (or a better satellite appears) the GS sends `BYE` and disconnects. A satellite never announces its own presence.

---

#### Decision points

The scheduler re-evaluates which satellite to serve whenever:

- a pass window opens or closes,
- a file transfer completes (satellite inventory becomes empty),
- a chunk finishes transferring (normal loop iteration),
- a chunk cannot fit in the remaining pass time,
- a higher-priority satellite's pass will open *during* the current chunk transfer — the scheduler detects this proactively at the start of each chunk by looking ahead across the chunk's duration, and may abandon the transfer early to switch.

At every decision point `_pick_feasible_satellite` is called. It iterates all currently visible passes and applies two filters before ranking.

---

#### Filter 1 — Inventory

A satellite with no pending chunks is skipped entirely. Once a file is fully downloaded the satellite is invisible to the scheduler even if its pass window is still open.

---

#### Filter 2 — Time feasibility

A satellite is skipped if the chunk transfer time at the current effective bandwidth exceeds the remaining pass time. This ensures no slot is wasted:

- After completing a chunk, if the next chunk would not finish before the pass ends, the GS immediately re-evaluates *all* visible satellites rather than sitting idle until the pass expires.
- A lower-priority satellite that still has time may be selected over a higher-priority one that does not.
- Time is only advanced when *no* visible satellite passes both filters.

---

#### Satellite selection

Among the satellites that pass both filters the GS selects the one with the **lowest priority number** (highest urgency). Ties are broken by total pending volume — more pending data wins, maximising future throughput.

---

#### Mid-chunk preemption (`MIN_CHUNK_COMPLETION_TO_HOLD`)

This rule applies only when a higher-priority satellite is **not yet visible** but will rise over the horizon *during* the current chunk transfer. A satellite that is already visible would have been selected by `_pick_feasible_satellite` before the chunk even started.

At the start of each chunk transfer the scheduler looks ahead across the chunk's duration: will a higher-priority satellite's pass open before this chunk finishes?

```
chunk_completion_at_switch = (new_pass.start − transfer_start) / transfer_time
```

- `chunk_completion_at_switch < MIN_CHUNK_COMPLETION_TO_HOLD (0.8)`:
  abandon the chunk now and wait — the GS advances the clock to `new_pass.start` (the satellite is still below the horizon; there is nothing to do in that gap) then connects to the higher-priority satellite.

- `chunk_completion_at_switch ≥ 0.8`:
  finish the chunk first, then switch. The satellite will have been visible for a short time already and the marginal cost of completing the chunk is low.

Chunks are atomic — partial data is worthless. The threshold balances the cost of abandoning nearly-complete work against the benefit of reaching the higher-priority satellite sooner.

A preemption is only considered if the incoming satellite's pass is long enough to complete at least one chunk at worst-case bandwidth (elevation factor = 0.2, horizon). Passes too short to be productive are never used as a preemption trigger.

---

### Dynamic Bandwidth

Signal strength follows a sine-wave model peaking at zenith (the midpoint of the pass window) and degrading to 20 % of nominal capacity at the horizon (start and end of the pass):

```python
factor = 0.2 + 0.8 * sin(π × elapsed / duration)
effective_bw_gbps = nominal_bandwidth_gbps × factor
chunk_transfer_time = (CHUNK_SIZE_GB × 8) / effective_bw_gbps  # GB → Gbit, divide by Gbps
```

The bandwidth is sampled at the start of each chunk transfer. This affects both throughput calculations and the `MIN_CHUNK_COMPLETION_TO_HOLD` timing: near the horizon, chunks take longer, which makes mid-chunk switches more likely to exceed the hold threshold.

### Signal Loss and Packet Drop

A 5 % probability of packet drop is applied server-side per chunk response. The satellite rolls `random.random() < 0.05` before sending `CHUNK_DATA`; if True it sends `DROP` instead. The GS retries the same chunk immediately. This is modelled at the application layer, making it independent of the transport protocol choice.

### Container Strategy: One Container Per Process

1. **True isolation matches the real-world model.** In an actual mission, each satellite ground terminal is a physically separate system. Separate containers make that boundary explicit and verifiable — each satellite has its own filesystem, network namespace, and process space, with no shared memory whatsoever.

2. **The spec's multi-process requirement is visible and enforceable.** With separate containers, the process separation is a first-class architectural fact: you can restart, inspect, or replace one satellite container without touching the others.

3. **The IPC mechanism is actually exercised.** With separate containers on a shared bridge network, the ground station genuinely resolves `satellite_1` via Docker's internal DNS and connects across container boundaries — the same mechanism that would work if the containers were on different physical hosts.


### N-Satellite Scalability

The system never hardcodes the number of satellites:

- **Local Python Mode**: `run.py` reads `config/pass_schedule.json`, extracts the unique set of `satellite_id` values, and spawns exactly one process per satellite. Port assignments are derived automatically (`BASE_PORT + index`).
- **Docker Compose Mode**: Running `python run.py --generate-docker` (or `--docker`) dynamically generates or updates the `docker-compose.yml` configuration. This maps one isolated Docker service container per discovered satellite and assigns consecutive ports (`BASE_PORT + index`), matching the ground station's command arguments dynamically.

Adding a new satellite requires only a new inventory JSON file and passes in the schedule.

### Time Simulation

The simulation runs on an accelerated clock (`TIME_SCALE`). All `sleep()` calls are scaled: a simulated chunk transfer of `t` seconds causes `time.sleep(t / TIME_SCALE)` in real time. This makes the simulation fast for testing while preserving the relative timing of all events. The `SimClock` object is shared (read-only) with the FastAPI dashboard thread for live time reporting.

---

## Project Structure

```
satellogic/
├── config/
│   ├── better_overlap_pass_schedule.json  # Pass windows (input)
│   ├── better_pass_schedule.json          # Pass windows (input)
│   ├── pass_schedule.json                 # Pass windows (input)
│   ├── satellite_1_inventory.json         # satellite_1 file inventory
│   └── satellite_2_inventory.json         # satellite_2 file inventory
├── src/
│   ├── common/
│   │   ├── models.py                      # Shared dataclasses and enums
│   │   └── protocol.py                    # Socket message types and send/recv helpers
│   ├── ground_station/
│   │   ├── main.py                        # GS process entry point
│   │   ├── scheduler.py                   # Scheduling algorithm and pass queue
│   │   ├── downlink.py                    # Active connection manager + SimClock
│   │   ├── event_log.py                   # Thread-safe event log
│   │   └── reporter.py                    # Final JSON report generator
│   ├── satellite/
│   │   ├── main.py                        # Satellite process entry point
│   │   ├── server.py                      # TCP socket server
│   │   └── file_manager.py                # JSON-based chunk inventory
│   └── dashboard/
│       └── app.py                         # FastAPI application factory
├── run.py                                 # Local launcher (spawns all processes)
├── settings.py                            # All tunables in one place
├── Dockerfile                             # Single image for all services
├── docker-compose.yml                     # 3-service orchestration
├── .dockerignore
└── requirements.txt
```

---

### Pre-Known Satellite Inventory (Exercise Simplification)

In a real mission the typical discovery flow is:

1. The satellite computes its own inventory (which files it holds, chunk counts, and maybe their priorities).
2. The GS connects during a pass → satellite announces its inventory via HELLO-ACK.
3. The GS schedules based on what it just learned.

In this simulation the GS pre-loads all satellite inventories from the config files at startup (`_build_file_info`), seeds the scheduler before any pass opens, and can rank satellites by priority from the very first simulated second — without waiting for a HELLO-ACK. The HELLO-ACK still happens and `update_inventory` is called, but it is a **synchronisation** step rather than a **discovery** step.

This simplification is explicitly consistent with the exercise framing, which states that the GS controls the schedule. It has two visible consequences in the protocol:

1. The HELLO message sends `pending` (what the GS still needs) rather than "what do you have?" — only unambiguous because the GS already knows the complete chunk space for each file.
2. The `-1` compact encoding in the pending list is safe because the GS knows `total_chunks` and the satellite can expand it using its own file records — both sides share the same ground truth from the config.

---

## Known Spec Ambiguity

The pass schedule field is named `bandwidth_gbps` and its JSON Schema description reads *"Gigabits per second"*. The illustrative example, however, uses a value of `1` and states "Total Capacity: 10 GB" over a 10-second window — arithmetic that is only consistent with **GB/s** (gigabytes per second), not Gbps.

This implementation treats `bandwidth_gbps` as **Gbps** (gigabits per second), trusting the field name and JSON Schema over the example. Gbps is also the physically realistic unit for a satellite radio downlink. The example text is considered to be in error. Transfer time is therefore computed as `(chunk_size_GB × 8) / bandwidth_gbps`.
