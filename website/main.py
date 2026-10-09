import asyncio
import json
import sqlite3
from datetime import datetime
from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import paho.mqtt.client as mqtt

DB_FILE = "power_meter.db"
HIVEMQ_BROKER = "broker.hivemq.com"
HIVEMQ_PORT = 1883
HIVEMQ_TOPIC = "elevate2026/powermeter/telemetry"
DAYA_KONTRAK_PLN = 900.0

app = FastAPI(title="Smart Power Meter - HiveMQ Cloud")

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            voltage REAL,
            current REAL,
            power REAL,
            apparent_power REAL,
            pf REAL,
            saldo_kwh REAL,
            runway_hours REAL,
            q1 REAL,
            q2 REAL,
            q3 REAL,
            q4 REAL,
            recom_mode TEXT,
            recom_message TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def evaluate_recommendation(data: dict) -> dict:
    power = data.get("power", 0.0)
    apparent_power = data.get("apparent_power", 0.0)
    saldo = data.get("saldo_kwh", 0.0)
    runway = data.get("runway_hours", 999.0)
    q1 = data.get("q1", 0.0)
    total_24h = q1 + data.get("q2", 0.0) + data.get("q3", 0.0) + data.get("q4", 0.0)
    current_hour = datetime.now().hour

    # Proteksi MCB (Daya Semu mendekati kapasitas VA kontrak)
    if apparent_power >= (0.85 * DAYA_KONTRAK_PLN):
        excess = apparent_power - (0.75 * DAYA_KONTRAK_PLN)
        return {
            "mode": "PEAK_SHAVING",
            "level": "danger",
            "badge": "BAHAYA: BEBAN KRITIS MCB",
            "message": f"Daya Semu ({apparent_power:.0f} VA) mendekati batas MCB {DAYA_KONTRAK_PLN:.0f} VA! Kurangi beban sebesar {excess:.0f} VA untuk mencegah jeglek."
        }

    if (saldo < q1) and (current_hour >= 21 or current_hour <= 5):
        hours_left = (7 - current_hour) if current_hour <= 5 else (7 + 24 - current_hour)
        safe_cap_w = (saldo * 1000.0) / max(1.0, hours_left)
        return {
            "mode": "RUNWAY_EXTENDER",
            "level": "warn",
            "badge": "PERINGATAN: RISIKO LISTRIK PADAM",
            "message": f"Saldo ({saldo:.2f} kWh) tidak cukup sampai pagi. Batasi daya di bawah {safe_cap_w:.0f} W agar listrik bertahan hingga 07:00."
        }

    if saldo <= 2.0:
        daily_est = max(1.0, total_24h)
        estimasi_biaya_mingguan = daily_est * 7 * 1352.0
        token_nominal = "Rp 50.000" if estimasi_biaya_mingguan > 35000 else "Rp 20.000"
        return {
            "mode": "TOP_UP_ADVISOR",
            "level": "info",
            "badge": "SARAN: ISI ULANG TOKEN",
            "message": f"Saldo kritis ({saldo:.2f} kWh). Berdasarkan proyeksi TinyML ({daily_est:.2f} kWh/hari), disarankan membeli token {token_nominal}."
        }

    return {
        "mode": "NORMAL",
        "level": "success",
        "badge": "STATUS: OPTIMAL",
        "message": f"Konsumsi listrik terkontrol. Berdasarkan proyeksi TinyML, saldo kuota diproyeksikan aman untuk {runway:.1f} jam ke depan."
    }

def save_to_db(data: dict, recom: dict):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO telemetry (
            timestamp, voltage, current, power, apparent_power, pf, saldo_kwh, runway_hours, 
            q1, q2, q3, q4, recom_mode, recom_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        data.get("voltage", 0.0),
        data.get("current", 0.0),
        data.get("power", 0.0),
        data.get("apparent_power", 0.0),
        data.get("pf", 0.0),
        data.get("saldo_kwh", 0.0),
        data.get("runway_hours", 0.0),
        data.get("q1", 0.0),
        data.get("q2", 0.0),
        data.get("q3", 0.0),
        data.get("q4", 0.0),
        recom.get("mode", ""),
        recom.get("message", "")
    ))
    conn.commit()
    conn.close()

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()
loop = asyncio.get_event_loop()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe(HIVEMQ_TOPIC)

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode("utf-8"))
        data["timestamp"] = datetime.now().strftime("%H:%M:%S")
        recommendation = evaluate_recommendation(data)
        data["recommendation"] = recommendation
        save_to_db(data, recommendation)
        asyncio.run_coroutine_threadsafe(manager.broadcast(json.dumps(data)), loop)
    except Exception as e:
        print(f"[ERROR] {e}")

mqtt_client = mqtt.Client(client_id=f"FastAPI_Listener_{datetime.now().timestamp()}")
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

@app.on_event("startup")
def startup_event():
    try:
        mqtt_client.connect(HIVEMQ_BROKER, HIVEMQ_PORT, 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"[MQTT ERROR] {e}")

@app.on_event("shutdown")
def shutdown_event():
    mqtt_client.loop_stop()
    mqtt_client.disconnect()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    return """
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Smart Power Meter - ELEVATE 2026</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            :root {
                --bg: #0b1329; --card: #17233f; --text: #f1f5f9;
                --primary: #38bdf8; --success: #34d399; --warn: #fbbf24; --danger: #f87171;
            }
            body { font-family: 'Segoe UI', Tahoma, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 24px; }
            .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #293859; padding-bottom: 16px; margin-bottom: 24px; }
            .header h1 { margin: 0; font-size: 24px; color: var(--primary); }
            .badge { padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 600; background: #064e3b; color: #6ee7b7; }
            
            .recom-card { background: #1e293b; border-radius: 12px; padding: 18px 24px; margin-bottom: 24px; border-left: 6px solid var(--primary); box-shadow: 0 4px 10px rgba(0,0,0,0.3); }
            .recom-header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
            .recom-tag { padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: bold; }
            .recom-tag.danger { background: #7f1d1d; color: #fca5a5; }
            .recom-tag.warn { background: #78350f; color: #fde68a; }
            .recom-tag.info { background: #1e3a8a; color: #93c5fd; }
            .recom-tag.success { background: #064e3b; color: #6ee7b7; }
            .recom-msg { font-size: 15px; color: #f8fafc; }

            .grid-5 { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 20px; }
            .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 20px; margin-bottom: 20px; }
            .card { background: var(--card); padding: 20px; border-radius: 12px; border: 1px solid #293859; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
            .card-title { font-size: 13px; text-transform: uppercase; color: #94a3b8; margin-bottom: 8px; font-weight: 600; }
            .card-val { font-size: 26px; font-weight: bold; color: var(--primary); }
            .card-val small { font-size: 14px; font-weight: normal; color: #cbd5e1; }
            
            .tinyml-container { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 16px; }
            .horizon-card { background: #0b1329; padding: 14px 8px; border-radius: 8px; text-align: center; border: 1px solid #293859; }
            .horizon-title { font-size: 12px; color: #94a3b8; }
            .horizon-val { font-size: 18px; font-weight: bold; color: var(--warn); margin-top: 6px; }
            .chart-wrapper { height: 260px; position: relative; }
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h1>Smart Power Meter & Edge-AI Predictor</h1>
                <small style="color: #94a3b8;">HiveMQ Cloud Broker & Prescriptive Recommender Engine</small>
            </div>
            <div id="statusBadge" class="badge">Menghubungkan HiveMQ...</div>
        </div>

        <div class="recom-card" id="recomCard">
            <div class="recom-header">
                <span class="recom-tag success" id="recomTag">SISTEM AKTIF</span>
                <span style="font-weight: 600; color: #cbd5e1;">Rekomendasi Tindakan:</span>
            </div>
            <div class="recom-msg" id="recomMsg">Menunggu data telemetri pertama...</div>
        </div>

        <div class="grid-5">
            <div class="card">
                <div class="card-title">Tegangan</div>
                <div class="card-val" id="valVoltage">0.00 <small>V</small></div>
            </div>
            <div class="card">
                <div class="card-title">Arus Efektif</div>
                <div class="card-val" id="valCurrent">0.000 <small>A</small></div>
            </div>
            <div class="card">
                <div class="card-title">Daya Aktif Nyata</div>
                <div class="card-val" id="valPower" style="color: var(--success);">0.00 <small>Watt</small></div>
            </div>
            <div class="card">
                <div class="card-title">Daya Semu (Apparent)</div>
                <div class="card-val" id="valApparent" style="color: #a78bfa;">0.00 <small>VA</small></div>
            </div>
            <div class="card">
                <div class="card-title">Faktor Daya</div>
                <div class="card-val" id="valPf">0.00 <small>%</small></div>
            </div>
        </div>

        <div class="grid-2">
            <div class="card">
                <div style="display: flex; justify-content: space-between;">
                    <div>
                        <div class="card-title">Sisa Saldo Token</div>
                        <div class="card-val" id="valSaldo" style="color: #60a5fa;">0.0000 <small>kWh</small></div>
                    </div>
                    <div style="text-align: right;">
                        <div class="card-title">Estimasi Runway Saldo</div>
                        <div class="card-val" id="valRunway" style="color: var(--warn);">0.0 <small>Jam</small></div>
                    </div>
                </div>
                <hr style="border-color: #293859; margin: 20px 0;">
                <div class="card-title">Proyeksi Konsumsi TinyML (AR-MLP C++ Native)</div>
                <div class="tinyml-container">
                    <div class="horizon-card">
                        <div class="horizon-title">Q1 (0-6 Jam)</div>
                        <div class="horizon-val" id="valQ1">0.000 <small>kWh</small></div>
                    </div>
                    <div class="horizon-card">
                        <div class="horizon-title">Q2 (6-12 Jam)</div>
                        <div class="horizon-val" id="valQ2">0.000 <small>kWh</small></div>
                    </div>
                    <div class="horizon-card">
                        <div class="horizon-title">Q3 (12-18 Jam)</div>
                        <div class="horizon-val" id="valQ3">0.000 <small>kWh</small></div>
                    </div>
                    <div class="horizon-card">
                        <div class="horizon-title">Q4 (18-24 Jam)</div>
                        <div class="horizon-val" id="valQ4">0.000 <small>kWh</small></div>
                    </div>
                </div>
            </div>

            <div class="card">
                <div class="card-title">Grafik Real-Time Daya Listrik (Watt vs VA)</div>
                <div class="chart-wrapper">
                    <canvas id="powerChart"></canvas>
                </div>
            </div>
        </div>

        <script>
            const ctx = document.getElementById('powerChart').getContext('2d');
            const powerChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [
                        { label: 'Daya Aktif (Watt)', borderColor: '#38bdf8', data: [], borderWidth: 2, tension: 0.3 },
                        { label: 'Daya Semu (VA)', borderColor: '#a78bfa', borderDash: [5, 5], data: [], borderWidth: 2, tension: 0.3 }
                    ]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    scales: {
                        x: { grid: { color: '#293859' }, ticks: { color: '#94a3b8' } },
                        y: { grid: { color: '#293859' }, ticks: { color: '#94a3b8' }, beginAtZero: true }
                    },
                    plugins: { legend: { labels: { color: '#f8fafc' } } }
                }
            });

            const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            let ws = new WebSocket(`${wsProtocol}//${window.location.host}/ws`);

            ws.onopen = () => {
                document.getElementById('statusBadge').innerText = 'Terkoneksi Real-Time';
                document.getElementById('statusBadge').style.background = '#064e3b';
            };

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                document.getElementById('valVoltage').innerHTML = `${data.voltage.toFixed(2)} <small>V</small>`;
                document.getElementById('valCurrent').innerHTML = `${data.current.toFixed(3)} <small>A</small>`;
                document.getElementById('valPower').innerHTML = `${data.power.toFixed(2)} <small>W</small>`;
                document.getElementById('valApparent').innerHTML = `${data.apparent_power.toFixed(2)} <small>VA</small>`;
                document.getElementById('valPf').innerHTML = `${data.pf.toFixed(2)} <small>%</small>`;
                document.getElementById('valSaldo').innerHTML = `${data.saldo_kwh.toFixed(4)} <small>kWh</small>`;
                document.getElementById('valRunway').innerHTML = `${data.runway_hours.toFixed(1)} <small>Jam</small>`;

                document.getElementById('valQ1').innerHTML = `${data.q1.toFixed(3)} <small>kWh</small>`;
                document.getElementById('valQ2').innerHTML = `${data.q2.toFixed(3)} <small>kWh</small>`;
                document.getElementById('valQ3').innerHTML = `${data.q3.toFixed(3)} <small>kWh</small>`;
                document.getElementById('valQ4').innerHTML = `${data.q4.toFixed(3)} <small>kWh</small>`;

                if (data.recommendation) {
                    const recom = data.recommendation;
                    const card = document.getElementById('recomCard');
                    const tag = document.getElementById('recomTag');
                    tag.className = `recom-tag ${recom.level}`;
                    tag.innerText = recom.badge;
                    document.getElementById('recomMsg').innerText = recom.message;
                    card.style.borderLeftColor = recom.level === 'danger' ? 'var(--danger)' : (recom.level === 'warn' ? 'var(--warn)' : 'var(--primary)');
                }

                if (powerChart.data.labels.length > 25) {
                    powerChart.data.labels.shift();
                    powerChart.data.datasets[0].data.shift();
                    powerChart.data.datasets[1].data.shift();
                }
                powerChart.data.labels.push(data.timestamp);
                powerChart.data.datasets[0].data.push(data.power);
                powerChart.data.datasets[1].data.push(data.apparent_power);
                powerChart.update();
            };

            ws.onclose = () => {
                document.getElementById('statusBadge').innerText = 'Terputus';
                document.getElementById('statusBadge').style.background = '#7f1d1d';
                setTimeout(() => location.reload(), 3000);
            };
        </script>
    </body>
    </html>
    """