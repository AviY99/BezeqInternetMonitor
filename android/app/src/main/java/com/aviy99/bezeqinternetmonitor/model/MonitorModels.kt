package com.aviy99.bezeqinternetmonitor.model

enum class ConnectionState {
    STABLE,
    DEGRADED,
    CRITICAL
}

enum class PublicTargetState {
    OK,
    LOSS,
    JITTER,
    SLOW,
    BAD,
    DOWN
}

data class WifiRadioSnapshot(
    val rssiDbm: Int? = null,
    val frequencyMhz: Int? = null,
    val band: String = "Unknown",
    val channel: Int? = null,
    val linkSpeedMbps: Int? = null
)

data class ProbeResult(
    val latencyMs: Double? = null,
    val jitterMs: Double? = null,
    val lossPercent: Double = 100.0
)

data class PublicTargetResult(
    val name: String,
    val probe: ProbeResult,
    val state: PublicTargetState
)

data class MonitorSnapshot(
    val timestampEpochMillis: Long,
    val wanConnected: Boolean,
    val wanUptimeSeconds: Long? = null,
    val router: ProbeResult,
    val publicTargets: List<PublicTargetResult>,
    val httpOk: Boolean,
    val httpLatencyMs: Double? = null,
    val wifi: WifiRadioSnapshot = WifiRadioSnapshot()
)

data class HealthAssessment(
    val score: Int,
    val state: ConnectionState,
    val diagnosis: String,
    val confidencePercent: Int
)
