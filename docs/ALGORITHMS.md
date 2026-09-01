# Reference algorithms

This document describes the behavior that the native Android app should match before it is tuned further.

## Continuous health diagnosis

The monitor distinguishes these broad classes:

1. WAN disconnected.
2. Local Wi-Fi/LAN failure or loss.
3. Local latency/jitter.
4. Wi-Fi weak-signal or likely congestion/interference.
5. Multi-target Internet/WAN instability.
6. Route-specific failure.
7. HTTP/DNS/connectivity issue.
8. Stable connection.

The diagnosis uses router measurements first so a local fault is not incorrectly blamed on the ISP.

## Public-target classification

A public target can be classified as:

- `DOWN`
- `BAD`
- `LOSS`
- `JITTER`
- `SLOW`
- `OK`

Multiple targets are used so a single ICMP path cannot automatically become a global Internet failure.

## Rolling stability

Main router and aggregate-Internet values use a rolling window of 6 samples.

This smooths one-off spikes but does not replace the confirmed-event state machine.

## Confirmed events

The dashboard applies:

- `START_CONFIRM = 3`
- `RECOVERY_CONFIRM = 3`
- `MERGE_GAP_SECONDS = 120`

This prevents a single transient sample from creating an incident.

## Source Qualification statistics

Six near-router rounds are collected.

For important series, the current implementation uses:

- median
- P90
- range/spread
- maximum loss
- HTTP success rate
- count of rounds with >= 2 healthy public targets + HTTP

This is deliberately focused on **variation over time**, not just averages.

## Room comparison

Room measurements use three independent rounds and median aggregation.

Initial comparison thresholds include:

- RSSI drop >= 8 dB: warning
- RSSI drop >= 15 dB: poor
- router-ping increase >= 10 ms: warning
- router-ping increase >= 25 ms: poor
- router-jitter increase >= 8 ms: warning
- router-jitter increase >= 20 ms: poor
- router loss >= 5%: poor
- link-speed drop >= 80 Mbps: warning

These thresholds are a starting point, not universal RF standards.
