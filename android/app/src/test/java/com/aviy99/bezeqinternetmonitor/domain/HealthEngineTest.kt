package com.aviy99.bezeqinternetmonitor.domain

import com.aviy99.bezeqinternetmonitor.model.MonitorSnapshot
import com.aviy99.bezeqinternetmonitor.model.ProbeResult
import com.aviy99.bezeqinternetmonitor.model.PublicTargetResult
import com.aviy99.bezeqinternetmonitor.model.PublicTargetState
import com.aviy99.bezeqinternetmonitor.model.WifiRadioSnapshot
import org.junit.Assert.assertEquals
import org.junit.Test

class HealthEngineTest {

    @Test
    fun wanDisconnectedWins() {
        val assessment = HealthEngine.assess(
            stableSnapshot().copy(
                wanConnected = false
            )
        )

        assertEquals(
            "WAN disconnected",
            assessment.diagnosis
        )
        assertEquals(0, assessment.score)
    }

    @Test
    fun elevatedLocalLatencyOn24GhzIsDiagnosedLocally() {
        val assessment = HealthEngine.assess(
            stableSnapshot().copy(
                router = ProbeResult(
                    latencyMs = 31.0,
                    jitterMs = 12.0,
                    lossPercent = 0.0
                ),
                wifi = WifiRadioSnapshot(
                    rssiDbm = -63,
                    frequencyMhz = 2437,
                    band = "2.4 GHz",
                    channel = 6,
                    linkSpeedMbps = 100
                )
            )
        )

        assertEquals(
            "Internet OK; 2.4 GHz Wi-Fi latency elevated",
            assessment.diagnosis
        )
    }

    private fun stableSnapshot() = MonitorSnapshot(
        timestampEpochMillis = 0L,
        wanConnected = true,
        wanUptimeSeconds = 1000,
        router = ProbeResult(
            latencyMs = 8.0,
            jitterMs = 2.0,
            lossPercent = 0.0
        ),
        publicTargets = listOf(
            PublicTargetResult(
                "Cloudflare",
                ProbeResult(15.0, 2.0, 0.0),
                PublicTargetState.OK
            ),
            PublicTargetResult(
                "Google",
                ProbeResult(16.0, 2.0, 0.0),
                PublicTargetState.OK
            ),
            PublicTargetResult(
                "Quad9",
                ProbeResult(17.0, 3.0, 0.0),
                PublicTargetState.OK
            )
        ),
        httpOk = true,
        httpLatencyMs = 80.0,
        wifi = WifiRadioSnapshot(
            rssiDbm = -55,
            frequencyMhz = 5180,
            band = "5 GHz",
            channel = 36,
            linkSpeedMbps = 433
        )
    )
}
