# Native Android port contract

## Goal

Build an installable Android APK that reproduces the stable Termux/web behavior while removing the runtime dependency on Termux, Python and Flask.

## Proposed stack

- Kotlin
- Jetpack Compose
- Foreground Service for continuous monitoring
- Room database
- Coroutines / Flow
- Android `WifiManager` / `ConnectivityManager`
- OkHttp or platform HTTP client for HTTP probes
- XML/SOAP over HTTP for Bezeq UPnP status/counters

## Functional parity checklist

### Dashboard
- Health score/status
- Diagnosis/confidence
- WAN status and uptime
- traffic estimate
- router ping/jitter/loss
- aggregate Internet ping/jitter/loss
- Cloudflare/Google/Quad9
- HTTP connectivity
- RSSI/band/channel/link speed
- 24-hour summary
- history graph
- confirmed events

### Monitoring
- persistent background monitoring
- survives UI navigation
- visible foreground-service notification while active
- restart/resume policy after process/device restart
- battery-optimization guidance

### Diagnostics
- TEST MODE
- Source Qualification
- 6 source-stability rounds
- SOURCE READY / LOCAL SOURCE UNSTABLE / WAN ISP UNSTABLE / REFERENCE SIGNAL TOO WEAK
- room tests with names
- 3-round median
- baseline comparison
- room-test history

## Android-specific design decisions

### Wi-Fi information

Use Android APIs instead of `termux-wifi-connectioninfo`.

Persist only fields needed by product logic. Avoid storing BSSID/MAC by default.

### ICMP / latency

The Python prototype uses the system `ping` command. Native Android should encapsulate latency probing behind an interface so the implementation can evolve without changing the diagnosis engine.

Candidate implementations can use an appropriate combination of:

- reachability/ICMP where available
- TCP connect latency
- HTTP request latency

The UI should describe the measurement accurately instead of calling a TCP/HTTP probe “ping” if it is not ICMP.

### Bezeq UPnP

The first Android build can use the known router endpoints for parity, but the long-term implementation should discover the device description/service control URLs dynamically.

### Database migration

Room entities should be versioned so later APK updates can migrate the database without deleting history.

## Suggested first APK milestone

1. Native dashboard shell.
2. Native Wi-Fi radio metrics.
3. Native background sampling + Room DB.
4. Router/local/public probes.
5. Port health/diagnosis logic.
6. UPnP WAN status/counters.
7. Event state machine.
8. Source Qualification + Room Test.
9. Package signed with a permanent release key.
