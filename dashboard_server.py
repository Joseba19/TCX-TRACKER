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
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, request

# ── Config ───────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "/data/workouts.db" if os.path.isdir("/data") else "workouts.db")

app = Flask(__name__)

# ── DB helpers ───────────────────────────────────────
def query(sql, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

def scalar(sql, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r else None

def execute(sql, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(sql, params)

# ── Formato ──────────────────────────────────────────
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

# ── Filtro de fechas (helper compartido) ─────────────
def date_filter():
    from_date = request.args.get("from")
    to_date   = request.args.get("to")
    clauses, params = [], []
    if from_date:
        clauses.append("start_time >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("start_time <= ?")
        params.append(to_date + "T23:59:59Z")
    where = ("AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


# ─────────────────────────────────────────────────────
# API — RESUMEN
# ─────────────────────────────────────────────────────
@app.route("/api/summary")
def api_summary():
    df, dp = date_filter()
    total_workouts = scalar(f"SELECT COUNT(*) FROM workouts WHERE 1=1 {df}", dp) or 0
    total_km       = scalar(f"SELECT SUM(distance_m)/1000.0 FROM workouts WHERE 1=1 {df}", dp) or 0
    total_time_h   = scalar(f"SELECT SUM(total_time_sec)/3600.0 FROM workouts WHERE 1=1 {df}", dp) or 0
    total_cal      = scalar(f"SELECT SUM(calories) FROM workouts WHERE 1=1 {df}", dp) or 0

    this_month = datetime.utcnow().strftime("%Y-%m")
    km_this_month   = scalar("SELECT SUM(distance_m)/1000.0 FROM workouts WHERE strftime('%Y-%m', start_time)=?", (this_month,)) or 0
    runs_this_month = scalar("SELECT COUNT(*) FROM workouts WHERE strftime('%Y-%m', start_time)=?", (this_month,)) or 0

    last = query(f"SELECT start_time, distance_m, total_time_sec, avg_hr, avg_pace_sec_km, sport FROM workouts WHERE 1=1 {df} ORDER BY start_time DESC LIMIT 1", dp)
    last = last[0] if last else {}

    weeks = query("SELECT DISTINCT strftime('%Y-%W', start_time) as wk FROM workouts ORDER BY wk DESC")
    streak = 0
    if weeks:
        now_wk = datetime.utcnow().strftime("%Y-%W")
        expected = now_wk
        for w in weeks:
            if w["wk"] == expected:
                streak += 1
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
            "date":    last.get("start_time", "")[:10] if last else "",
            "sport":   last.get("sport", ""),
            "dist_km": round((last.get("distance_m") or 0) / 1000, 2),
            "time":    fmt_time(last.get("total_time_sec")),
            "avg_hr":  last.get("avg_hr"),
            "pace":    fmt_pace(last.get("avg_pace_sec_km")),
        }
    })


# ─────────────────────────────────────────────────────
# API — ENTRENAMIENTOS + DELETE
# ─────────────────────────────────────────────────────
@app.route("/api/workouts")
def api_workouts():
    df, dp = date_filter()
    rows = query(f"SELECT id, sport, start_time, distance_m, total_time_sec, avg_hr, max_hr, avg_pace_sec_km, calories, avg_cadence FROM workouts WHERE 1=1 {df} ORDER BY start_time DESC", dp)
    for r in rows:
        r["dist_km"]  = round((r["distance_m"] or 0) / 1000, 2)
        r["pace_fmt"] = fmt_pace(r["avg_pace_sec_km"])
        r["time_fmt"] = fmt_time(r["total_time_sec"])
        r["date"]     = r["start_time"][:10] if r["start_time"] else ""
    return jsonify(rows)


@app.route("/api/workouts/<int:workout_id>", methods=["DELETE"])
def api_delete_workout(workout_id):
    w = scalar("SELECT id FROM workouts WHERE id=?", (workout_id,))
    if not w:
        return jsonify({"error": f"No existe el entrenamiento {workout_id}"}), 404
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM trackpoints WHERE workout_id=?", (workout_id,))
        conn.execute("DELETE FROM workouts WHERE id=?", (workout_id,))
    return jsonify({"deleted": workout_id})


# ─────────────────────────────────────────────────────
# API — EFICIENCIA
# ─────────────────────────────────────────────────────
@app.route("/api/efficiency")
def api_efficiency():
    df, dp = date_filter()
    rows = query(f"SELECT start_time, avg_pace_sec_km, avg_hr, avg_speed_ms, distance_m FROM workouts WHERE avg_hr IS NOT NULL AND avg_pace_sec_km IS NOT NULL {df} ORDER BY start_time", dp)
    result = []
    for r in rows:
        eff = round(r["avg_speed_ms"] / (r["avg_hr"] / 60), 3) if r["avg_speed_ms"] and r["avg_hr"] else None
        result.append({
            "date": r["start_time"][:10], "pace_sec": r["avg_pace_sec_km"],
            "pace_fmt": fmt_pace(r["avg_pace_sec_km"]), "avg_hr": r["avg_hr"],
            "eff": eff, "dist_km": round((r["distance_m"] or 0) / 1000, 2),
        })
    return jsonify(result)


# ─────────────────────────────────────────────────────
# API — CARGA SEMANAL
# ─────────────────────────────────────────────────────
@app.route("/api/weekly")
def api_weekly():
    df, dp = date_filter()
    rows = query(f"""
        SELECT strftime('%Y-%W', start_time) as week,
               COUNT(*) as runs,
               ROUND(SUM(distance_m)/1000.0, 2) as km,
               ROUND(SUM(total_time_sec)/60.0, 0) as minutes,
               ROUND(AVG(avg_hr), 0) as avg_hr
        FROM workouts WHERE 1=1 {df}
        GROUP BY week ORDER BY week DESC LIMIT 20
    """, dp)
    rows.reverse()
    for r in rows:
        try:
            dt = datetime.strptime(r["week"] + " 1", "%Y-%W %w")
            r["label"] = dt.strftime("%-d %b")
        except Exception:
            r["label"] = r["week"]
    return jsonify(rows)


# ─────────────────────────────────────────────────────
# API — ZONAS FC
# ─────────────────────────────────────────────────────
@app.route("/api/zones")
def api_zones():
    df, dp = date_filter()
    if dp:
        df_tp = df.replace("start_time", "w.start_time")
        sql = f"""SELECT
            SUM(CASE WHEN t.hr < 115 THEN 1 ELSE 0 END) as z1,
            SUM(CASE WHEN t.hr >= 115 AND t.hr < 135 THEN 1 ELSE 0 END) as z2,
            SUM(CASE WHEN t.hr >= 135 AND t.hr < 155 THEN 1 ELSE 0 END) as z3,
            SUM(CASE WHEN t.hr >= 155 AND t.hr < 170 THEN 1 ELSE 0 END) as z4,
            SUM(CASE WHEN t.hr >= 170 THEN 1 ELSE 0 END) as z5,
            COUNT(*) as total
            FROM trackpoints t JOIN workouts w ON t.workout_id = w.id
            WHERE t.hr IS NOT NULL {df_tp}"""
    else:
        sql = "SELECT SUM(CASE WHEN hr<115 THEN 1 ELSE 0 END) as z1, SUM(CASE WHEN hr>=115 AND hr<135 THEN 1 ELSE 0 END) as z2, SUM(CASE WHEN hr>=135 AND hr<155 THEN 1 ELSE 0 END) as z3, SUM(CASE WHEN hr>=155 AND hr<170 THEN 1 ELSE 0 END) as z4, SUM(CASE WHEN hr>=170 THEN 1 ELSE 0 END) as z5, COUNT(*) as total FROM trackpoints WHERE hr IS NOT NULL"

    r = query(sql, dp)
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
        zones.append({"zone": f"Z{i}", "label": label, "pct": round(count/total*100, 1), "count": count, "color": color})
    return jsonify(zones)


# ─────────────────────────────────────────────────────
# API — RÉCORDS
# ─────────────────────────────────────────────────────
@app.route("/api/records")
def api_records():
    targets = [{"dist_km": 1, "label": "1 km"}, {"dist_km": 3, "label": "3 km"},
               {"dist_km": 5, "label": "5 km"}, {"dist_km": 10, "label": "10 km"}]
    records = []
    for t in targets:
        row = query("SELECT start_time, avg_pace_sec_km, distance_m, avg_hr FROM workouts WHERE distance_m >= ? AND avg_pace_sec_km IS NOT NULL ORDER BY avg_pace_sec_km ASC LIMIT 1", (t["dist_km"]*1000,))
        if row:
            r = row[0]
            records.append({"label": t["label"], "pace": fmt_pace(r["avg_pace_sec_km"]), "pace_sec": r["avg_pace_sec_km"], "date": r["start_time"][:10], "hr": r["avg_hr"]})
        else:
            records.append({"label": t["label"], "pace": "--", "pace_sec": None, "date": None, "hr": None})
    return jsonify(records)


# ─────────────────────────────────────────────────────
# API — HEATMAP
# ─────────────────────────────────────────────────────
@app.route("/api/heatmap")
def api_heatmap():
    rows = query("SELECT date(start_time) as day, COUNT(*) as runs, ROUND(SUM(distance_m)/1000.0, 2) as km FROM workouts WHERE start_time >= date('now', '-365 days') GROUP BY day")
    return jsonify(rows)


# ─────────────────────────────────────────────────────
# API — COMPARATIVA
# ─────────────────────────────────────────────────────
@app.route("/api/compare")
def api_compare():
    a_from = request.args.get("a_from")
    a_to   = request.args.get("a_to")
    b_from = request.args.get("b_from")
    b_to   = request.args.get("b_to")

    if not all([a_from, a_to, b_from, b_to]):
        return jsonify({"error": "Faltan parámetros: a_from, a_to, b_from, b_to"}), 400

    def period_stats(from_d, to_d):
        rows = query("SELECT distance_m, total_time_sec, avg_hr, avg_pace_sec_km, calories FROM workouts WHERE start_time >= ? AND start_time <= ? ORDER BY start_time", (from_d, to_d + "T23:59:59Z"))
        if not rows:
            return {"runs": 0, "km": 0, "avg_pace": None, "avg_pace_fmt": "--", "avg_hr": None, "total_cal": 0, "total_time_h": 0, "avg_km_per_run": 0, "from": from_d, "to": to_d}

        total_dist = sum(r["distance_m"] or 0 for r in rows)
        total_time = sum(r["total_time_sec"] or 0 for r in rows)
        total_cal  = sum(r["calories"] or 0 for r in rows)
        hr_vals    = [r["avg_hr"] for r in rows if r["avg_hr"]]
        avg_hr     = sum(hr_vals) / len(hr_vals) if hr_vals else None
        avg_pace   = total_time / (total_dist / 1000) if total_dist else None

        weekly = query("SELECT strftime('%Y-%W', start_time) as wk, ROUND(SUM(distance_m)/1000.0,2) as km FROM workouts WHERE start_time >= ? AND start_time <= ? GROUP BY wk ORDER BY wk", (from_d, to_d + "T23:59:59Z"))

        return {
            "runs": len(rows), "km": round(total_dist/1000, 1),
            "avg_pace": avg_pace, "avg_pace_fmt": fmt_pace(avg_pace),
            "avg_hr": round(avg_hr, 1) if avg_hr else None,
            "total_cal": int(total_cal), "total_time_h": round(total_time/3600, 1),
            "avg_km_per_run": round(total_dist/1000/len(rows), 2),
            "weekly_km": [{"week": w["wk"], "km": w["km"]} for w in weekly],
            "from": from_d, "to": to_d,
        }

    a = period_stats(a_from, a_to)
    b = period_stats(b_from, b_to)

    def delta(va, vb, invert=False):
        if va is None or vb is None or va == 0:
            return None
        d = ((vb - va) / abs(va)) * 100
        return round(-d if invert else d, 1)

    return jsonify({
        "a": a, "b": b,
        "delta": {
            "km":             delta(a["km"], b["km"]),
            "runs":           delta(a["runs"], b["runs"]),
            "avg_pace":       delta(a["avg_pace"], b["avg_pace"], invert=True),
            "avg_hr":         delta(a["avg_hr"], b["avg_hr"], invert=True),
            "avg_km_per_run": delta(a["avg_km_per_run"], b["avg_km_per_run"]),
            "cal_per_run":    delta(a["total_cal"]/a["runs"] if a["runs"] else None,
                                   b["total_cal"]/b["runs"] if b["runs"] else None),
            "total_time_h":  delta(a["total_time_h"], b["total_time_h"]),
        }
    })


# ─────────────────────────────────────────────────────
# API — RANGO DE FECHAS DISPONIBLE
# ─────────────────────────────────────────────────────
@app.route("/api/date_range")
def api_date_range():
    return jsonify({
        "min": scalar("SELECT date(MIN(start_time)) FROM workouts"),
        "max": scalar("SELECT date(MAX(start_time)) FROM workouts"),
    })


# ─────────────────────────────────────────────────────
# PÁGINA PRINCIPAL
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
        print(f"⚠  No se encuentra {DB_PATH}.")
        sys.exit(1)
    print(f"✓  Dashboard en: http://localhost:{port}")
    app.run(debug=False, port=port, host="0.0.0.0")