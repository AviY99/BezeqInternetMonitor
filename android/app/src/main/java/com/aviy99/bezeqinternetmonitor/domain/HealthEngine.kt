package com.aviy99.bezeqinternetmonitor.domain

import com.aviy99.bezeqinternetmonitor.model.ConnectionState
import com.aviy99.bezeqinternetmonitor.model.HealthAssessment
import com.aviy99.bezeqinternetmonitor.model.MonitorSnapshot
import com.aviy99.bezeqinternetmonitor.model.PublicTargetState

/**
 * First native port of the stable Python diagnosis rules.
 *
 * The Python implementation in /termux remains the behavioral reference.
 * Thresholds stay explicit here so parity can be unit-tested before tuning.
 */
object HealthEngine {

    fun assess(snapshot: MonitorSnapshot): HealthAssessment {
        val diagnosis = diagnose(snapshot)
        val score = score(snapshot)
        val state = when {
            score < 30 -> ConnectionState.CRITICAL
            score < 75 -> ConnectionState.DEGRADED
            else -> ConnectionState.STABLE
        }

        return HealthAssessment(
            score = score,
            state = state,
            diagnosis = diagnosis.first,
            confidencePercent = diagnosis.second
        )
    }

    private fun diagnose(snapshot: MonitorSnapshot): Pair<String, Int> {
        if (!snapshot.wanConnected) {
            return "WAN disconnected" to 100
        }

        val router = snapshot.router
        val rssi = snapshot.wifi.rssiDbm
        val band = snapshot.wifi.band

        if (router.latencyMs == null || router.lossPercent >= 20.0) {
            return if (rssi != null && rssi <= -75) {
                "Weak Wi-Fi signal causing local network problems" to 99
            } else {
                "Local Wi-Fi/LAN problem" to 98
            }
        }

        if (router.lossPercent >= 5.0) {
            return if (rssi != null && rssi <= -72) {
                "Wi-Fi packet loss with weak/fair signal" to 98
            } else {
                "Local Wi-Fi/LAN packet loss" to 95
            }
        }

        if (
            rssi != null &&
            rssi <= -75 &&
            (
                router.latencyMs >= 20.0 ||
                (router.jitterMs != null && router.jitterMs >= 15.0)
            )
        ) {
            return "Weak Wi-Fi signal is degrading the local connection" to 98
        }

        if (
            rssi != null &&
            rssi in -74..-66 &&
            (
                router.latencyMs >= 25.0 ||
                (router.jitterMs != null && router.jitterMs >= 20.0)
            )
        ) {
            return "Fair Wi-Fi signal with elevated local latency/jitter" to 94
        }

        if (
            band == "2.4 GHz" &&
            router.latencyMs >= 25.0 &&
            router.jitterMs != null &&
            router.jitterMs >= 20.0
        ) {
            return "Likely 2.4 GHz Wi-Fi congestion/interference" to 93
        }

        if (
            router.latencyMs >= 45.0 ||
            (router.jitterMs != null && router.jitterMs >= 25.0)
        ) {
            return if (rssi != null && rssi >= -65) {
                "Wi-Fi congestion/interference despite good signal" to 92
            } else {
                "Local Wi-Fi/LAN latency is high" to 90
            }
        }

        val bad = snapshot.publicTargets.filter {
            it.state == PublicTargetState.DOWN ||
                it.state == PublicTargetState.BAD
        }

        val degraded = snapshot.publicTargets.filter {
            it.state == PublicTargetState.LOSS ||
                it.state == PublicTargetState.JITTER ||
                it.state == PublicTargetState.SLOW
        }

        val healthy = snapshot.publicTargets.filter {
            it.state == PublicTargetState.OK
        }

        if (bad.size >= 2 && !snapshot.httpOk) {
            return "WAN / ISP instability" to 97
        }

        if (bad.size >= 2) {
            return "Internet path instability" to 92
        }

        if (bad.size == 1 && healthy.isNotEmpty() && snapshot.httpOk) {
            return "Route to ${bad.first().name} is unstable" to 88
        }

        if (!snapshot.httpOk && healthy.size >= 2) {
            return "HTTP/DNS connectivity issue" to 85
        }

        if (degraded.size >= 2) {
            return "Internet latency/jitter is elevated" to 82
        }

        if (degraded.size == 1 && healthy.size >= 2) {
            return "Minor issue on ${degraded.first().name} path" to 76
        }

        if (router.latencyMs >= 25.0) {
            return if (band == "2.4 GHz") {
                "Internet OK; 2.4 GHz Wi-Fi latency elevated" to 84
            } else {
                "Internet OK; local Wi-Fi latency elevated" to 80
            }
        }

        return "Connection looks stable" to 94
    }

    /**
     * Initial native score port.
     *
     * This is deliberately isolated because the Python score is heuristic.
     * We will add golden-data parity tests before changing thresholds.
     */
    private fun score(snapshot: MonitorSnapshot): Int {
        if (!snapshot.wanConnected) return 0

        var result = 100

        val router = snapshot.router
        result -= when {
            router.latencyMs == null -> 45
            router.latencyMs >= 80 -> 30
            router.latencyMs >= 45 -> 20
            router.latencyMs >= 25 -> 10
            else -> 0
        }

        result -= when {
            router.lossPercent >= 20 -> 35
            router.lossPercent >= 5 -> 20
            router.lossPercent > 0 -> 8
            else -> 0
        }

        result -= when {
            router.jitterMs == null -> 0
            router.jitterMs >= 40 -> 18
            router.jitterMs >= 25 -> 12
            router.jitterMs >= 15 -> 6
            else -> 0
        }

        val targetPenalty = snapshot.publicTargets.sumOf {
            when (it.state) {
                PublicTargetState.DOWN -> 12
                PublicTargetState.BAD -> 10
                PublicTargetState.LOSS -> 6
                PublicTargetState.JITTER -> 5
                PublicTargetState.SLOW -> 4
                PublicTargetState.OK -> 0
            }
        }

        result -= targetPenalty.coerceAtMost(25)

        if (!snapshot.httpOk) {
            result -= 15
        }

        return result.coerceIn(0, 100)
    }
}
