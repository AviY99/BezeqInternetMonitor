# Bezeq Internet Monitor

Stable Termux + local-web prototype used as the reference implementation for the native Android APK.

## Stable snapshot

**Reference version:** `termux-web-v0.5-stable`

This snapshot includes:

- Continuous monitoring of the Bezeq router and Internet path.
- Bezeq WAN status and uptime through UPnP/SOAP.
- WAN byte counters and estimated current traffic.
- Router latency, jitter and packet loss.
- Cloudflare, Google and Quad9 public probes.
- HTTP connectivity check.
- Wi-Fi RSSI, band, channel and negotiated link speed through Termux:API.
- Rolling health score and diagnosis.
- SQLite history.
- Confirmed-event detection.
- Wi-Fi room testing.
- TEST MODE so active room tests do not contaminate health history/events.
- Source Qualification gate: room testing is enabled only when the source near the router is stable.

## Reference hardware/network assumptions

The prototype was developed against a Bezeq router reachable at:

- Router LAN IP: `10.0.0.138`
- UPnP HTTP port: `49152`
- WAN PPP control: `/417a6a61/upnp/control/WANPPPConn1`
- WAN Common Interface control: `/417a6a61/upnp/control/WANCommonIFC1`

These paths are router-model/firmware specific and must become discoverable/configurable in the Android port.

## Run on Termux

Runtime files are expected as:

```text
~/monitor_v5.py
~/dashboard_v5_rooms.py
~/internet_monitor.db
```

Install the runtime dependencies:

```bash
pkg install python termux-api
pip install flask
```

The separate Android **Termux:API companion app** is also required and must match the Termux signing/source family.

Start the monitor:

```bash
nohup python ~/monitor_v5.py > ~/monitor_v5.log 2>&1 &
```

Start the local dashboard:

```bash
python ~/dashboard_v5_rooms.py
```

Open:

```text
http://127.0.0.1:8080
```

## Project layout

- `termux/monitor_v5.py` — stable continuous monitor.
- `web/dashboard_v5_rooms.py` — stable Flask dashboard, source qualification and room tests.
- `docs/ARCHITECTURE.md` — current architecture.
- `docs/ALGORITHMS.md` — reference decision logic.
- `docs/ANDROID_PORT.md` — native Android port contract.
- `docs/STABLE_STATE.md` — exact current-state handoff.
- `docs/LEGACY_HISTORY.md` — complete retained Termux/Web version history and archive map.
- `archive/termux-web-history/` — retained historical source/launcher artifacts.
- `scripts/start_termux.sh` — convenience launcher.
- `requirements.txt` — Python package requirements.

## Native Android direction

The Android app should reproduce the behavior of this stable snapshot, not embed Termux. The Python implementation is the behavioral reference while Kotlin/Android components replace Termux, Flask and shell commands.
