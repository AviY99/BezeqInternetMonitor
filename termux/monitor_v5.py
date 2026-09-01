import urllib.request
import xml.etree.ElementTree as ET
import subprocess
import sqlite3
import re
import time
import statistics
import json
import os
from datetime import datetime
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

ROUTER = "10.0.0.138"

TARGETS = {
    "Cloudflare": "1.1.1.1",
    "Google": "8.8.8.8",
    "Quad9": "9.9.9.9",
}

HTTP_CHECK_URL = "https://connectivitycheck.gstatic.com/generate_204"

BASE = "http://10.0.0.138:49152"
PPP_URL = BASE + "/417a6a61/upnp/control/WANPPPConn1"
IFC_URL = BASE + "/417a6a61/upnp/control/WANCommonIFC1"

PPP_SERVICE = "urn:schemas-upnp-org:service:WANPPPConnection:1"
IFC_SERVICE = "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1"

DB_PATH = "internet_monitor.db"
ROLLING_SAMPLES = 6

TEST_MODE_FILE = os.path.expanduser(
    "~/internet_monitor_test_mode.json"
)
TEST_MODE_ACK_FILE = os.path.expanduser(
    "~/internet_monitor_test_mode_ack.json"
)


def _read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def active_test_mode():
    data = _read_json_file(TEST_MODE_FILE)

    if not data:
        return None

    try:
        expires_at = float(data.get("expires_at", 0))
    except Exception:
        expires_at = 0

    if expires_at <= time.time():
        for path in (TEST_MODE_FILE, TEST_MODE_ACK_FILE):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except Exception:
                pass
        return None

    return data


def acknowledge_test_mode(mode):
    token = mode.get("token")

    if not token:
        return

    payload = {
        "token": token,
        "acknowledged_at": time.time()
    }

    temp_path = TEST_MODE_ACK_FILE + ".tmp"

    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

        os.replace(temp_path, TEST_MODE_ACK_FILE)
    except Exception:
        try:
            os.remove(temp_path)
        except Exception:
            pass


def show_test_mode(mode, message="Background monitoring paused"):
    label = mode.get("label") or "Wi-Fi room test"

    print("\033[2J\033[H", end="")
    print("=== Bezeq Monitor V5 / TEST MODE ===")
    print()
    print(message)
    print("Room test    :", label)
    print()
    print("Health samples and events are NOT being recorded.")
    print("Monitoring will resume automatically when the test ends.")
    print()




def soap(url, service, action):
    body = f'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:{action} xmlns:u="{service}"/>
</s:Body>
</s:Envelope>'''

    request = urllib.request.Request(
        url,
        data=body.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service}#{action}"'
        }
    )

    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read()


def xml_value(data, name):
    root = ET.fromstring(data)
    for element in root.iter():
        if element.tag.split("}")[-1] == name:
            return element.text
    return None


def get_counter(action):
    data = soap(IFC_URL, IFC_SERVICE, action)
    tags = {
        "GetTotalBytesReceived": "NewTotalBytesReceived",
        "GetTotalBytesSent": "NewTotalBytesSent"
    }
    return int(xml_value(data, tags[action]) or 0)


def ping(host, count=3):
    try:
        process = subprocess.run(
            ["ping", "-c", str(count), "-W", "2", host],
            capture_output=True,
            text=True,
            timeout=12
        )

        output = process.stdout + process.stderr
        times = [
            float(x)
            for x in re.findall(r'time[=<]([\d.]+)', output)
        ]

        loss_match = re.search(r'([\d.]+)% packet loss', output)
        loss = float(loss_match.group(1)) if loss_match else 100.0

        if not times:
            return {"ping": None, "jitter": None, "loss": loss}

        avg = sum(times) / len(times)

        if len(times) > 1:
            jitter = sum(
                abs(times[i] - times[i - 1])
                for i in range(1, len(times))
            ) / (len(times) - 1)
        else:
            jitter = 0.0

        return {"ping": avg, "jitter": jitter, "loss": loss}

    except Exception:
        return {"ping": None, "jitter": None, "loss": 100.0}


def http_check():
    started = time.monotonic()

    try:
        request = urllib.request.Request(
            HTTP_CHECK_URL,
            headers={"User-Agent": "BezeqMonitor/4.0"}
        )

        with urllib.request.urlopen(request, timeout=5) as response:
            response.read(16)
            latency = (time.monotonic() - started) * 1000
            return {
                "ok": response.status in (200, 204),
                "latency": latency,
                "status": response.status,
            }

    except Exception:
        return {"ok": False, "latency": None, "status": None}



def wifi_channel(frequency_mhz):
    if frequency_mhz is None:
        return None

    try:
        f = int(frequency_mhz)
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


def wifi_band(frequency_mhz):
    if frequency_mhz is None:
        return "Unknown"

    try:
        f = int(frequency_mhz)
    except Exception:
        return "Unknown"

    if 2400 <= f < 2500:
        return "2.4 GHz"

    if 4900 <= f < 5900:
        return "5 GHz"

    if 5925 <= f <= 7125:
        return "6 GHz"

    return "Unknown"


def wifi_quality(rssi):
    if rssi is None:
        return "Unknown"

    try:
        r = int(rssi)
    except Exception:
        return "Unknown"

    if r >= -55:
        return "Excellent"

    if r >= -65:
        return "Good"

    if r >= -72:
        return "Fair"

    if r >= -80:
        return "Weak"

    return "Very weak"


def get_wifi_info():
    try:
        process = subprocess.run(
            ["termux-wifi-connectioninfo"],
            capture_output=True,
            text=True,
            timeout=6
        )

        if process.returncode != 0 or not process.stdout.strip():
            return {
                "rssi": None,
                "frequency_mhz": None,
                "link_speed_mbps": None,
                "band": "Unknown",
                "channel": None,
                "quality": "Unknown",
            }

        data = json.loads(process.stdout)

        rssi = data.get("rssi")
        frequency = data.get("frequency_mhz")
        link_speed = data.get("link_speed_mbps")

        return {
            "rssi": rssi,
            "frequency_mhz": frequency,
            "link_speed_mbps": link_speed,
            "band": wifi_band(frequency),
            "channel": wifi_channel(frequency),
            "quality": wifi_quality(rssi),
        }

    except Exception:
        return {
            "rssi": None,
            "frequency_mhz": None,
            "link_speed_mbps": None,
            "band": "Unknown",
            "channel": None,
            "quality": "Unknown",
        }


def safe_median(values):
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return float(statistics.median(clean))


def rolling_average(history, field):
    values = [
        item[field]
        for item in history
        if item[field] is not None
    ]
    if not values:
        return None
    return sum(values) / len(values)


def classify_target(result):
    if result["ping"] is None or result["loss"] >= 100:
        return "DOWN"
    if result["loss"] >= 20:
        return "BAD"
    if result["loss"] > 0:
        return "LOSS"
    if result["jitter"] is not None and result["jitter"] >= 30:
        return "JITTER"
    if result["ping"] is not None and result["ping"] >= 120:
        return "SLOW"
    return "OK"


def diagnose(
    wan_status,
    router_ping,
    router_jitter,
    router_loss,
    target_results,
    http_result,
    wifi
):
    if wan_status != "Connected":
        return "WAN disconnected", 100

    rssi = wifi.get("rssi")
    band = wifi.get("band")
    quality = wifi.get("quality")

    if router_ping is None or router_loss >= 20:
        if rssi is not None and rssi <= -75:
            return "Weak Wi-Fi signal causing local network problems", 99
        return "Local Wi-Fi/LAN problem", 98

    if router_loss >= 5:
        if rssi is not None and rssi <= -72:
            return "Wi-Fi packet loss with weak/fair signal", 98
        return "Local Wi-Fi/LAN packet loss", 95

    # Wi-Fi-aware local diagnosis.
    if rssi is not None:
        if rssi <= -75 and (
            router_ping >= 20
            or (router_jitter is not None and router_jitter >= 15)
        ):
            return "Weak Wi-Fi signal is degrading the local connection", 98

        if -75 < rssi <= -66 and (
            router_ping >= 25
            or (router_jitter is not None and router_jitter >= 20)
        ):
            return "Fair Wi-Fi signal with elevated local latency/jitter", 94

    if (
        band == "2.4 GHz"
        and router_ping >= 25
        and router_jitter is not None
        and router_jitter >= 20
    ):
        return "Likely 2.4 GHz Wi-Fi congestion/interference", 93

    if router_ping >= 45 or (
        router_jitter is not None and router_jitter >= 25
    ):
        if quality in ("Excellent", "Good"):
            return "Wi-Fi congestion/interference despite good signal", 92
        return "Local Wi-Fi/LAN latency is high", 90

    states = {
        name: classify_target(result)
        for name, result in target_results.items()
    }

    bad_names = [
        name for name, state in states.items()
        if state in ("DOWN", "BAD")
    ]

    degraded_names = [
        name for name, state in states.items()
        if state in ("LOSS", "JITTER", "SLOW")
    ]

    healthy_names = [
        name for name, state in states.items()
        if state == "OK"
    ]

    if len(bad_names) >= 2 and not http_result["ok"]:
        return "WAN / ISP instability", 97

    if len(bad_names) >= 2:
        return "Internet path instability", 92

    if len(bad_names) == 1 and len(healthy_names) >= 1 and http_result["ok"]:
        return f"Route to {bad_names[0]} is unstable", 88

    if not http_result["ok"] and len(healthy_names) >= 2:
        return "HTTP/DNS connectivity issue", 85

    if len(degraded_names) >= 2:
        return "Internet latency/jitter is elevated", 82

    if len(degraded_names) == 1 and len(healthy_names) >= 2:
        return f"Minor issue on {degraded_names[0]} path", 76

    if router_ping >= 25:
        if band == "2.4 GHz":
            return "Internet OK; 2.4 GHz Wi-Fi latency elevated", 84
        return "Internet OK; local Wi-Fi latency elevated", 80

    return "Connection looks stable", 94


def calculate_score(
    wan_status,
    router_ping,
    router_jitter,
    router_loss,
    net_ping,
    net_jitter,
    net_loss,
    target_results,
    http_ok
):
    if wan_status != "Connected":
        return 0

    score = 100.0

    if router_ping is None:
        score -= 40
    elif router_ping > 60:
        score -= 25
    elif router_ping > 40:
        score -= 15
    elif router_ping > 25:
        score -= 8
    elif router_ping > 12:
        score -= 3

    if router_jitter is not None:
        if router_jitter > 35:
            score -= 12
        elif router_jitter > 20:
            score -= 6

    if router_loss is not None:
        score -= min(router_loss * 2.5, 30)

    if net_ping is None:
        score -= 35
    elif net_ping > 150:
        score -= 25
    elif net_ping > 100:
        score -= 15
    elif net_ping > 60:
        score -= 8
    elif net_ping > 40:
        score -= 3

    if net_jitter is not None:
        if net_jitter > 50:
            score -= 18
        elif net_jitter > 30:
            score -= 10
        elif net_jitter > 18:
            score -= 5

    if net_loss is not None:
        score -= min(net_loss * 2.5, 35)

    states = [classify_target(r) for r in target_results.values()]
    hard_bad = sum(1 for s in states if s in ("DOWN", "BAD"))

    if hard_bad >= 2:
        score -= 18
    elif hard_bad == 1:
        score -= 5

    if not http_ok:
        score -= 12

    return max(0, min(100, round(score)))


def health_from_score(score):
    if score >= 90:
        return "EXCELLENT"
    if score >= 75:
        return "STABLE"
    if score >= 55:
        return "DEGRADED"
    if score >= 30:
        return "POOR"
    return "CRITICAL"


def fmt(value):
    if value is None:
        return "---"
    return f"{value:.1f}"


def ensure_columns(db):
    existing = {
        row[1]
        for row in db.execute("PRAGMA table_info(measurements)")
    }

    additions = {
        "cloudflare_ping": "REAL",
        "cloudflare_jitter": "REAL",
        "cloudflare_loss": "REAL",
        "google_ping": "REAL",
        "google_jitter": "REAL",
        "google_loss": "REAL",
        "quad9_ping": "REAL",
        "quad9_jitter": "REAL",
        "quad9_loss": "REAL",
        "http_ok": "INTEGER",
        "http_latency": "REAL",
        "diagnosis": "TEXT",
        "confidence": "INTEGER",
        "wifi_rssi": "INTEGER",
        "wifi_frequency_mhz": "INTEGER",
        "wifi_link_speed_mbps": "INTEGER",
        "wifi_band": "TEXT",
        "wifi_channel": "INTEGER",
        "wifi_quality": "TEXT",
    }

    for name, sql_type in additions.items():
        if name not in existing:
            db.execute(
                f"ALTER TABLE measurements ADD COLUMN {name} {sql_type}"
            )

    db.commit()


db = sqlite3.connect(DB_PATH, timeout=10)
db.execute("PRAGMA journal_mode=WAL")

db.execute('''
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    wan_status TEXT,
    uptime INTEGER,
    download REAL,
    upload REAL,
    router_ping REAL,
    router_jitter REAL,
    router_loss REAL,
    net_ping REAL,
    net_jitter REAL,
    net_loss REAL,
    score INTEGER,
    health TEXT
)
''')

ensure_columns(db)

history = deque(maxlen=ROLLING_SAMPLES)

last_rx = get_counter("GetTotalBytesReceived")
last_tx = get_counter("GetTotalBytesSent")
last_counter_time = time.monotonic()
previous_uptime = None

while True:
    try:
        test_mode = active_test_mode()

        if test_mode:
            acknowledge_test_mode(test_mode)
            show_test_mode(test_mode)
            time.sleep(1)
            continue

        status_xml = soap(
            PPP_URL,
            PPP_SERVICE,
            "GetStatusInfo"
        )

        wan_status = xml_value(
            status_xml,
            "NewConnectionStatus"
        )

        uptime = int(
            xml_value(status_xml, "NewUptime") or 0
        )

        reconnect = (
            previous_uptime is not None
            and uptime < previous_uptime
        )
        previous_uptime = uptime

        rx = get_counter("GetTotalBytesReceived")
        tx = get_counter("GetTotalBytesSent")

        now_counter = time.monotonic()
        elapsed = max(now_counter - last_counter_time, 0.1)

        delta_rx = rx - last_rx
        delta_tx = tx - last_tx

        if delta_rx < 0:
            delta_rx += 2**32
        if delta_tx < 0:
            delta_tx += 2**32

        download = delta_rx * 8 / elapsed / 1_000_000
        upload = delta_tx * 8 / elapsed / 1_000_000

        last_rx = rx
        last_tx = tx
        last_counter_time = now_counter

        jobs = {"Router": ROUTER}
        jobs.update(TARGETS)

        ping_results = {}

        with ThreadPoolExecutor(max_workers=4) as executor:
            future_map = {
                executor.submit(
                    ping,
                    host,
                    5 if name == "Router" else 3
                ): name
                for name, host in jobs.items()
            }

            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    ping_results[name] = future.result()
                except Exception:
                    ping_results[name] = {
                        "ping": None,
                        "jitter": None,
                        "loss": 100.0,
                    }

        router_result = ping_results["Router"]

        target_results = {
            name: ping_results[name]
            for name in TARGETS
        }

        http_result = http_check()
        wifi = get_wifi_info()

        current_net_ping = safe_median(
            [result["ping"] for result in target_results.values()]
        )
        current_net_jitter = safe_median(
            [result["jitter"] for result in target_results.values()]
        )
        current_net_loss = safe_median(
            [result["loss"] for result in target_results.values()]
        )

        test_mode = active_test_mode()

        if test_mode:
            acknowledge_test_mode(test_mode)
            show_test_mode(
                test_mode,
                "TEST MODE started; current background sample discarded"
            )
            time.sleep(1)
            continue

        history.append({
            "router_ping": router_result["ping"],
            "router_jitter": router_result["jitter"],
            "router_loss": router_result["loss"],
            "net_ping": current_net_ping,
            "net_jitter": current_net_jitter,
            "net_loss": current_net_loss,
        })

        stable_router_ping = rolling_average(history, "router_ping")
        stable_router_jitter = rolling_average(history, "router_jitter")
        stable_router_loss = rolling_average(history, "router_loss")
        stable_net_ping = rolling_average(history, "net_ping")
        stable_net_jitter = rolling_average(history, "net_jitter")
        stable_net_loss = rolling_average(history, "net_loss")

        diagnosis, confidence = diagnose(
            wan_status,
            stable_router_ping,
            stable_router_jitter,
            stable_router_loss,
            target_results,
            http_result,
            wifi
        )

        score = calculate_score(
            wan_status,
            stable_router_ping,
            stable_router_jitter,
            stable_router_loss,
            stable_net_ping,
            stable_net_jitter,
            stable_net_loss,
            target_results,
            http_result["ok"]
        )

        health = health_from_score(score)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        db.execute('''
        INSERT INTO measurements (
            timestamp,
            wan_status,
            uptime,
            download,
            upload,
            router_ping,
            router_jitter,
            router_loss,
            net_ping,
            net_jitter,
            net_loss,
            score,
            health,
            cloudflare_ping,
            cloudflare_jitter,
            cloudflare_loss,
            google_ping,
            google_jitter,
            google_loss,
            quad9_ping,
            quad9_jitter,
            quad9_loss,
            http_ok,
            http_latency,
            diagnosis,
            confidence,
            wifi_rssi,
            wifi_frequency_mhz,
            wifi_link_speed_mbps,
            wifi_band,
            wifi_channel,
            wifi_quality
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?
        )
        ''', (
            timestamp,
            wan_status,
            uptime,
            download,
            upload,
            stable_router_ping,
            stable_router_jitter,
            stable_router_loss,
            stable_net_ping,
            stable_net_jitter,
            stable_net_loss,
            score,
            health,
            target_results["Cloudflare"]["ping"],
            target_results["Cloudflare"]["jitter"],
            target_results["Cloudflare"]["loss"],
            target_results["Google"]["ping"],
            target_results["Google"]["jitter"],
            target_results["Google"]["loss"],
            target_results["Quad9"]["ping"],
            target_results["Quad9"]["jitter"],
            target_results["Quad9"]["loss"],
            1 if http_result["ok"] else 0,
            http_result["latency"],
            diagnosis,
            confidence,
            wifi.get("rssi"),
            wifi.get("frequency_mhz"),
            wifi.get("link_speed_mbps"),
            wifi.get("band"),
            wifi.get("channel"),
            wifi.get("quality")
        ))

        db.commit()

        days = uptime // 86400
        hours = (uptime % 86400) // 3600
        minutes = (uptime % 3600) // 60

        print("\033[2J\033[H", end="")
        print("=== Bezeq Monitor V5 / Wi-Fi Diagnostics ===")
        print()
        print("Time        :", timestamp)
        print("WAN         :", wan_status)
        print(f"Uptime      : {days}d {hours:02}h {minutes:02}m")

        if reconnect:
            print("WAN RECONNECT DETECTED")

        print()
        print(f"Traffic down: {download:.2f} Mbps")
        print(f"Traffic up  : {upload:.2f} Mbps")

        print()
        print("LOCAL NETWORK")
        print("Router ping :", fmt(stable_router_ping), "ms")
        print("Router jit. :", fmt(stable_router_jitter), "ms")
        print("Router loss :", fmt(stable_router_loss), "%")

        print()
        print("WI-FI RADIO")
        print("RSSI        :", wifi.get("rssi"), "dBm")
        print("Quality     :", wifi.get("quality"))
        print("Band        :", wifi.get("band"))
        print("Channel     :", wifi.get("channel"))
        print("Link speed  :", wifi.get("link_speed_mbps"), "Mbps")

        print()
        print("PUBLIC TARGETS")

        for name in ("Cloudflare", "Google", "Quad9"):
            result = target_results[name]
            state = classify_target(result)
            print(
                f"{name:<10}: "
                f"{fmt(result['ping']):>6} ms  "
                f"jit {fmt(result['jitter']):>6}  "
                f"loss {fmt(result['loss']):>5}%  "
                f"{state}"
            )

        print()
        print("Internet ping :", fmt(stable_net_ping), "ms")
        print("Internet jit. :", fmt(stable_net_jitter), "ms")
        print("Internet loss :", fmt(stable_net_loss), "%")

        print()
        print(
            "HTTP check    :",
            "OK" if http_result["ok"] else "FAILED",
            (
                f"({http_result['latency']:.0f} ms)"
                if http_result["latency"] is not None
                else ""
            )
        )

        print()
        print("Samples       :", len(history), "/", ROLLING_SAMPLES)
        print("SCORE         :", score, "/ 100")
        print("STATUS        :", health)
        print("DIAGNOSIS     :", diagnosis)
        print("CONFIDENCE    :", confidence, "%")

        if len(history) < ROLLING_SAMPLES:
            print()
            print("Collecting stability data...")

        print()
        print("Ctrl+C to stop")

        time.sleep(3)

    except KeyboardInterrupt:
        print("\nStopped.")
        db.close()
        break

    except Exception as error:
        print("Monitor error:", error)
        time.sleep(5)
