#!/usr/bin/env python3
"""
TCX Dashboard Server
--------------------
Servidor Flask que expone los datos de workouts.db como API JSON
y sirve el dashboard en el navegador.

Uso:
    python dashboard_server.py          # arranca en http://localhost:5000
    python dashboard_server.py --port 8080
"""

import sqlite3
import json
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, render_template_string, send_from_directory

# ── Config ───────────────────────────────────────────
# En Docker los datos viven en /data; en local usa el directorio actual
DB_PATH   = os.environ.get("DB_PATH", "/data/workouts.db" if os.path.isdir("/data") else "workouts.db")
TEMPLATES = Path(__file__).parent / "templates"

app = Flask(__name__)

# ── DB helper ────────────────────────────────────────
def query(sql, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

def scalar(sql, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r else None

# ── Utilidades ───────────────────────────────────────
def fmt_pace(sec_per_km):
    if not sec_per_km:
        return "--"
    m, s = divmod(int(sec_per_km), 60)
    return f"{m}:{s:02d}"

def fmt_time(seconds):
    if not seconds:
        return "--"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ─────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────

@app.route("/api/summary")
def api_summary():
    """Tarjetas de resumen rápido."""
    total_workouts = scalar("SELECT COUNT(*) FROM workouts") or 0
    total_km       = scalar("SELECT SUM(distance_m)/1000.0 FROM workouts") or 0
    total_time_h   = scalar("SELECT SUM(total_time_sec)/3600.0 FROM workouts") or 0
    total_cal      = scalar("SELECT SUM(calories) FROM workouts") or 0

    # Mes actual
    this_month = datetime.utcnow().strftime("%Y-%m")
    km_this_month = scalar(
        "SELECT SUM(distance_m)/1000.0 FROM workouts WHERE strftime('%Y-%m', start_time)=?",
        (this_month,)
    ) or 0
    runs_this_month = scalar(
        "SELECT COUNT(*) FROM workouts WHERE strftime('%Y-%m', start_time)=?",
        (this_month,)
    ) or 0

    # Última salida
    last = query("""
        SELECT start_time, distance_m, total_time_sec, avg_hr, avg_pace_sec_km, sport
        FROM workouts ORDER BY start_time DESC LIMIT 1
    """)
    last = last[0] if last else {}

    # Racha de semanas con al menos 1 salida
    weeks = query("""
        SELECT DISTINCT strftime('%Y-%W', start_time) as wk
        FROM workouts ORDER BY wk DESC
    """)
    streak = 0
    if weeks:
        now_wk = datetime.utcnow().strftime("%Y-%W")
        expected = now_wk
        for w in weeks:
            if w["wk"] == expected:
                streak += 1
                # retroceder una semana
                dt = datetime.strptime(expected + " 1", "%Y-%W %w")
                expected = (dt - timedelta(weeks=1)).strftime("%Y-%W")
            else:
                break

    return jsonify({
        "total_workouts": total_workouts,
        "total_km": round(total_km, 1),
        "total_time_h": round(total_time_h, 1),
        "total_cal": int(total_cal),
        "km_this_month": round(km_this_month, 1),
        "runs_this_month": runs_this_month,
        "streak_weeks": streak,
        "last": {
            "date":     last.get("start_time", "")[:10] if last else "",
            "sport":    last.get("sport", ""),
            "dist_km":  round((last.get("distance_m") or 0) / 1000, 2),
            "time":     fmt_time(last.get("total_time_sec")),
            "avg_hr":   last.get("avg_hr"),
            "pace":     fmt_pace(last.get("avg_pace_sec_km")),
        }
    })


@app.route("/api/workouts")
def api_workouts():
    """Lista de todas las salidas para la tabla."""
    rows = query("""
        SELECT id, sport, start_time, distance_m, total_time_sec,
               avg_hr, max_hr, avg_pace_sec_km, calories, avg_cadence
        FROM workouts ORDER BY start_time DESC
    """)
    for r in rows:
        r["dist_km"]   = round((r["distance_m"] or 0) / 1000, 2)
        r["pace_fmt"]  = fmt_pace(r["avg_pace_sec_km"])
        r["time_fmt"]  = fmt_time(r["total_time_sec"])
        r["date"]      = r["start_time"][:10] if r["start_time"] else ""
    return jsonify(rows)


@app.route("/api/efficiency")
def api_efficiency():
    """
    Eficiencia aeróbica por salida: ritmo (seg/km) y FC media.
    Índice de eficiencia = velocidad_ms / (FC_media / 60)
    Cuanto mayor, mejor: más metros por latido.
    """
    rows = query("""
        SELECT start_time, avg_pace_sec_km, avg_hr, avg_speed_ms, distance_m
        FROM workouts
        WHERE avg_hr IS NOT NULL AND avg_pace_sec_km IS NOT NULL
        ORDER BY start_time
    """)
    result = []
    for r in rows:
        eff = None
        if r["avg_speed_ms"] and r["avg_hr"]:
            eff = round(r["avg_speed_ms"] / (r["avg_hr"] / 60), 3)  # m/latido
        result.append({
            "date":      r["start_time"][:10],
            "pace_sec":  r["avg_pace_sec_km"],
            "pace_fmt":  fmt_pace(r["avg_pace_sec_km"]),
            "avg_hr":    r["avg_hr"],
            "eff":       eff,
            "dist_km":   round((r["distance_m"] or 0) / 1000, 2),
        })
    return jsonify(result)


@app.route("/api/weekly")
def api_weekly():
    """Carga semanal: km y tiempo por semana (últimas 20 semanas)."""
    rows = query("""
        SELECT
            strftime('%Y-%W', start_time) as week,
            COUNT(*) as runs,
            ROUND(SUM(distance_m)/1000.0, 2) as km,
            ROUND(SUM(total_time_sec)/60.0, 0) as minutes,
            ROUND(AVG(avg_hr), 0) as avg_hr
        FROM workouts
        GROUP BY week
        ORDER BY week DESC
        LIMIT 20
    """)
    rows.reverse()
    # Añadir label legible
    for r in rows:
        try:
            dt = datetime.strptime(r["week"] + " 1", "%Y-%W %w")
            r["label"] = dt.strftime("%-d %b")
        except Exception:
            r["label"] = r["week"]
    return jsonify(rows)


@app.route("/api/zones")
def api_zones():
    """Distribución de zonas FC acumulada (en % de tiempo/trackpoints)."""
    # Calculamos sobre trackpoints para mayor precisión
    r = query("""
        SELECT
            SUM(CASE WHEN hr < 115 THEN 1 ELSE 0 END)              as z1,
            SUM(CASE WHEN hr >= 115 AND hr < 135 THEN 1 ELSE 0 END) as z2,
            SUM(CASE WHEN hr >= 135 AND hr < 155 THEN 1 ELSE 0 END) as z3,
            SUM(CASE WHEN hr >= 155 AND hr < 170 THEN 1 ELSE 0 END) as z4,
            SUM(CASE WHEN hr >= 170 THEN 1 ELSE 0 END)              as z5,
            COUNT(*) as total
        FROM trackpoints WHERE hr IS NOT NULL
    """)
    r = r[0] if r else {}
    total = r.get("total") or 1
    zones = []
    for i, (key, label, color) in enumerate([
        ("z1", "Z1 Recuperación <115", "#4ade80"),
        ("z2", "Z2 Base aeróbica 115-135", "#86efac"),
        ("z3", "Z3 Umbral aeróbico 135-155", "#facc15"),
        ("z4", "Z4 Umbral anaeróbico 155-170", "#f97316"),
        ("z5", "Z5 Máximo >170", "#ef4444"),
    ], 1):
        count = r.get(key) or 0
        zones.append({
            "zone": f"Z{i}",
            "label": label,
            "pct": round(count / total * 100, 1),
            "count": count,
            "color": color,
        })
    return jsonify(zones)


@app.route("/api/records")
def api_records():
    """
    Récords personales por distancia calculados desde los trackpoints.
    Para cada distancia objetivo, busca la ventana temporal más rápida.
    Método simplificado: mejor ritmo medio en salidas de >= esa distancia.
    """
    targets = [
        {"dist_km": 1,   "label": "1 km"},
        {"dist_km": 3,   "label": "3 km"},
        {"dist_km": 5,   "label": "5 km"},
        {"dist_km": 10,  "label": "10 km"},
    ]
    records = []
    for t in targets:
        dist_m = t["dist_km"] * 1000
        row = query("""
            SELECT start_time, avg_pace_sec_km, distance_m, avg_hr
            FROM workouts
            WHERE distance_m >= ? AND avg_pace_sec_km IS NOT NULL
            ORDER BY avg_pace_sec_km ASC
            LIMIT 1
        """, (dist_m,))
        if row:
            r = row[0]
            records.append({
                "label":    t["label"],
                "pace":     fmt_pace(r["avg_pace_sec_km"]),
                "pace_sec": r["avg_pace_sec_km"],
                "date":     r["start_time"][:10],
                "hr":       r["avg_hr"],
            })
        else:
            records.append({
                "label":    t["label"],
                "pace":     "--",
                "pace_sec": None,
                "date":     None,
                "hr":       None,
            })
    return jsonify(records)


@app.route("/api/heatmap")
def api_heatmap():
    """Actividad diaria para el heatmap de consistencia (últimos 365 días)."""
    rows = query("""
        SELECT
            date(start_time) as day,
            COUNT(*) as runs,
            ROUND(SUM(distance_m)/1000.0, 2) as km
        FROM workouts
        WHERE start_time >= date('now', '-365 days')
        GROUP BY day
    """)
    return jsonify(rows)


# ─────────────────────────────────────────────────────
# PÁGINA PRINCIPAL — sirve el dashboard HTML
# ─────────────────────────────────────────────────────
@app.route("/")
def index():
    html_path = Path(__file__).parent / "dashboard.html"
    return html_path.read_text(encoding="utf-8")


if __name__ == "__main__":
    port = 5000
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    if not Path(DB_PATH).exists():
        print(f"⚠  No se encuentra {DB_PATH}. Asegúrate de estar en el mismo directorio.")
        print(f"   Arranca el watcher primero:  python tcx_tracker.py watch")
        sys.exit(1)

    print(f"✓  Dashboard en: http://localhost:{port}")
    print(f"   Base de datos: {Path(DB_PATH).resolve()}")
    print(f"   (Ctrl+C para detener)")
    app.run(debug=False, port=port, host="0.0.0.0")