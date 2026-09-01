# Native Android port

This directory is the native rewrite of the stable Termux/Web reference in the repository root.

## Milestone 0

The initial commit establishes:

- Android application module and package `com.aviy99.bezeqinternetmonitor`.
- `compileSdk 37`.
- Jetpack Compose BOM `2026.08.00`.
- Native monitor data models.
- First pure-Kotlin port of the health/diagnosis engine.
- Unit tests for high-priority diagnosis ordering.
- Placeholder dashboard proving the native UI shell.

The stable Python implementation remains authoritative while parity is built.

## Port order

1. Wi-Fi radio adapter using Android APIs.
2. Local-router and public-target probe abstraction.
3. HTTP connectivity adapter.
4. Continuous sampling repository.
5. Foreground monitoring service.
6. Local persistent database.
7. Full dashboard/history/events UI.
8. Bezeq UPnP/SOAP WAN status and counters.
9. TEST MODE.
10. Source Qualification.
11. Room-by-room testing.
12. Release signing and APK update path.

## Important parity rule

Do not tune thresholds merely to make Android results look better. First reproduce the stable reference behavior with captured/golden inputs; then calibrate platform-specific probes explicitly.
