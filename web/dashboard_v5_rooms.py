from flask import Flask, jsonify, Response, request, stream_with_context
import sqlite3
import subprocess
import json
import re
import statistics
import os
import time
import uuid
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

app = Flask(__name__)
DB = "internet_monitor.db"

TEST_MODE_FILE = os.path.expanduser(
    "~/internet_monitor_test_mode.json"
)
TEST_MODE_ACK_FILE = os.path.expanduser(
    "~/internet_monitor_test_mode_ack.json"
)


def read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def current_test_mode():
    data = read_json_file(TEST_MODE_FILE)

    if not data:
        return None

    try:
        expires_at = float(data.get("expires_at", 0))
    except Exception:
        expires_at = 0

    if expires_at <= time.time():
        clear_stale_test_mode()
        return None

    return data


def clear_stale_test_mode():
    for path in (TEST_MODE_FILE, TEST_MODE_ACK_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception:
            pass


def begin_test_mode(role, label):
    if current_test_mode():
        return None

    token = uuid.uuid4().hex

    payload = {
        "token": token,
        "role": role,
        "label": label,
        "started_at": time.time(),
        "expires_at": time.time() + 240
    }

    temp_path = TEST_MODE_FILE + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    os.replace(temp_path, TEST_MODE_FILE)

    try:
        os.remove(TEST_MODE_ACK_FILE)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    return token


def end_test_mode(token):
    data = read_json_file(TEST_MODE_FILE)

    if data and data.get("token") != token:
        return

    clear_stale_test_mode()


def monitor_process_running():
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python .*monitor_v5.py"],
            capture_output=True,
            text=True,
            timeout=3
        )
        return result.returncode == 0
    except Exception:
        return False


def monitor_acknowledged(token):
    ack = read_json_file(TEST_MODE_ACK_FILE)
    return bool(
        ack and ack.get("token") == token
    )



START_CONFIRM = 3
RECOVERY_CONFIRM = 3
MERGE_GAP_SECONDS = 120


def get_connection():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def get_rows(limit=300):
    con = get_connection()
    try:
        rows = con.execute(
            """
            SELECT *
            FROM measurements
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        con.close()


def get_24h_rows():
    cutoff = (
        datetime.now() - timedelta(hours=24)
    ).strftime("%Y-%m-%d %H:%M:%S")

    con = get_connection()
    try:
        rows = con.execute(
            """
            SELECT *
            FROM measurements
            WHERE timestamp >= ?
            ORDER BY id ASC
            """,
            (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def parse_ts(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def issue_for(row):
    score = row.get("score")
    diagnosis = row.get("diagnosis") or ""
    wan = row.get("wan_status")

    if wan != "Connected":
        return "WAN", "WAN disconnected"

    if score is not None and score < 30:
        return "CRITICAL", diagnosis or "Critical connection degradation"

    if "Weak Wi-Fi signal" in diagnosis or "weak/fair signal" in diagnosis:
        return "WIFI_SIGNAL", diagnosis

    if "2.4 GHz" in diagnosis or "congestion/interference" in diagnosis:
        return "WIFI_CONGESTION", diagnosis

    if "packet loss" in diagnosis and ("Wi-Fi" in diagnosis or "LAN" in diagnosis):
        return "LOCAL_LOSS", diagnosis

    if "Wi-Fi" in diagnosis or "LAN" in diagnosis:
        return "LOCAL_LATENCY", diagnosis

    if "WAN / ISP" in diagnosis:
        return "ISP", diagnosis

    if "HTTP/DNS" in diagnosis:
        return "HTTP", diagnosis

    if "Route to" in diagnosis:
        return "ROUTE", diagnosis

    if score is not None and score < 75:
        return "DEGRADED", diagnosis or "Connection degraded"

    return None


def raw_confirmed_events(rows):
    events = []

    active = None
    pending_issue = None
    pending_count = 0
    recovery_count = 0

    for row in rows:
        ts_text = row.get("timestamp")
        ts = parse_ts(ts_text)

        if ts is None:
            continue

        issue = issue_for(row)

        if active is None:
            if issue is None:
                pending_issue = None
                pending_count = 0
                continue

            if pending_issue and pending_issue[0] == issue[0]:
                pending_count += 1
            else:
                pending_issue = issue
                pending_count = 1

            if pending_count >= START_CONFIRM:
                active = {
                    "type": issue[0],
                    "description": issue[1],
                    "started_at": ts_text,
                    "ended_at": None,
                    "duration_sec": 0,
                    "min_score": row.get("score"),
                    "active": True,
                }

                pending_issue = None
                pending_count = 0
                recovery_count = 0

            continue

        # Event is active.
        if issue is not None and issue[0] == active["type"]:
            recovery_count = 0
            active["description"] = issue[1]

            score = row.get("score")
            if score is not None:
                if active["min_score"] is None:
                    active["min_score"] = score
                else:
                    active["min_score"] = min(
                        active["min_score"],
                        score
                    )

            continue

        if issue is not None and issue[0] != active["type"]:
            # Different problem: do not instantly close; require confirmation.
            recovery_count += 1
        else:
            recovery_count += 1

        if recovery_count >= RECOVERY_CONFIRM:
            active["ended_at"] = ts_text
            start = parse_ts(active["started_at"])

            if start:
                active["duration_sec"] = max(
                    0,
                    int((ts - start).total_seconds())
                )

            active["active"] = False
            events.append(active)
            active = None
            recovery_count = 0

            # If current row is already another issue, let future rows confirm it.
            if issue is not None:
                pending_issue = issue
                pending_count = 1
            else:
                pending_issue = None
                pending_count = 0

    if active is not None:
        now = datetime.now()
        start = parse_ts(active["started_at"])

        if start:
            active["duration_sec"] = max(
                0,
                int((now - start).total_seconds())
            )

        active["active"] = True
        events.append(active)

    return events


def merge_events(events):
    if not events:
        return []

    merged = []

    for event in events:
        if not merged:
            merged.append(event)
            continue

        prev = merged[-1]

        if prev["type"] != event["type"]:
            merged.append(event)
            continue

        prev_end = parse_ts(prev.get("ended_at"))
        event_start = parse_ts(event.get("started_at"))

        if prev_end is None or event_start is None:
            merged.append(event)
            continue

        gap = (event_start - prev_end).total_seconds()

        if gap > MERGE_GAP_SECONDS:
            merged.append(event)
            continue

        # Merge short gaps of the same problem into one useful incident.
        prev["ended_at"] = event.get("ended_at")
        prev["active"] = event.get("active", False)
        prev["description"] = event.get("description") or prev["description"]

        if event.get("min_score") is not None:
            if prev.get("min_score") is None:
                prev["min_score"] = event["min_score"]
            else:
                prev["min_score"] = min(
                    prev["min_score"],
                    event["min_score"]
                )

        start = parse_ts(prev["started_at"])
        end = (
            datetime.now()
            if prev["active"]
            else parse_ts(prev.get("ended_at"))
        )

        if start and end:
            prev["duration_sec"] = max(
                0,
                int((end - start).total_seconds())
            )

    return merged


def build_events():
    rows = get_24h_rows()
    events = raw_confirmed_events(rows)
    events = merge_events(events)
    return list(reversed(events[-20:]))


def build_summary():
    rows = get_24h_rows()

    if not rows:
        return {
            "samples": 0,
            "availability": None,
            "avg_score": None,
            "min_score": None,
            "avg_router_ping": None,
            "avg_net_ping": None,
            "avg_net_jitter": None,
            "avg_net_loss": None,
            "events": 0,
        }

    def values(key):
        return [
            float(r[key])
            for r in rows
            if r.get(key) is not None
        ]

    score_values = values("score")
    router_ping_values = values("router_ping")
    net_ping_values = values("net_ping")
    net_jitter_values = values("net_jitter")
    net_loss_values = values("net_loss")

    available = sum(
        1
        for r in rows
        if r.get("wan_status") == "Connected"
        and (r.get("score") is None or r.get("score") >= 30)
    )

    availability = 100.0 * available / len(rows)

    events = build_events()

    return {
        "samples": len(rows),
        "availability": availability,
        "avg_score": (
            sum(score_values) / len(score_values)
            if score_values else None
        ),
        "min_score": (
            min(score_values)
            if score_values else None
        ),
        "avg_router_ping": (
            sum(router_ping_values) / len(router_ping_values)
            if router_ping_values else None
        ),
        "avg_net_ping": (
            sum(net_ping_values) / len(net_ping_values)
            if net_ping_values else None
        ),
        "avg_net_jitter": (
            sum(net_jitter_values) / len(net_jitter_values)
            if net_jitter_values else None
        ),
        "avg_net_loss": (
            sum(net_loss_values) / len(net_loss_values)
            if net_loss_values else None
        ),
        "events": len(events),
    }


ROOM_ROUTER = "10.0.0.138"
ROOM_TARGETS = {
    "Cloudflare": "1.1.1.1",
    "Google": "8.8.8.8",
    "Quad9": "9.9.9.9",
}


def ensure_room_table():
    con = sqlite3.connect(DB, timeout=10)

    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS wifi_room_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                role TEXT NOT NULL,
                label TEXT NOT NULL,
                rssi REAL,
                frequency_mhz REAL,
                band TEXT,
                channel INTEGER,
                link_speed_mbps REAL,
                router_ping REAL,
                router_jitter REAL,
                router_loss REAL,
                public_ping REAL,
                public_jitter REAL,
                public_loss REAL
            )
        """)
        con.commit()

    finally:
        con.close()



def ensure_source_qualification_table():
    con = sqlite3.connect(DB, timeout=10)

    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS wifi_source_qualification (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                rounds INTEGER NOT NULL,
                rssi_median REAL,
                rssi_range REAL,
                router_ping_median REAL,
                router_ping_p90 REAL,
                router_ping_range REAL,
                router_jitter_median REAL,
                router_jitter_p90 REAL,
                router_loss_max REAL,
                public_ping_median REAL,
                public_ping_p90 REAL,
                public_jitter_median REAL,
                public_jitter_p90 REAL,
                public_loss_max REAL,
                http_success_rate REAL,
                healthy_public_rounds INTEGER
            )
        """)
        con.commit()

    finally:
        con.close()


def latest_source_qualification():
    ensure_source_qualification_table()

    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row

    try:
        row = con.execute("""
            SELECT *
            FROM wifi_source_qualification
            ORDER BY id DESC
            LIMIT 1
        """).fetchone()

        return dict(row) if row else None

    finally:
        con.close()


def save_source_qualification(result):
    ensure_source_qualification_table()

    con = sqlite3.connect(DB, timeout=10)

    try:
        con.execute("""
            INSERT INTO wifi_source_qualification (
                timestamp,
                status,
                summary,
                rounds,
                rssi_median,
                rssi_range,
                router_ping_median,
                router_ping_p90,
                router_ping_range,
                router_jitter_median,
                router_jitter_p90,
                router_loss_max,
                public_ping_median,
                public_ping_p90,
                public_jitter_median,
                public_jitter_p90,
                public_loss_max,
                http_success_rate,
                healthy_public_rounds
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result["timestamp"],
            result["status"],
            result["summary"],
            result["rounds"],
            result.get("rssi_median"),
            result.get("rssi_range"),
            result.get("router_ping_median"),
            result.get("router_ping_p90"),
            result.get("router_ping_range"),
            result.get("router_jitter_median"),
            result.get("router_jitter_p90"),
            result.get("router_loss_max"),
            result.get("public_ping_median"),
            result.get("public_ping_p90"),
            result.get("public_jitter_median"),
            result.get("public_jitter_p90"),
            result.get("public_loss_max"),
            result.get("http_success_rate"),
            result.get("healthy_public_rounds"),
        ))
        con.commit()

    finally:
        con.close()


def active_wifi_info():
    try:
        process = subprocess.run(
            ["termux-wifi-connectioninfo"],
            capture_output=True,
            text=True,
            timeout=8
        )

        if process.returncode != 0 or not process.stdout.strip():
            return {}

        return json.loads(process.stdout)

    except Exception:
        return {}


def active_ping(host, count):
    try:
        process = subprocess.run(
            ["ping", "-c", str(count), "-W", "2", host],
            capture_output=True,
            text=True,
            timeout=max(14, count * 3)
        )

        output = process.stdout + process.stderr

        times = [
            float(x)
            for x in re.findall(
                r'time[=<]([\d.]+)',
                output
            )
        ]

        loss_match = re.search(
            r'([\d.]+)% packet loss',
            output
        )

        loss = (
            float(loss_match.group(1))
            if loss_match else 100.0
        )

        if not times:
            return {
                "ping": None,
                "jitter": None,
                "loss": loss
            }

        avg = sum(times) / len(times)

        jitter = (
            sum(
                abs(times[i] - times[i - 1])
                for i in range(1, len(times))
            ) / (len(times) - 1)
            if len(times) > 1 else 0.0
        )

        return {
            "ping": avg,
            "jitter": jitter,
            "loss": loss
        }

    except Exception:
        return {
            "ping": None,
            "jitter": None,
            "loss": 100.0
        }


def room_band(frequency):
    try:
        f = int(frequency)
    except Exception:
        return "Unknown"

    if 2400 <= f < 2500:
        return "2.4 GHz"

    if 4900 <= f < 5900:
        return "5 GHz"

    if 5925 <= f <= 7125:
        return "6 GHz"

    return "Unknown"


def room_channel(frequency):
    try:
        f = int(frequency)
    except Exception:
        return None

    if f == 2484:
        return 14

    if 2412 <= f <= 2472:
        return int((f - 2407) / 5)

    if 5000 <= f <= 5900:
        return int((f - 5000) / 5)

    if 5955 <= f <= 7115:
        return int((f - 5950) / 5)

    return None


def median_value(values):
    clean = [
        value
        for value in values
        if value is not None
    ]

    if not clean:
        return None

    return float(statistics.median(clean))




SOURCE_QUALIFICATION_ROUNDS = 6


def percentile_value(values, percentile):
    clean = sorted(
        float(v)
        for v in values
        if v is not None
    )

    if not clean:
        return None

    if len(clean) == 1:
        return clean[0]

    position = (len(clean) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(clean) - 1)
    fraction = position - lower

    return (
        clean[lower] * (1 - fraction)
        + clean[upper] * fraction
    )


def range_value(values):
    clean = [
        float(v)
        for v in values
        if v is not None
    ]

    if not clean:
        return None

    return max(clean) - min(clean)


def max_value(values):
    clean = [
        float(v)
        for v in values
        if v is not None
    ]

    if not clean:
        return None

    return max(clean)


def active_http_check():
    start = time.monotonic()

    try:
        request_obj = urllib.request.Request(
            "https://connectivitycheck.gstatic.com/generate_204",
            headers={
                "User-Agent": "InternetMonitor/5"
            }
        )

        with urllib.request.urlopen(
            request_obj,
            timeout=6
        ) as response:
            status = int(
                getattr(response, "status", 0)
            )

        latency_ms = (
            time.monotonic() - start
        ) * 1000

        return {
            "ok": status in (200, 204),
            "latency": latency_ms
        }

    except Exception:
        return {
            "ok": False,
            "latency": None
        }


def perform_source_round():
    wifi = active_wifi_info()

    jobs = {
        "Router": (
            active_ping,
            ROOM_ROUTER,
            6
        ),
        "Cloudflare": (
            active_ping,
            ROOM_TARGETS["Cloudflare"],
            4
        ),
        "Google": (
            active_ping,
            ROOM_TARGETS["Google"],
            4
        ),
        "Quad9": (
            active_ping,
            ROOM_TARGETS["Quad9"],
            4
        ),
        "HTTP": (
            active_http_check,
        )
    }

    results = {}

    with ThreadPoolExecutor(
        max_workers=5
    ) as executor:
        futures = {}

        for name, job in jobs.items():
            fn = job[0]
            args = job[1:]

            futures[
                executor.submit(fn, *args)
            ] = name

        for future in as_completed(futures):
            name = futures[future]

            try:
                results[name] = future.result()
            except Exception:
                if name == "HTTP":
                    results[name] = {
                        "ok": False,
                        "latency": None
                    }
                else:
                    results[name] = {
                        "ping": None,
                        "jitter": None,
                        "loss": 100.0
                    }

    router = results["Router"]

    target_results = {
        name: results[name]
        for name in ROOM_TARGETS
    }

    healthy_targets = sum(
        1
        for result in target_results.values()
        if (
            result.get("ping") is not None
            and float(result.get("loss") or 0) < 20
        )
    )

    public_ping = median_value([
        result.get("ping")
        for result in target_results.values()
    ])

    public_jitter = median_value([
        result.get("jitter")
        for result in target_results.values()
    ])

    public_loss = median_value([
        result.get("loss")
        for result in target_results.values()
    ])

    return {
        "rssi": wifi.get("rssi"),
        "frequency_mhz": wifi.get(
            "frequency_mhz"
        ),
        "link_speed_mbps": wifi.get(
            "link_speed_mbps"
        ),
        "router_ping": router.get("ping"),
        "router_jitter": router.get(
            "jitter"
        ),
        "router_loss": router.get("loss"),
        "public_ping": public_ping,
        "public_jitter": public_jitter,
        "public_loss": public_loss,
        "healthy_targets": healthy_targets,
        "http_ok": bool(
            results["HTTP"].get("ok")
        ),
        "http_latency": results["HTTP"].get(
            "latency"
        ),
    }


def qualify_source(rounds):
    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    rssi_values = [
        r.get("rssi")
        for r in rounds
    ]

    router_ping_values = [
        r.get("router_ping")
        for r in rounds
    ]

    router_jitter_values = [
        r.get("router_jitter")
        for r in rounds
    ]

    router_loss_values = [
        r.get("router_loss")
        for r in rounds
    ]

    public_ping_values = [
        r.get("public_ping")
        for r in rounds
    ]

    public_jitter_values = [
        r.get("public_jitter")
        for r in rounds
    ]

    public_loss_values = [
        r.get("public_loss")
        for r in rounds
    ]

    rssi_median = median_value(
        rssi_values
    )
    rssi_range = range_value(
        rssi_values
    )

    router_ping_median = median_value(
        router_ping_values
    )
    router_ping_p90 = percentile_value(
        router_ping_values,
        0.90
    )
    router_ping_range = range_value(
        router_ping_values
    )

    router_jitter_median = median_value(
        router_jitter_values
    )
    router_jitter_p90 = percentile_value(
        router_jitter_values,
        0.90
    )

    router_loss_max = max_value(
        router_loss_values
    )

    public_ping_median = median_value(
        public_ping_values
    )
    public_ping_p90 = percentile_value(
        public_ping_values,
        0.90
    )

    public_jitter_median = median_value(
        public_jitter_values
    )
    public_jitter_p90 = percentile_value(
        public_jitter_values,
        0.90
    )

    public_loss_max = max_value(
        public_loss_values
    )

    http_successes = sum(
        1
        for r in rounds
        if r.get("http_ok")
    )

    http_success_rate = (
        (http_successes / len(rounds)) * 100
        if rounds else 0
    )

    healthy_public_rounds = sum(
        1
        for r in rounds
        if (
            int(r.get("healthy_targets") or 0) >= 2
            and r.get("http_ok")
        )
    )

    # First gate: is the reference radio signal itself acceptable?
    weak_reference = (
        rssi_median is None
        or rssi_median <= -72
        or (
            rssi_range is not None
            and rssi_range > 10
        )
    )

    if weak_reference:
        summary_parts = []

        if rssi_median is None:
            summary_parts.append(
                "RSSI could not be measured"
            )
        else:
            summary_parts.append(
                f"median RSSI is {rssi_median:.0f} dBm"
            )

        if (
            rssi_range is not None
            and rssi_range > 10
        ):
            summary_parts.append(
                f"RSSI varied by {rssi_range:.0f} dB"
            )

        status = "REFERENCE_SIGNAL_TOO_WEAK"
        summary = (
            "Reference Wi-Fi signal is not good enough "
            "for a reliable room baseline: "
            + "; ".join(summary_parts)
            + ". Move closer to the router and retry."
        )

    else:
        # Second gate: local source must be stable before testing rooms.
        local_unstable = any([
            router_ping_median is None,
            router_ping_p90 is None,
            router_jitter_median is None,
            (
                router_loss_max is not None
                and router_loss_max > 0
            ),
            (
                router_ping_median is not None
                and router_ping_median > 20
            ),
            (
                router_ping_p90 is not None
                and router_ping_p90 > 30
            ),
            (
                router_ping_range is not None
                and router_ping_range > 18
            ),
            (
                router_jitter_median is not None
                and router_jitter_median > 10
            ),
            (
                router_jitter_p90 is not None
                and router_jitter_p90 > 18
            ),
        ])

        if local_unstable:
            reasons = []

            if (
                router_ping_median is not None
                and router_ping_median > 20
            ):
                reasons.append(
                    f"router ping median {router_ping_median:.1f} ms"
                )

            if (
                router_ping_p90 is not None
                and router_ping_p90 > 30
            ):
                reasons.append(
                    f"router ping P90 {router_ping_p90:.1f} ms"
                )

            if (
                router_ping_range is not None
                and router_ping_range > 18
            ):
                reasons.append(
                    f"router ping spread {router_ping_range:.1f} ms"
                )

            if (
                router_jitter_median is not None
                and router_jitter_median > 10
            ):
                reasons.append(
                    f"router jitter median {router_jitter_median:.1f} ms"
                )

            if (
                router_jitter_p90 is not None
                and router_jitter_p90 > 18
            ):
                reasons.append(
                    f"router jitter P90 {router_jitter_p90:.1f} ms"
                )

            if (
                router_loss_max is not None
                and router_loss_max > 0
            ):
                reasons.append(
                    f"local loss reached {router_loss_max:.1f}%"
                )

            if not reasons:
                reasons.append(
                    "local router measurements were incomplete"
                )

            status = "LOCAL_SOURCE_UNSTABLE"
            summary = (
                "The source is already unstable near the router: "
                + "; ".join(reasons)
                + ". Room comparisons are locked until the source becomes stable."
            )

        else:
            # Third gate: WAN/Internet must also be reasonably stable.
            wan_unstable = any([
                healthy_public_rounds < max(
                    5,
                    len(rounds) - 1
                ),
                http_success_rate < 85,
                (
                    public_loss_max is not None
                    and public_loss_max >= 20
                ),
                (
                    public_jitter_median is not None
                    and public_jitter_median > 30
                ),
                (
                    public_jitter_p90 is not None
                    and public_jitter_p90 > 50
                ),
            ])

            if wan_unstable:
                reasons = []

                if healthy_public_rounds < max(
                    5,
                    len(rounds) - 1
                ):
                    reasons.append(
                        f"only {healthy_public_rounds}/{len(rounds)} rounds had at least 2 healthy public targets plus HTTP"
                    )

                if http_success_rate < 85:
                    reasons.append(
                        f"HTTP success was {http_success_rate:.0f}%"
                    )

                if (
                    public_loss_max is not None
                    and public_loss_max >= 20
                ):
                    reasons.append(
                        f"public-target loss reached {public_loss_max:.1f}%"
                    )

                if (
                    public_jitter_median is not None
                    and public_jitter_median > 30
                ):
                    reasons.append(
                        f"public jitter median {public_jitter_median:.1f} ms"
                    )

                if (
                    public_jitter_p90 is not None
                    and public_jitter_p90 > 50
                ):
                    reasons.append(
                        f"public jitter P90 {public_jitter_p90:.1f} ms"
                    )

                status = "WAN_ISP_UNSTABLE"
                summary = (
                    "The local link to the router is stable, "
                    "but the Internet side is not stable enough: "
                    + "; ".join(reasons)
                    + ". Room testing is locked because the source is not clean."
                )

            else:
                status = "SOURCE_READY"
                summary = (
                    "Source is stable near the router. "
                    "Room-by-room Wi-Fi testing is now valid."
                )

    return {
        "timestamp": timestamp,
        "status": status,
        "summary": summary,
        "rounds": len(rounds),
        "rssi_median": rssi_median,
        "rssi_range": rssi_range,
        "router_ping_median": router_ping_median,
        "router_ping_p90": router_ping_p90,
        "router_ping_range": router_ping_range,
        "router_jitter_median": router_jitter_median,
        "router_jitter_p90": router_jitter_p90,
        "router_loss_max": router_loss_max,
        "public_ping_median": public_ping_median,
        "public_ping_p90": public_ping_p90,
        "public_jitter_median": public_jitter_median,
        "public_jitter_p90": public_jitter_p90,
        "public_loss_max": public_loss_max,
        "http_success_rate": http_success_rate,
        "healthy_public_rounds": healthy_public_rounds,
    }



def perform_room_round():
    wifi = active_wifi_info()

    jobs = {"Router": ROOM_ROUTER}
    jobs.update(ROOM_TARGETS)

    results = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(
                active_ping,
                host,
                6 if name == "Router" else 4
            ): name
            for name, host in jobs.items()
        }

        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception:
                results[name] = {
                    "ping": None,
                    "jitter": None,
                    "loss": 100.0
                }

    router = results["Router"]
    public_results = [results[name] for name in ROOM_TARGETS]

    return {
        "rssi": wifi.get("rssi"),
        "frequency_mhz": wifi.get("frequency_mhz"),
        "link_speed_mbps": wifi.get("link_speed_mbps"),
        "router_ping": router.get("ping"),
        "router_jitter": router.get("jitter"),
        "router_loss": router.get("loss"),
        "public_ping": median_value([x.get("ping") for x in public_results]),
        "public_jitter": median_value([x.get("jitter") for x in public_results]),
        "public_loss": median_value([x.get("loss") for x in public_results]),
    }


def aggregate_room_rounds(rounds):
    frequency = median_value([x.get("frequency_mhz") for x in rounds])

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rssi": median_value([x.get("rssi") for x in rounds]),
        "frequency_mhz": frequency,
        "band": room_band(frequency),
        "channel": room_channel(frequency),
        "link_speed_mbps": median_value([x.get("link_speed_mbps") for x in rounds]),
        "router_ping": median_value([x.get("router_ping") for x in rounds]),
        "router_jitter": median_value([x.get("router_jitter") for x in rounds]),
        "router_loss": median_value([x.get("router_loss") for x in rounds]),
        "public_ping": median_value([x.get("public_ping") for x in rounds]),
        "public_jitter": median_value([x.get("public_jitter") for x in rounds]),
        "public_loss": median_value([x.get("public_loss") for x in rounds]),
        "rounds": len(rounds),
    }


def perform_room_measurement():
    rounds = [perform_room_round() for _ in range(3)]
    return aggregate_room_rounds(rounds)


def save_room_measurement(role, label, measurement):
    ensure_room_table()

    con = sqlite3.connect(DB, timeout=10)

    try:
        cur = con.execute("""
            INSERT INTO wifi_room_tests (
                timestamp,
                role,
                label,
                rssi,
                frequency_mhz,
                band,
                channel,
                link_speed_mbps,
                router_ping,
                router_jitter,
                router_loss,
                public_ping,
                public_jitter,
                public_loss
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            measurement["timestamp"],
            role,
            label,
            measurement["rssi"],
            measurement["frequency_mhz"],
            measurement["band"],
            measurement["channel"],
            measurement["link_speed_mbps"],
            measurement["router_ping"],
            measurement["router_jitter"],
            measurement["router_loss"],
            measurement["public_ping"],
            measurement["public_jitter"],
            measurement["public_loss"]
        ))

        con.commit()
        return cur.lastrowid

    finally:
        con.close()


def latest_room_baseline():
    ensure_room_table()

    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row

    try:
        row = con.execute("""
            SELECT *
            FROM wifi_room_tests
            WHERE role = 'baseline'
            ORDER BY id DESC
            LIMIT 1
        """).fetchone()

        return dict(row) if row else None

    finally:
        con.close()


def room_test_rows(limit=30):
    ensure_room_table()

    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row

    try:
        rows = con.execute("""
            SELECT *
            FROM wifi_room_tests
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        con.close()


def compare_room(test, baseline):
    if not baseline:
        return {
            "status": "NO_BASELINE",
            "summary": "Save a near-router baseline first."
        }

    notes = []
    status = "GOOD"

    def difference(key):
        a = test.get(key)
        b = baseline.get(key)

        if a is None or b is None:
            return None

        return float(a) - float(b)

    rssi_delta = difference("rssi")
    ping_delta = difference("router_ping")
    jitter_delta = difference("router_jitter")
    loss_delta = difference("router_loss")
    link_delta = difference("link_speed_mbps")

    if rssi_delta is not None:
        if rssi_delta <= -15:
            status = "BAD"
            notes.append(
                f"signal is {abs(rssi_delta):.0f} dB weaker"
            )

        elif rssi_delta <= -8:
            if status != "BAD":
                status = "WARN"

            notes.append(
                f"signal is {abs(rssi_delta):.0f} dB weaker"
            )

    if ping_delta is not None:
        if ping_delta >= 25:
            status = "BAD"
            notes.append(
                f"router ping increased by "
                f"{ping_delta:.1f} ms"
            )

        elif ping_delta >= 10:
            if status != "BAD":
                status = "WARN"

            notes.append(
                f"router ping increased by "
                f"{ping_delta:.1f} ms"
            )

    if jitter_delta is not None:
        if jitter_delta >= 20:
            status = "BAD"
            notes.append(
                f"router jitter increased by "
                f"{jitter_delta:.1f} ms"
            )

        elif jitter_delta >= 8:
            if status != "BAD":
                status = "WARN"

            notes.append(
                f"router jitter increased by "
                f"{jitter_delta:.1f} ms"
            )

    if (
        test.get("router_loss") is not None
        and float(test["router_loss"]) >= 5
    ):
        status = "BAD"
        notes.append(
            f"local packet loss is "
            f"{float(test['router_loss']):.1f}%"
        )

    elif loss_delta is not None and loss_delta >= 3:
        if status != "BAD":
            status = "WARN"

        notes.append(
            f"local packet loss increased by "
            f"{loss_delta:.1f}%"
        )

    if link_delta is not None and link_delta <= -80:
        if status != "BAD":
            status = "WARN"

        notes.append(
            f"link speed dropped by "
            f"{abs(link_delta):.0f} Mbps"
        )

    if (
        test.get("band")
        and baseline.get("band")
        and test["band"] != baseline["band"]
    ):
        notes.append(
            f"band changed from "
            f"{baseline['band']} to {test['band']}"
        )

    if not notes:
        summary = (
            "This room is close to the "
            "near-router baseline."
        )
    else:
        summary = "; ".join(notes) + "."

    return {
        "status": status,
        "summary": summary,
        "rssi_delta": rssi_delta,
        "router_ping_delta": ping_delta,
        "router_jitter_delta": jitter_delta,
        "router_loss_delta": loss_delta,
        "link_speed_delta": link_delta
    }



@app.route(
    "/api/room-test-progress",
    methods=["POST"]
)
def api_room_test_progress():
    payload = request.get_json(
        silent=True
    ) or {}

    role = payload.get(
        "role",
        "room"
    )

    label = (
        payload.get("label") or ""
    ).strip()

    if role not in (
        "baseline",
        "room"
    ):
        return jsonify({
            "error": "Invalid room-test role"
        }), 400

    if role == "baseline":
        label = "Near router"

    elif not label:
        return jsonify({
            "error": "Room name is required"
        }), 400

    # Room testing is impossible unless the latest source qualification passed.
    if role == "room":
        source = latest_source_qualification()

        if (
            not source
            or source.get("status") != "SOURCE_READY"
        ):
            return jsonify({
                "error": (
                    "Source is not qualified as stable. "
                    "Run Source Qualification near the router first."
                )
            }), 409

        if not latest_room_baseline():
            return jsonify({
                "error": (
                    "No valid near-router baseline exists. "
                    "Run Source Qualification first."
                )
            }), 409

    token = begin_test_mode(
        role,
        label
    )

    if not token:
        return jsonify({
            "error": (
                "Another Wi-Fi room test "
                "is already running"
            )
        }), 409

    @stream_with_context
    def generate():
        def emit(obj):
            return json.dumps(obj) + "\n"

        try:
            yield emit({
                "type": "progress",
                "percent": 2,
                "stage": "Entering TEST MODE..."
            })

            if monitor_process_running():
                deadline = (
                    time.monotonic() + 20
                )

                while (
                    time.monotonic()
                    < deadline
                ):
                    if monitor_acknowledged(
                        token
                    ):
                        break

                    yield emit({
                        "type": "progress",
                        "percent": 5,
                        "stage": (
                            "Pausing background monitor "
                            "so the measurement stays clean..."
                        )
                    })

                    time.sleep(0.5)

                if not monitor_acknowledged(
                    token
                ):
                    yield emit({
                        "type": "error",
                        "percent": 0,
                        "stage": (
                            "Could not enter TEST MODE"
                        ),
                        "message": (
                            "The background monitor did not "
                            "confirm TEST MODE."
                        )
                    })
                    return

            if role == "baseline":
                # Qualification is intentionally longer than a room test.
                # We need repeated measurements over time to prove that
                # the source itself is stable.
                rounds = []

                yield emit({
                    "type": "progress",
                    "percent": 8,
                    "stage": (
                        "Source Qualification started — "
                        "keep the phone 1–2 m from the router..."
                    )
                })

                for index in range(
                    SOURCE_QUALIFICATION_ROUNDS
                ):
                    start_percent = (
                        10
                        + index * 12
                    )

                    yield emit({
                        "type": "progress",
                        "percent": start_percent,
                        "stage": (
                            f"Stability sample "
                            f"{index + 1} of "
                            f"{SOURCE_QUALIFICATION_ROUNDS}..."
                        )
                    })

                    round_result = (
                        perform_source_round()
                    )

                    rounds.append(
                        round_result
                    )

                    yield emit({
                        "type": "progress",
                        "percent": (
                            start_percent + 8
                        ),
                        "stage": (
                            f"Sample {index + 1} complete — "
                            f"router {round_result.get('router_ping') or 0:.1f} ms, "
                            f"jitter {round_result.get('router_jitter') or 0:.1f} ms"
                        )
                    })

                yield emit({
                    "type": "progress",
                    "percent": 86,
                    "stage": (
                        "Analyzing source stability..."
                    )
                })

                qualification = (
                    qualify_source(rounds)
                )

                save_source_qualification(
                    qualification
                )

                baseline = None

                if (
                    qualification["status"]
                    == "SOURCE_READY"
                ):
                    baseline_measurement = (
                        aggregate_room_rounds(
                            rounds
                        )
                    )

                    save_room_measurement(
                        "baseline",
                        "Near router",
                        baseline_measurement
                    )

                    baseline = (
                        latest_room_baseline()
                    )

                    yield emit({
                        "type": "progress",
                        "percent": 94,
                        "stage": (
                            "SOURCE READY — "
                            "saving valid baseline..."
                        )
                    })

                else:
                    yield emit({
                        "type": "progress",
                        "percent": 94,
                        "stage": (
                            qualification["status"]
                            .replace("_", " ")
                        )
                    })

                time.sleep(2)
                end_test_mode(token)

                yield emit({
                    "type": "result",
                    "percent": 100,
                    "stage": "Complete",
                    "qualification": qualification,
                    "baseline": baseline,
                    "comparison": {
                        "status": (
                            "BASELINE"
                            if qualification["status"]
                            == "SOURCE_READY"
                            else qualification["status"]
                        ),
                        "summary": (
                            qualification["summary"]
                        )
                    }
                })

            else:
                # Once the source is proven stable, room tests remain short:
                # 3 rounds and the median.
                rounds = []

                for index in range(3):
                    yield emit({
                        "type": "progress",
                        "percent": (
                            12 + index * 25
                        ),
                        "stage": (
                            f"Room sample "
                            f"{index + 1} of 3..."
                        )
                    })

                    rounds.append(
                        perform_room_round()
                    )

                    yield emit({
                        "type": "progress",
                        "percent": (
                            32 + index * 25
                        ),
                        "stage": (
                            f"Room sample "
                            f"{index + 1} complete"
                        )
                    })

                yield emit({
                    "type": "progress",
                    "percent": 90,
                    "stage": (
                        "Comparing room with "
                        "the qualified baseline..."
                    )
                })

                measurement = (
                    aggregate_room_rounds(
                        rounds
                    )
                )

                save_room_measurement(
                    role,
                    label,
                    measurement
                )

                baseline = (
                    latest_room_baseline()
                )

                comparison = compare_room(
                    measurement,
                    baseline
                )

                time.sleep(2)
                end_test_mode(token)

                yield emit({
                    "type": "result",
                    "percent": 100,
                    "stage": "Complete",
                    "measurement": measurement,
                    "comparison": comparison,
                    "baseline": baseline,
                    "qualification": (
                        latest_source_qualification()
                    )
                })

        finally:
            end_test_mode(token)

    return Response(
        generate(),
        mimetype=(
            "application/x-ndjson"
        ),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


@app.route(
    "/api/room-test",
    methods=["POST"]
)
def api_room_test():
    payload = request.get_json(
        silent=True
    ) or {}

    role = payload.get(
        "role",
        "room"
    )

    label = (
        payload.get("label") or ""
    ).strip()

    if role not in (
        "baseline",
        "room"
    ):
        return jsonify({
            "error": "Invalid room-test role"
        }), 400

    if role == "baseline":
        label = "Near router"

    elif not label:
        return jsonify({
            "error": "Room name is required"
        }), 400

    measurement = perform_room_measurement()

    save_room_measurement(
        role,
        label,
        measurement
    )

    baseline = latest_room_baseline()

    if role == "baseline":
        comparison = {
            "status": "BASELINE",
            "summary": (
                "Baseline saved. "
                "Now test another room."
            )
        }

    else:
        comparison = compare_room(
            measurement,
            baseline
        )

    return jsonify({
        "measurement": measurement,
        "comparison": comparison,
        "baseline": baseline
    })


@app.route("/api/room-tests")
def api_room_tests():
    source = (
        latest_source_qualification()
    )

    source_ready = bool(
        source
        and source.get("status")
        == "SOURCE_READY"
    )

    baseline = (
        latest_room_baseline()
        if source_ready
        else None
    )

    rows = room_test_rows()

    result = []

    for row in rows:
        item = dict(row)

        if row["role"] == "baseline":
            item["comparison"] = {
                "status": "BASELINE",
                "summary": (
                    "Qualified reference measurement"
                    if source_ready
                    else "Previous reference measurement"
                )
            }

        elif source_ready and baseline:
            item["comparison"] = (
                compare_room(
                    row,
                    baseline
                )
            )

        else:
            item["comparison"] = {
                "status": "NO_BASELINE",
                "summary": (
                    "Source is not currently qualified. "
                    "Run Source Qualification again."
                )
            }

        result.append(item)

    return jsonify({
        "source": source,
        "source_ready": source_ready,
        "baseline": baseline,
        "tests": result
    })


@app.route(
    "/api/room-tests/clear",
    methods=["POST"]
)
def api_clear_room_tests():
    ensure_room_table()

    con = sqlite3.connect(
        DB,
        timeout=10
    )

    try:
        con.execute(
            "DELETE FROM wifi_room_tests"
        )
        con.execute(
            "DELETE FROM wifi_source_qualification"
        )
        con.commit()

    finally:
        con.close()

    return jsonify({"ok": True})


@app.route("/api/data")
def api_data():
    return jsonify(get_rows())


@app.route("/api/events")
def api_events():
    return jsonify(build_events())


@app.route("/api/summary")
def api_summary():
    return jsonify(build_summary())


@app.route("/")
def index():
    html = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0b0b">
<title>Internet Monitor V5 + Rooms</title>

<style>
:root {
    --bg: #0b0b0b;
    --card: #191919;
    --card2: #242424;
    --text: #f5f5f5;
    --muted: #aaa;
    --green: #35e66b;
    --yellow: #ffd54a;
    --orange: #ff9f43;
    --red: #ff5252;
}

* { box-sizing: border-box; }

body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: Arial, Helvetica, sans-serif;
    padding: 18px;
}

.container {
    max-width: 920px;
    margin: auto;
}

h1 {
    font-size: clamp(34px, 9vw, 56px);
    margin: 8px 0 26px;
}

.card {
    background: var(--card);
    border-radius: 22px;
    padding: 20px;
    margin-bottom: 14px;
}

.grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 14px;
}

.metric {
    background: var(--card2);
    border-radius: 18px;
    padding: 16px;
}

.label {
    color: var(--muted);
    font-size: 14px;
    margin-bottom: 8px;
}

.value {
    font-size: clamp(23px, 6vw, 36px);
}

.health-score {
    font-size: clamp(50px, 14vw, 78px);
    font-weight: bold;
}

.health-text {
    font-size: 26px;
}

.green { color: var(--green); }
.yellow { color: var(--yellow); }
.orange { color: var(--orange); }
.red { color: var(--red); }

.diagnosis {
    font-size: 24px;
    line-height: 1.3;
}

.confidence {
    margin-top: 8px;
    color: var(--muted);
}

.section-title {
    font-size: 19px;
    font-weight: bold;
    margin-bottom: 14px;
}

.target-row {
    display: grid;
    grid-template-columns: 1.2fr repeat(3, 1fr) auto;
    gap: 8px;
    align-items: center;
    background: var(--card2);
    padding: 13px;
    border-radius: 14px;
    margin-bottom: 9px;
}

.target-name {
    font-weight: bold;
}

.target-meta {
    font-size: 14px;
    color: #ddd;
    white-space: nowrap;
}

.badge {
    padding: 5px 9px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: bold;
    background: #333;
}

.badge.ok { color: var(--green); }
.badge.warn { color: var(--yellow); }
.badge.bad { color: var(--red); }

.http-row {
    display: flex;
    justify-content: space-between;
    font-size: 20px;
}


.wifi-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
}

.wifi-item {
    background: var(--card2);
    padding: 13px;
    border-radius: 14px;
}

.wifi-value {
    font-size: 24px;
    margin-top: 5px;
}

.wifi-advice {
    margin-top: 12px;
    color: #ddd;
    line-height: 1.4;
}


.room-test-card {
    border: 1px solid #303030;
}

.room-step {
    background: var(--card2);
    border-radius: 15px;
    padding: 15px;
    margin-bottom: 11px;
}

.room-step-title {
    font-size: 18px;
    font-weight: 700;
    margin-bottom: 7px;
}

.room-step-text {
    color: var(--muted);
    font-size: 14px;
    line-height: 1.4;
}

.room-actions {
    margin-top: 13px;
}

.room-button {
    appearance: none;
    border: 0;
    border-radius: 14px;
    padding: 13px 16px;
    min-height: 48px;
    background: var(--green);
    color: #07130a;
    font-size: 16px;
    font-weight: 700;
}

.room-button:disabled {
    opacity: 0.55;
}

.room-secondary {
    appearance: none;
    border: 1px solid #555;
    border-radius: 12px;
    padding: 11px 14px;
    background: #2b2b2b;
    color: var(--text);
    font-weight: 700;
}

.room-input-row {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 10px;
    margin-top: 13px;
}

.room-input {
    min-width: 0;
    width: 100%;
    border: 1px solid #444;
    border-radius: 13px;
    padding: 12px;
    background: #101010;
    color: var(--text);
    font-size: 16px;
}



.source-gate {
    margin: 12px 0;
    border-radius: 15px;
    padding: 15px;
    border: 1px solid #444;
    background: var(--card2);
}

.source-gate-title {
    font-size: 18px;
    font-weight: 800;
    margin-bottom: 7px;
}

.source-gate-summary {
    line-height: 1.45;
}

.source-gate-metrics {
    margin-top: 9px;
    color: var(--muted);
    font-size: 13px;
    line-height: 1.45;
}

.source-gate-ready {
    border-color: #2c7541;
}

.source-gate-ready .source-gate-title {
    color: var(--green);
}

.source-gate-local,
.source-gate-wan,
.source-gate-signal {
    border-color: #806c2f;
}

.source-gate-local .source-gate-title,
.source-gate-wan .source-gate-title,
.source-gate-signal .source-gate-title {
    color: var(--yellow);
}

.source-gate-idle .source-gate-title {
    color: var(--muted);
}

.room-lock-hint {
    margin-top: 9px;
}

.room-progress-wrap {
    display: none;
    margin-top: 14px;
}

.room-progress-wrap.show {
    display: block;
}

.room-progress-track {
    width: 100%;
    height: 12px;
    background: #2b2b2b;
    border-radius: 999px;
    overflow: hidden;
}

.room-progress-bar {
    width: 0%;
    height: 100%;
    background: var(--green);
    transition: width 0.35s ease;
}

.room-progress-label {
    margin-top: 8px;
    color: var(--muted);
    font-size: 14px;
    line-height: 1.4;
}

.room-status {
    color: var(--muted);
    margin: 12px 0;
    line-height: 1.4;
}

.room-results {
    display: grid;
    gap: 10px;
    margin-top: 14px;
}

.room-result {
    background: var(--card2);
    border-radius: 15px;
    padding: 14px;
}

.room-result-head {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: center;
}

.room-result-name {
    font-size: 18px;
    font-weight: 700;
}

.room-badge {
    border-radius: 999px;
    padding: 5px 9px;
    background: #333;
    font-size: 12px;
    font-weight: 700;
}

.room-good {
    color: var(--green);
}

.room-warn {
    color: var(--yellow);
}

.room-bad {
    color: var(--red);
}

.room-baseline {
    color: #8ab4ff;
}

.room-description {
    margin-top: 8px;
    line-height: 1.4;
}

.room-metrics {
    color: var(--muted);
    font-size: 13px;
    line-height: 1.45;
    margin-top: 9px;
}

@media (max-width: 620px) {
    .room-input-row {
        grid-template-columns: 1fr;
    }
}

.summary-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
}

.summary-item {
    background: var(--card2);
    padding: 13px;
    border-radius: 14px;
}

.summary-value {
    font-size: 24px;
    margin-top: 5px;
}

canvas {
    width: 100%;
    height: 230px;
    background: #101010;
    border-radius: 14px;
}

.event {
    background: var(--card2);
    border-radius: 14px;
    padding: 14px;
    margin-bottom: 10px;
}

.event-head {
    display: flex;
    justify-content: space-between;
    gap: 12px;
}

.event-type {
    font-weight: bold;
}

.event-desc {
    margin-top: 7px;
    line-height: 1.3;
}

.event-meta {
    color: var(--muted);
    font-size: 13px;
    margin-top: 8px;
}

.active {
    color: var(--red);
}

.empty {
    color: var(--muted);
}

.timestamp {
    color: var(--muted);
    font-size: 13px;
    margin-top: 12px;
}

@media (max-width: 620px) {
    .target-row {
        grid-template-columns: 1fr 1fr;
    }
}
</style>
</head>

<body>
<div class="container">

<h1>Internet Monitor</h1>

<div class="card">
    <div class="label">Internet Health</div>
    <div id="score" class="health-score">-- / 100</div>
    <div id="health" class="health-text">Loading...</div>
</div>

<div class="card">
    <div class="label">Diagnosis</div>
    <div id="diagnosis" class="diagnosis">--</div>
    <div id="confidence" class="confidence">Confidence: --</div>
</div>

<div class="grid">
    <div class="metric">
        <div class="label">WAN</div>
        <div id="wan" class="value">--</div>
    </div>

    <div class="metric">
        <div class="label">WAN Uptime</div>
        <div id="uptime" class="value">--</div>
    </div>

    <div class="metric">
        <div class="label">Router Ping</div>
        <div id="routerPing" class="value">--</div>
    </div>

    <div class="metric">
        <div class="label">Router Jitter</div>
        <div id="routerJitter" class="value">--</div>
    </div>

    <div class="metric">
        <div class="label">Internet Ping</div>
        <div id="netPing" class="value">--</div>
    </div>

    <div class="metric">
        <div class="label">Internet Jitter</div>
        <div id="jitter" class="value">--</div>
    </div>

    <div class="metric">
        <div class="label">Packet Loss</div>
        <div id="loss" class="value">--</div>
    </div>

    <div class="metric">
        <div class="label">Traffic ↓ / ↑</div>
        <div id="traffic" class="value">--</div>
    </div>
</div>


<div class="card">
    <div class="section-title">Wi-Fi diagnostics</div>

    <div class="wifi-grid">
        <div class="wifi-item">
            <div class="label">Signal (RSSI)</div>
            <div id="wifiRssi" class="wifi-value">--</div>
        </div>

        <div class="wifi-item">
            <div class="label">Signal quality</div>
            <div id="wifiQuality" class="wifi-value">--</div>
        </div>

        <div class="wifi-item">
            <div class="label">Band / Channel</div>
            <div id="wifiBandChannel" class="wifi-value">--</div>
        </div>

        <div class="wifi-item">
            <div class="label">Link speed</div>
            <div id="wifiLinkSpeed" class="wifi-value">--</div>
        </div>
    </div>

    <div id="wifiAdvice" class="wifi-advice">Waiting for Wi-Fi data...</div>
</div>


<div class="card room-test-card">
    <div class="section-title">
        Room-by-room Wi-Fi test
    </div>

    <div class="room-step">
        <div class="room-step-title">
            1. Qualify the source near the router
        </div>

        <div class="room-step-text">
            Stand about 1–2 meters from the router and keep the phone still.
            The app will pause background monitoring and take 6 stability samples
            over time. Only a stable source can become the room-test baseline.
        </div>

        <div class="room-actions">
            <button
                id="saveRoomBaseline"
                class="room-button"
                type="button"
            >
                Qualify Source & Save Baseline
            </button>
        </div>
    </div>

    <div
        id="sourceGate"
        class="source-gate source-gate-idle"
        aria-live="polite"
    >
        <div
            id="sourceGateTitle"
            class="source-gate-title"
        >
            SOURCE NOT QUALIFIED
        </div>

        <div
            id="sourceGateSummary"
            class="source-gate-summary"
        >
            Run Source Qualification near the router before testing rooms.
        </div>

        <div
            id="sourceGateMetrics"
            class="source-gate-metrics"
        ></div>
    </div>

    <div class="room-step">
        <div class="room-step-title">
            2. Test another room
        </div>

        <div class="room-step-text">
            Walk to the room, enter a name, keep the
            phone still, and run the comparison. The app will pause background monitoring, run 3 rounds, show live progress, and use the median.
        </div>

        <div class="room-input-row">
            <input
                id="roomName"
                class="room-input"
                type="text"
                maxlength="40"
                placeholder="Example: Bedroom"
            >

            <button
                id="testRoomButton"
                class="room-button"
                type="button"
                disabled
            >
                Test Room
            </button>
        </div>

        <div
            id="roomLockHint"
            class="room-step-text room-lock-hint"
        >
            Locked until the source is qualified as stable.
        </div>
    </div>

    <div
        id="roomProgressWrap"
        class="room-progress-wrap"
    >
        <div class="room-progress-track">
            <div
                id="roomProgressBar"
                class="room-progress-bar"
            ></div>
        </div>

        <div
            id="roomProgressLabel"
            class="room-progress-label"
            aria-live="polite"
        >
            Waiting...
        </div>
    </div>

    <div
        id="roomTestStatus"
        class="room-status"
        aria-live="polite"
    ></div>

    <button
        id="clearRoomTests"
        class="room-secondary"
        type="button"
    >
        Clear Room Tests
    </button>

    <div
        id="roomResults"
        class="room-results"
    >
        <div class="empty">
            No room tests yet.
        </div>
    </div>
</div>

<div class="card">
    <div class="section-title">Public targets</div>

    <div class="target-row">
        <div class="target-name">Cloudflare</div>
        <div id="cfPing" class="target-meta">-- ms</div>
        <div id="cfJitter" class="target-meta">jit --</div>
        <div id="cfLoss" class="target-meta">-- %</div>
        <div id="cfState" class="badge">--</div>
    </div>

    <div class="target-row">
        <div class="target-name">Google</div>
        <div id="gPing" class="target-meta">-- ms</div>
        <div id="gJitter" class="target-meta">jit --</div>
        <div id="gLoss" class="target-meta">-- %</div>
        <div id="gState" class="badge">--</div>
    </div>

    <div class="target-row">
        <div class="target-name">Quad9</div>
        <div id="qPing" class="target-meta">-- ms</div>
        <div id="qJitter" class="target-meta">jit --</div>
        <div id="qLoss" class="target-meta">-- %</div>
        <div id="qState" class="badge">--</div>
    </div>
</div>

<div class="card">
    <div class="section-title">HTTP connectivity</div>
    <div class="http-row">
        <span id="httpState">--</span>
        <span id="httpLatency">-- ms</span>
    </div>
</div>

<div class="card">
    <div class="section-title">24-hour summary</div>
    <div class="summary-grid">
        <div class="summary-item">
            <div class="label">Availability</div>
            <div id="availability" class="summary-value">--</div>
        </div>

        <div class="summary-item">
            <div class="label">Average score</div>
            <div id="avgScore" class="summary-value">--</div>
        </div>

        <div class="summary-item">
            <div class="label">Worst score</div>
            <div id="minScore" class="summary-value">--</div>
        </div>

        <div class="summary-item">
            <div class="label">Confirmed events</div>
            <div id="eventCount" class="summary-value">--</div>
        </div>
    </div>
</div>

<div class="card">
    <div class="section-title">Health history</div>
    <canvas id="chart"></canvas>
</div>

<div class="card">
    <div class="section-title">Confirmed events — last 24 hours</div>
    <div id="events">
        <div class="empty">Loading events...</div>
    </div>
</div>

<div id="updated" class="timestamp">Last update: --</div>

</div>

<script>
function num(v) {
    if (v === null || v === undefined) return null;
    let n = Number(v);
    return Number.isFinite(n) ? n : null;
}

function fmt(v, digits = 1) {
    let n = num(v);
    return n === null ? "---" : n.toFixed(digits);
}

function uptime(seconds) {
    let s = Number(seconds || 0);
    let d = Math.floor(s / 86400);
    let h = Math.floor((s % 86400) / 3600);
    let m = Math.floor((s % 3600) / 60);
    return `${d}d ${h}h ${m}m`;
}

function scoreClass(score) {
    if (score >= 90) return "green";
    if (score >= 75) return "green";
    if (score >= 55) return "yellow";
    if (score >= 30) return "orange";
    return "red";
}

function targetState(ping, jitter, loss) {
    let p = num(ping);
    let j = num(jitter);
    let l = num(loss);

    if (p === null || l === null || l >= 100) {
        return {text: "DOWN", cls: "bad"};
    }

    if (l >= 20) {
        return {text: "BAD", cls: "bad"};
    }

    if (l > 0) {
        return {text: "LOSS", cls: "warn"};
    }

    if (j !== null && j >= 30) {
        return {text: "JITTER", cls: "warn"};
    }

    if (p >= 120) {
        return {text: "SLOW", cls: "warn"};
    }

    return {text: "OK", cls: "ok"};
}

function setTarget(prefix, ping, jitter, loss) {
    document.getElementById(prefix + "Ping").innerText =
        fmt(ping) + " ms";

    document.getElementById(prefix + "Jitter").innerText =
        "jit " + fmt(jitter);

    document.getElementById(prefix + "Loss").innerText =
        fmt(loss) + " %";

    let state = targetState(ping, jitter, loss);
    let el = document.getElementById(prefix + "State");

    el.innerText = state.text;
    el.className = "badge " + state.cls;
}


function wifiAdvice(r) {
    let rssi = num(r.wifi_rssi);
    let routerPing = num(r.router_ping);
    let routerJitter = num(r.router_jitter);
    let band = r.wifi_band || "Unknown";

    if (rssi !== null && rssi <= -75) {
        return "Weak signal: move closer to the router or improve Wi-Fi coverage.";
    }

    if (
        band === "2.4 GHz" &&
        routerJitter !== null &&
        routerJitter >= 20
    ) {
        return "2.4 GHz plus high local jitter suggests interference/congestion. If your router has 5 GHz, test 5 GHz while near the router.";
    }

    if (
        rssi !== null &&
        rssi >= -65 &&
        routerJitter !== null &&
        routerJitter >= 20
    ) {
        return "Signal strength is good, so interference or local Wi-Fi congestion is more likely than distance.";
    }

    if (
        rssi !== null &&
        rssi <= -66 &&
        routerPing !== null &&
        routerPing >= 25
    ) {
        return "Signal is only fair and local latency is elevated. Distance/walls may be contributing.";
    }

    return "Wi-Fi radio conditions currently look reasonable.";
}


function durationText(sec) {
    sec = Number(sec || 0);

    let h = Math.floor(sec / 3600);
    let m = Math.floor((sec % 3600) / 60);
    let s = Math.floor(sec % 60);

    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function shortTime(ts) {
    if (!ts) return "--";
    let parts = ts.split(" ");
    return parts.length >= 2 ? parts[1] : ts;
}

function renderEvents(events) {
    let root = document.getElementById("events");

    if (!events.length) {
        root.innerHTML =
            '<div class="empty">No confirmed degradation events in the last 24 hours.</div>';
        return;
    }

    root.innerHTML = events.map(e => {
        let status = e.active
            ? '<span class="active">ACTIVE</span>'
            : 'Recovered';

        let end = e.ended_at
            ? shortTime(e.ended_at)
            : 'now';

        return `
        <div class="event">
            <div class="event-head">
                <div class="event-type">${e.type}</div>
                <div>${status}</div>
            </div>
            <div class="event-desc">${e.description || "--"}</div>
            <div class="event-meta">
                ${shortTime(e.started_at)} → ${end}
                · ${durationText(e.duration_sec)}
                · min score ${e.min_score ?? "--"}
            </div>
        </div>`;
    }).join("");
}

function drawChart(rows) {
    let canvas = document.getElementById("chart");
    let ratio = window.devicePixelRatio || 1;

    canvas.width = canvas.clientWidth * ratio;
    canvas.height = 230 * ratio;

    let ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

    let w = canvas.clientWidth;
    let h = 230;

    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "#333";
    ctx.lineWidth = 1;

    for (let value of [0, 25, 50, 75, 100]) {
        let y = h - (value / 100) * h;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
    }

    if (!rows || rows.length < 2) return;

    ctx.strokeStyle = "#35e66b";
    ctx.lineWidth = 3;
    ctx.beginPath();

    rows.forEach((r, i) => {
        let score = Number(r.score || 0);
        let x = (i / (rows.length - 1)) * w;
        let y = h - (score / 100) * h;

        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });

    ctx.stroke();
}


function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function roomBadge(status) {
    if (status === "BAD") {
        return {
            text: "POOR",
            cls: "room-bad"
        };
    }

    if (status === "WARN") {
        return {
            text: "WEAKER",
            cls: "room-warn"
        };
    }

    if (status === "BASELINE") {
        return {
            text: "BASELINE",
            cls: "room-baseline"
        };
    }

    if (status === "NO_BASELINE") {
        return {
            text: "NO BASELINE",
            cls: "room-warn"
        };
    }

    return {
        text: "GOOD",
        cls: "room-good"
    };
}


function sourceGateClass(status) {
    if (status === "SOURCE_READY") {
        return "source-gate source-gate-ready";
    }

    if (status === "LOCAL_SOURCE_UNSTABLE") {
        return "source-gate source-gate-local";
    }

    if (status === "WAN_ISP_UNSTABLE") {
        return "source-gate source-gate-wan";
    }

    if (status === "REFERENCE_SIGNAL_TOO_WEAK") {
        return "source-gate source-gate-signal";
    }

    return "source-gate source-gate-idle";
}

function sourceGateTitle(status) {
    if (status === "SOURCE_READY") {
        return "SOURCE READY";
    }

    if (status === "LOCAL_SOURCE_UNSTABLE") {
        return "LOCAL SOURCE UNSTABLE";
    }

    if (status === "WAN_ISP_UNSTABLE") {
        return "WAN / ISP UNSTABLE";
    }

    if (status === "REFERENCE_SIGNAL_TOO_WEAK") {
        return "REFERENCE SIGNAL TOO WEAK";
    }

    return "SOURCE NOT QUALIFIED";
}

function updateSourceGate(payload) {
    const source = payload.source || null;
    const ready = Boolean(
        payload.source_ready
    );

    const gate = document.getElementById(
        "sourceGate"
    );

    const title = document.getElementById(
        "sourceGateTitle"
    );

    const summary = document.getElementById(
        "sourceGateSummary"
    );

    const metrics = document.getElementById(
        "sourceGateMetrics"
    );

    const roomButton = document.getElementById(
        "testRoomButton"
    );

    const lockHint = document.getElementById(
        "roomLockHint"
    );

    const status = source
        ? source.status
        : null;

    gate.className = sourceGateClass(
        status
    );

    title.innerText = sourceGateTitle(
        status
    );

    if (!source) {
        summary.innerText =
            "Run Source Qualification near the router before testing rooms.";

        metrics.innerText = "";

    } else {
        summary.innerText =
            source.summary || "";

        metrics.innerText = [
            `RSSI median ${roomFmt(source.rssi_median, 0)} dBm`,
            `RSSI spread ${roomFmt(source.rssi_range)} dB`,
            `Router median ${roomFmt(source.router_ping_median)} ms`,
            `Router P90 ${roomFmt(source.router_ping_p90)} ms`,
            `Jitter median ${roomFmt(source.router_jitter_median)} ms`,
            `Jitter P90 ${roomFmt(source.router_jitter_p90)} ms`,
            `HTTP ${roomFmt(source.http_success_rate, 0)}%`,
            `Healthy WAN rounds ${source.healthy_public_rounds ?? "---"}/${source.rounds ?? "---"}`
        ].join(" · ");
    }

    roomButton.disabled = !ready;

    lockHint.innerText = ready
        ? "Source is qualified. Room testing is enabled."
        : "Locked until the source is qualified as stable.";
}


function renderRoomTests(payload) {
    const root = document.getElementById(
        "roomResults"
    );

    const tests = payload.tests || [];

    if (!tests.length) {
        root.innerHTML =
            '<div class="empty">' +
            'No room tests yet.' +
            '</div>';
        return;
    }

    root.innerHTML = tests.map(test => {
        const comparison =
            test.comparison || {};

        const badge = roomBadge(
            comparison.status
        );

        const channel =
            test.channel === null ||
            test.channel === undefined
                ? ""
                : ` / Ch ${test.channel}`;

        const metrics = [
            `RSSI ${test.rssi ?? "---"} dBm`,
            `Router ${roomFmt(test.router_ping)} ms`,
            `Jitter ${roomFmt(test.router_jitter)} ms`,
            `Loss ${roomFmt(test.router_loss)}%`,
            `${escapeHtml(test.band || "Unknown")}${channel}`,
            `Link ${roomFmt(
                test.link_speed_mbps,
                0
            )} Mbps`
        ].join(" · ");

        return `
        <div class="room-result">
            <div class="room-result-head">
                <div class="room-result-name">
                    ${escapeHtml(test.label)}
                </div>

                <div
                    class="room-badge ${badge.cls}"
                >
                    ${badge.text}
                </div>
            </div>

            <div class="room-description">
                ${escapeHtml(
                    comparison.summary || "--"
                )}
            </div>

            <div class="room-metrics">
                ${metrics}
                <br>
                ${escapeHtml(test.timestamp || "")}
            </div>
        </div>`;
    }).join("");
}

function roomFmt(value, digits = 1) {
    if (
        value === null ||
        value === undefined
    ) {
        return "---";
    }

    const n = Number(value);

    if (!Number.isFinite(n)) {
        return "---";
    }

    return n.toFixed(digits);
}

async function loadRoomTests() {
    try {
        const response = await fetch(
            "/api/room-tests",
            {cache: "no-store"}
        );

        const payload =
            await response.json();

        updateSourceGate(payload);
        renderRoomTests(payload);

    } catch (error) {
        console.log(error);
    }
}


async function runRoomTest(role) {
    const status = document.getElementById("roomTestStatus");
    const baselineButton = document.getElementById("saveRoomBaseline");
    const roomButton = document.getElementById("testRoomButton");
    const progressWrap = document.getElementById("roomProgressWrap");
    const progressBar = document.getElementById("roomProgressBar");
    const progressLabel = document.getElementById("roomProgressLabel");

    let label = "Near router";

    if (role === "room") {
        label = document.getElementById("roomName").value.trim();

        if (!label) {
            status.innerText = "Enter a room name first.";
            return;
        }
    }

    baselineButton.disabled = true;
    roomButton.disabled = true;

    progressWrap.classList.add("show");
    progressBar.style.width = "0%";
    progressLabel.innerText = "Preparing test...";

    status.innerText =
        role === "baseline"
            ? "Source Qualification is running. Keep the phone still 1–2 m from the router."
            : `Keep the phone still in ${label}.`;

    try {
        const response = await fetch(
            "/api/room-test-progress",
            {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    role: role,
                    label: label
                })
            }
        );

        if (!response.ok) {
            const payload = await response.json();
            throw new Error(payload.error || "Room test failed");
        }

        if (!response.body || !response.body.getReader) {
            throw new Error("Streaming not supported");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let finalResult = null;

        while (true) {
            const {value, done} = await reader.read();

            if (done) break;

            buffer += decoder.decode(value, {stream: true});
            const lines = buffer.split("\n");
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.trim()) continue;

                const event = JSON.parse(line);

                const percent = Math.max(
                    0,
                    Math.min(100, Number(event.percent || 0))
                );

                progressBar.style.width = percent + "%";
                progressLabel.innerText = event.stage || "Testing...";

                if (event.type === "error") {
                    throw new Error(
                        event.message ||
                        "Could not enter TEST MODE"
                    );
                }

                if (event.type === "result") {
                    finalResult = event;
                }
            }
        }

        if (!finalResult) {
            throw new Error("No final result received");
        }

        progressBar.style.width = "100%";
        progressLabel.innerText = "Complete";

        const comparison = finalResult.comparison || {};

        if (role === "baseline") {
            const qualification =
                finalResult.qualification || {};

            status.innerText =
                qualification.summary ||
                "Source Qualification completed.";
        } else {
            status.innerText =
                `${label}: ${
                    comparison.summary ||
                    "Test completed."
                }`;
        }

        await loadRoomTests();

        setTimeout(() => {
            progressWrap.classList.remove("show");
        }, 1800);

    } catch (error) {
        progressBar.style.width = "0%";
        progressLabel.innerText = "Test failed";
        status.innerText =
            "Room test failed: " + (error.message || "unknown error");
    } finally {
        baselineButton.disabled = false;
        roomButton.disabled = false;
    }
}

document.getElementById(
    "saveRoomBaseline"
).addEventListener(
    "click",
    () => runRoomTest("baseline")
);

document.getElementById(
    "testRoomButton"
).addEventListener(
    "click",
    () => runRoomTest("room")
);

document.getElementById(
    "clearRoomTests"
).addEventListener(
    "click",
    async () => {
        const status =
            document.getElementById(
                "roomTestStatus"
            );

        try {
            await fetch(
                "/api/room-tests/clear",
                {method: "POST"}
            );

            status.innerText =
                "Room tests cleared.";

            await loadRoomTests();

        } catch (error) {
            status.innerText =
                "Could not clear room tests.";
        }
    }
);

async function update() {
    try {
        let [dataRes, eventRes, summaryRes] = await Promise.all([
            fetch("/api/data", {cache: "no-store"}),
            fetch("/api/events", {cache: "no-store"}),
            fetch("/api/summary", {cache: "no-store"})
        ]);

        let rows = await dataRes.json();
        let events = await eventRes.json();
        let summary = await summaryRes.json();

        if (!rows.length) return;

        let r = rows[rows.length - 1];
        let score = Number(r.score || 0);

        let scoreEl = document.getElementById("score");
        scoreEl.innerText = score + " / 100";
        scoreEl.className = "health-score " + scoreClass(score);

        let healthEl = document.getElementById("health");
        healthEl.innerText = r.health || "--";
        healthEl.className = "health-text " + scoreClass(score);

        document.getElementById("diagnosis").innerText =
            r.diagnosis || "--";

        document.getElementById("confidence").innerText =
            "Confidence: " +
            (r.confidence === null || r.confidence === undefined
                ? "--"
                : r.confidence + "%");

        document.getElementById("wan").innerText =
            r.wan_status || "--";

        document.getElementById("uptime").innerText =
            uptime(r.uptime);

        document.getElementById("routerPing").innerText =
            fmt(r.router_ping) + " ms";

        document.getElementById("routerJitter").innerText =
            fmt(r.router_jitter) + " ms";

        document.getElementById("netPing").innerText =
            fmt(r.net_ping) + " ms";

        document.getElementById("jitter").innerText =
            fmt(r.net_jitter) + " ms";

        document.getElementById("loss").innerText =
            fmt(r.net_loss) + " %";

        document.getElementById("traffic").innerText =
            fmt(r.download, 2) + " / " + fmt(r.upload, 2) + " Mbps";


        document.getElementById("wifiRssi").innerText =
            (r.wifi_rssi === null || r.wifi_rssi === undefined)
                ? "---"
                : r.wifi_rssi + " dBm";

        document.getElementById("wifiQuality").innerText =
            r.wifi_quality || "Unknown";

        document.getElementById("wifiBandChannel").innerText =
            (r.wifi_band || "Unknown") +
            (
                r.wifi_channel === null || r.wifi_channel === undefined
                    ? ""
                    : " / Ch " + r.wifi_channel
            );

        document.getElementById("wifiLinkSpeed").innerText =
            (r.wifi_link_speed_mbps === null || r.wifi_link_speed_mbps === undefined)
                ? "---"
                : r.wifi_link_speed_mbps + " Mbps";

        document.getElementById("wifiAdvice").innerText =
            wifiAdvice(r);

        setTarget(
            "cf",
            r.cloudflare_ping,
            r.cloudflare_jitter,
            r.cloudflare_loss
        );

        setTarget(
            "g",
            r.google_ping,
            r.google_jitter,
            r.google_loss
        );

        setTarget(
            "q",
            r.quad9_ping,
            r.quad9_jitter,
            r.quad9_loss
        );

        let httpOk = Number(r.http_ok) === 1;

        let httpState = document.getElementById("httpState");
        httpState.innerText = httpOk ? "OK" : "FAILED";
        httpState.className = httpOk ? "green" : "red";

        document.getElementById("httpLatency").innerText =
            fmt(r.http_latency, 0) + " ms";

        document.getElementById("availability").innerText =
            fmt(summary.availability, 2) + "%";

        document.getElementById("avgScore").innerText =
            fmt(summary.avg_score, 1);

        document.getElementById("minScore").innerText =
            fmt(summary.min_score, 0);

        document.getElementById("eventCount").innerText =
            summary.events ?? "--";

        document.getElementById("updated").innerText =
            "Last update: " + (r.timestamp || "--");

        drawChart(rows);
        renderEvents(events);

    } catch (error) {
        console.log(error);
    }
}

update();
loadRoomTests();
setInterval(update, 5000);
setInterval(loadRoomTests, 15000);

window.addEventListener("resize", update);
</script>

</body>
</html>
"""

    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=8080,
        debug=False,
        threaded=True
    )
