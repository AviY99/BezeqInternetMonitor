# Termux / Web development history

This directory preserves the historical source artifacts that were produced during development of the Termux + Flask prototype.

The **authoritative stable reference is not the latest filename numerically**. It is:

- `../../termux/monitor_v5.py`
- `../../web/dashboard_v5_rooms.py`

Those stable files correspond to the Source Qualification + TEST MODE design documented in `../../docs/STABLE_STATE.md`.

## Why preserve these files?

The native Android port is a rewrite. Keeping the earlier implementations makes it possible to:

- trace why an algorithm or UI decision was introduced;
- compare behavior when porting logic to Kotlin;
- recover an earlier implementation if a regression is found;
- distinguish experiments from the accepted product flow.

## Version lineage

```text
monitor v2
  -> v3 multi-target/HTTP
  -> v4 per-target metrics
  -> v5 Wi-Fi radio diagnostics
       -> v5 TEST MODE
       -> v5 Source Qualification (stable reference)

dashboard v2
  -> v3 event timeline
  -> v4 confirmed events + 24h summary
  -> v5 Wi-Fi diagnostics
       -> V6 active-test experiment
       -> V7 room-test experiment
       -> back to clean V5 + Rooms
            -> 3-round median
            -> live progress
            -> TEST MODE
            -> Source Qualification (stable reference)
```

## Archived artifacts

| File | Role / historical significance |
|---|---|
| `monitor_v2.py` | Early SQLite-based monitor with score/history; predecessor to multi-target diagnostics. |
| `monitor_v3.py` | Added multi-target public probing and HTTP connectivity/diagnosis concepts. |
| `monitor_v4.py` | Added per-target jitter/loss fields and the database schema used by later dashboards. |
| `monitor_v5.py` | Wi-Fi-aware continuous monitor: RSSI/frequency/band/channel/link speed plus Wi-Fi-aware diagnosis. |
| `monitor_v5_testmode.py` | V5 monitor with coordinated TEST MODE pause/acknowledgement so active tests do not contaminate health history. |
| `monitor_v5_source_qualification.py` | Stable monitor side used with Source Qualification; behavior is the TEST MODE monitor reference. |
| `dashboard_v2.py` | Dashboard generation with public targets, HTTP connectivity, diagnosis and confidence. |
| `dashboard_v3.py` | Added per-target Ping/Jitter/Loss and event timeline. |
| `dashboard_v4.py` | Introduced confirmed events: 3 bad samples to confirm, 3 good to recover, merge gap, 24h summary. |
| `dashboard_v5.py` | Wi-Fi diagnostics dashboard paired with monitor V5. |
| `dashboard_v5_rooms.py` | V5 dashboard plus guided near-router baseline and room-by-room comparison. |
| `dashboard_v5_rooms_3x.py` | Room tests changed from a single measurement to 3 rounds using medians. |
| `dashboard_v5_rooms_progress.py` | Added live progress reporting for baseline/room tests without changing the 3-round comparison math. |
| `dashboard_v5_rooms_testmode.py` | Added coordinated TEST MODE around active room tests. |
| `dashboard_v5_source_qualification.py` | Stable dashboard: 6-round Source Qualification gates room testing; only SOURCE_READY saves/unlocks a baseline. |
| `dashboard_v6.py` | Experimental one-shot active Wi-Fi test button; later intentionally removed from product flow. |
| `dashboard_v7.py` | Experimental room-test integration built on V6; superseded by the cleaner V5 + Rooms line. |
| `Start_Internet_Monitor.sh` | Early Termux launcher that starts monitor/dashboard and opens the local browser. |
| `Stop_Internet_Monitor.sh` | Stops the Termux monitor/dashboard and releases wake lock. |
| `Start_Internet_Monitor_V6.sh` | Launcher targeting the experimental V6 dashboard. |
| `Start_Internet_Monitor_V7.sh` | Launcher targeting the experimental V7 dashboard. |
| `Start_V5_With_Rooms.sh` | Launcher for the cleaner V5 + Rooms branch. |

## Earlier prototype not preserved as a standalone file

Before `monitor_v2.py`, an initial hand-edited `monitor.py` prototype was used to validate Bezeq UPnP/SOAP access and router/public latency checks. That exact file is not present in the retained artifact set, so it is **not reconstructed or presented as original source**.

Its known validated behavior is documented in the project docs:

- router `10.0.0.138`;
- UPnP device description on port `49152`;
- `WANPPPConnection:1` status;
- `WANCommonInterfaceConfig:1` byte counters;
- router and Internet latency checks.

This note exists so the historical record is explicit about the gap rather than silently pretending the first prototype was archived.

## Runtime files intentionally not versioned

The following are runtime state, not source artifacts, and are deliberately excluded from Git:

- `internet_monitor.db`, `-wal`, `-shm`
- monitor/dashboard `.log` files
- TEST MODE JSON flag/ack files
- Python `__pycache__` / `.pyc`

They are described in the stable-state documentation and `.gitignore`.

## Integrity manifest

`ARCHIVE_MANIFEST.json` records size and SHA-256 for every retained historical artifact.
