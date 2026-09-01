# Stable state handoff

## What is considered stable

The stable baseline is the combination of:

- `termux/monitor_v5.py`
- `web/dashboard_v5_rooms.py`

The application is currently a Termux-hosted continuous monitor plus a Flask dashboard on `127.0.0.1:8080`.

## Continuous monitor

The monitor samples approximately every 3 seconds and stores data in `internet_monitor.db`.

### Router/WAN

- SOAP `GetStatusInfo` on `WANPPPConnection:1`.
- Connection status.
- WAN uptime.
- Last connection error.
- Total bytes received/sent from `WANCommonInterfaceConfig:1`.
- Traffic-rate estimate from byte-counter deltas.

### Local network

- Router target: `10.0.0.138`.
- Router ping, jitter and packet loss.
- Six-sample rolling averages are used for the main local/network health metrics.

### Public Internet

Targets:

- Cloudflare `1.1.1.1`
- Google `8.8.8.8`
- Quad9 `9.9.9.9`

HTTP connectivity:

- `https://connectivitycheck.gstatic.com/generate_204`

### Wi-Fi radio

Read through `termux-wifi-connectioninfo`:

- RSSI
- frequency
- derived 2.4/5/6 GHz band
- derived channel
- negotiated link speed

SSID/BSSID/MAC are not required for the product logic and should not be persisted unless a future feature explicitly requires them.

## Dashboard/event system

Event confirmation:

- 3 consecutive bad samples to confirm an event.
- 3 consecutive good samples to recover.
- Same-type events separated by less than 120 seconds are merged.

The dashboard shows:

- current health score/status
- diagnosis/confidence
- WAN state/uptime
- router and Internet latency/jitter/loss
- public target health
- HTTP connectivity
- Wi-Fi radio data
- 24-hour summary
- health history
- confirmed events

## TEST MODE

Active room/source tests generate extra network traffic and therefore pause the continuous background monitor.

Protocol:

1. Dashboard creates `~/internet_monitor_test_mode.json`.
2. Continuous monitor detects it, acknowledges with `~/internet_monitor_test_mode_ack.json`, and pauses health sampling.
3. A sample already in progress is discarded if TEST MODE begins before it is committed.
4. Dashboard waits for acknowledgment before active testing starts.
5. Room/source testing runs.
6. A short cooldown is applied.
7. TEST MODE is removed and continuous monitoring resumes automatically.
8. TEST MODE also expires defensively if stale.

This prevents active diagnostic traffic from creating artificial Health Score changes or Confirmed Events.

## Source Qualification

Room testing is invalid until the near-router source is proven stable.

The qualification currently runs **6 repeated stability samples** while the phone remains about 1–2 meters from the router.

Possible results:

- `SOURCE_READY`
- `LOCAL_SOURCE_UNSTABLE`
- `WAN_ISP_UNSTABLE`
- `REFERENCE_SIGNAL_TOO_WEAK`

Only `SOURCE_READY` saves a new valid baseline and unlocks Room Test.

### Reference-signal gate

Fails when:

- RSSI is unavailable, or
- median RSSI <= -72 dBm, or
- RSSI range > 10 dB.

### Local-source gate

Fails when any important local condition is bad, including:

- local packet loss > 0 in any qualification round
- router ping median > 20 ms
- router ping P90 > 30 ms
- router ping spread > 18 ms
- router jitter median > 10 ms
- router jitter P90 > 18 ms

### WAN/ISP gate

After the local source passes, WAN is considered unstable when conditions such as these occur:

- fewer than 5 of 6 rounds have at least two healthy public targets plus HTTP
- HTTP success < 85%
- public-target loss reaches >= 20%
- public jitter median > 30 ms
- public jitter P90 > 50 ms

Thresholds are initial reference values and can be recalibrated from real-world Android data.

## Room Test

After `SOURCE_READY`:

- User enters a room name.
- Test runs 3 rounds.
- Median values are used.
- The result is compared to the qualified near-router baseline.

Comparison considers:

- RSSI delta
- router ping delta
- router jitter delta
- router packet loss
- link-speed delta
- Wi-Fi band change

The room receives a result such as `GOOD`, `WEAKER`, or `POOR`.

## Important product rule

A bad room result is meaningful only when the source is stable. If the source itself becomes unstable, room comparisons must be locked until Source Qualification succeeds again.
