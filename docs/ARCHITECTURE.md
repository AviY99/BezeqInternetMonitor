# Current architecture

```text
Android phone
  |
  +-- Termux
       |
       +-- monitor_v5.py
       |    +-- UPnP/SOAP -> Bezeq router
       |    +-- ping -> router/public targets
       |    +-- HTTPS connectivity check
       |    +-- Termux:API -> Wi-Fi radio data
       |    +-- SQLite -> internet_monitor.db
       |
       +-- dashboard_v5_rooms.py
            +-- Flask -> 127.0.0.1:8080
            +-- reads SQLite
            +-- confirmed-event logic
            +-- source qualification
            +-- room testing
            +-- TEST MODE coordination
```

## Native Android replacement

```text
Android app
  |
  +-- Foreground monitoring service
  |    +-- router/WAN probe
  |    +-- local latency probe
  |    +-- public network probes
  |    +-- Android Wi-Fi APIs
  |    +-- health/diagnosis engine
  |
  +-- Room database
  |
  +-- Repository/domain layer
  |
  +-- Jetpack Compose UI
       +-- dashboard
       +-- history/events
       +-- source qualification
       +-- room tests
```

The native app should not run Flask and should not require Termux or Termux:API.
