#!/usr/bin/env python3
"""
TCX Workout Tracker
-------------------
Lee archivos .tcx de entrenamientos y guarda los datos en una base de datos SQLite.
Incluye funciones para importar archivos, consultar estadísticas y listar entrenamientos.

Uso:
    python tcx_tracker.py watch                       # Vigilar carpeta Archivos/ (modo automático)
    python tcx_tracker.py watch /ruta/carpeta         # Vigilar una carpeta específica
    python tcx_tracker.py import archivo.tcx          # Importar un archivo manualmente
    python tcx_tracker.py import carpeta/             # Importar todos los .tcx de una carpeta
    python tcx_tracker.py list                        # Listar todos los entrenamientos
    python tcx_tracker.py stats                       # Estadísticas generales
    python tcx_tracker.py show <id>                   # Ver detalle de un entrenamiento
    python tcx_tracker.py export <id>                 # Exportar trackpoints a CSV
    python tcx_tracker.py delete <id>                 # Eliminar un entrenamiento
    python tcx_tracker.py delete <id> --confirm       # Eliminar sin pedir confirmación
"""

import sqlite3
import xml.etree.ElementTree as ET
import sys
import os
import glob
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
DB_PATH = "workouts.db"
WATCH_FOLDER = "Archivos"       # Carpeta vigilada por defecto
WATCH_INTERVAL = 5              # Segundos entre comprobaciones
LOG_FILE = "tcx_tracker.log"    # Log de importaciones automáticas

# Logging: muestra en pantalla Y guarda en fichero
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# Namespaces usados en archivos Garmin/Zepp TCX
NS = {
    "tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2",
    "ns3": "http://www.garmin.com/xmlschemas/ActivityExtension/v2",
}


# ─────────────────────────────────────────────
# BASE DE DATOS
# ─────────────────────────────────────────────
def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    """Crea las tablas si no existen."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workouts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name       TEXT,
                sport           TEXT,
                start_time      TEXT,          -- ISO8601 UTC
                notes           TEXT,
                total_time_sec  REAL,          -- segundos totales
                distance_m      REAL,          -- metros
                calories        INTEGER,
                avg_hr          INTEGER,       -- ppm
                max_hr          INTEGER,       -- ppm
                avg_pace_sec_km REAL,          -- seg/km  (calculado)
                avg_speed_ms    REAL,          -- m/s     (calculado desde trackpoints)
                avg_cadence     REAL,          -- pasos/min
                imported_at     TEXT DEFAULT (datetime('now')),
                UNIQUE(file_name, start_time)
            );

            CREATE TABLE IF NOT EXISTS trackpoints (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                workout_id  INTEGER REFERENCES workouts(id) ON DELETE CASCADE,
                time        TEXT,
                latitude    REAL,
                longitude   REAL,
                hr          INTEGER,   -- ppm
                cadence     INTEGER,   -- pasos/min
                speed_ms    REAL       -- m/s
            );

            CREATE INDEX IF NOT EXISTS idx_tp_workout ON trackpoints(workout_id);
            CREATE INDEX IF NOT EXISTS idx_workout_start ON workouts(start_time);
        """)
    print(f"[DB] Base de datos lista: {DB_PATH}")


# ─────────────────────────────────────────────
# PARSER TCX
# ─────────────────────────────────────────────
def _text(element, path, ns=NS, default=None):
    """Helper: obtiene texto de un subelemento o devuelve default."""
    node = element.find(path, ns)
    return node.text if node is not None else default


def _float(element, path, ns=NS):
    v = _text(element, path, ns)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _int(element, path, ns=NS):
    v = _text(element, path, ns)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def parse_tcx(filepath: str) -> dict:
    """
    Parsea un archivo TCX y devuelve un dict con:
      - workout: dict con los metadatos del entrenamiento
      - trackpoints: lista de dicts con los puntos GPS/HR
    """
    tree = ET.parse(filepath)
    root = tree.getroot()

    activity = root.find("tcx:Activities/tcx:Activity", NS)
    if activity is None:
        raise ValueError(f"No se encontró <Activity> en {filepath}")

    sport = activity.get("Sport", "Unknown")
    start_time = _text(activity, "tcx:Id", NS)
    notes = _text(activity, "tcx:Notes", NS)

    # Acumular datos de todos los laps
    total_time = 0.0
    total_dist = 0.0
    total_cal = 0
    max_hr = 0
    avg_hr_values = []

    trackpoints = []

    for lap in activity.findall("tcx:Lap", NS):
        total_time += float(_text(lap, "tcx:TotalTimeSeconds", NS) or 0)
        total_dist += float(_text(lap, "tcx:DistanceMeters", NS) or 0)
        total_cal += int(_text(lap, "tcx:Calories", NS) or 0)

        lap_avg_hr = _int(lap, "tcx:AverageHeartRateBpm/tcx:Value", NS)
        lap_max_hr = _int(lap, "tcx:MaximumHeartRateBpm/tcx:Value", NS)
        if lap_avg_hr:
            avg_hr_values.append(lap_avg_hr)
        if lap_max_hr and lap_max_hr > max_hr:
            max_hr = lap_max_hr

        for tp in lap.findall("tcx:Track/tcx:Trackpoint", NS):
            t = _text(tp, "tcx:Time", NS)
            lat = _float(tp, "tcx:Position/tcx:LatitudeDegrees", NS)
            lon = _float(tp, "tcx:Position/tcx:LongitudeDegrees", NS)
            hr = _int(tp, "tcx:HeartRateBpm/tcx:Value", NS)
            cad = _int(tp, "tcx:Cadence", NS)
            speed = _float(tp, "tcx:Extensions/ns3:TPX/ns3:Speed", NS)

            trackpoints.append({
                "time": t,
                "latitude": lat,
                "longitude": lon,
                "hr": hr,
                "cadence": cad,
                "speed_ms": speed,
            })

    # Calcular métricas derivadas
    avg_hr = int(sum(avg_hr_values) / len(avg_hr_values)) if avg_hr_values else None

    speeds = [tp["speed_ms"] for tp in trackpoints if tp["speed_ms"]]
    avg_speed = sum(speeds) / len(speeds) if speeds else None

    cadences = [tp["cadence"] for tp in trackpoints if tp["cadence"]]
    avg_cadence = sum(cadences) / len(cadences) if cadences else None

    # Ritmo medio en seg/km — siempre desde tiempo/distancia totales (más preciso)
    if total_dist > 0 and total_time > 0:
        avg_pace_sec_km = total_time / (total_dist / 1000)
    else:
        avg_pace_sec_km = None

    workout = {
        "file_name": os.path.basename(filepath),
        "sport": sport,
        "start_time": start_time,
        "notes": notes,
        "total_time_sec": total_time,
        "distance_m": total_dist,
        "calories": total_cal,
        "avg_hr": avg_hr,
        "max_hr": max_hr if max_hr else None,
        "avg_pace_sec_km": avg_pace_sec_km,
        "avg_speed_ms": avg_speed,
        "avg_cadence": avg_cadence,
    }

    return {"workout": workout, "trackpoints": trackpoints}


# ─────────────────────────────────────────────
# IMPORTACIÓN
# ─────────────────────────────────────────────
def import_tcx(filepath: str):
    """Importa un archivo TCX a la base de datos."""
    print(f"[IMPORT] Procesando: {filepath}")
    try:
        data = parse_tcx(filepath)
    except Exception as e:
        print(f"  ✗ Error al parsear: {e}")
        return

    w = data["workout"]
    tps = data["trackpoints"]

    with get_connection() as conn:
        try:
            cur = conn.execute("""
                INSERT INTO workouts
                    (file_name, sport, start_time, notes, total_time_sec,
                     distance_m, calories, avg_hr, max_hr,
                     avg_pace_sec_km, avg_speed_ms, avg_cadence)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                w["file_name"], w["sport"], w["start_time"], w["notes"],
                w["total_time_sec"], w["distance_m"], w["calories"],
                w["avg_hr"], w["max_hr"],
                w["avg_pace_sec_km"], w["avg_speed_ms"], w["avg_cadence"],
            ))
            workout_id = cur.lastrowid

            conn.executemany("""
                INSERT INTO trackpoints
                    (workout_id, time, latitude, longitude, hr, cadence, speed_ms)
                VALUES (?,?,?,?,?,?,?)
            """, [
                (workout_id, tp["time"], tp["latitude"], tp["longitude"],
                 tp["hr"], tp["cadence"], tp["speed_ms"])
                for tp in tps
            ])

            print(f"  ✓ Importado: ID={workout_id} | {w['sport']} | {w['start_time']}")
            print(f"    Distancia: {w['distance_m']/1000:.2f} km | "
                  f"Tiempo: {fmt_time(w['total_time_sec'])} | "
                  f"FC media: {w['avg_hr']} ppm | "
                  f"Ritmo: {fmt_pace(w['avg_pace_sec_km'])}")
            print(f"    Trackpoints guardados: {len(tps)}")

        except sqlite3.IntegrityError:
            print(f"  ⚠ Ya existe en la base de datos (mismo archivo y hora de inicio).")


def import_path(path: str):
    """Importa un archivo o todos los .tcx de una carpeta."""
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "**/*.tcx"), recursive=True))
        files += sorted(glob.glob(os.path.join(path, "*.tcx")))
        files = list(set(files))
        if not files:
            print(f"No se encontraron archivos .tcx en {path}")
            return
        print(f"Encontrados {len(files)} archivos .tcx")
        for f in sorted(files):
            import_tcx(f)
    elif os.path.isfile(path):
        import_tcx(path)
    else:
        print(f"Ruta no encontrada: {path}")


# ─────────────────────────────────────────────
# UTILIDADES DE FORMATO
# ─────────────────────────────────────────────
def fmt_time(seconds):
    if seconds is None:
        return "--"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_pace(sec_per_km):
    if sec_per_km is None:
        return "--"
    m = int(sec_per_km // 60)
    s = int(sec_per_km % 60)
    return f"{m}:{s:02d} /km"


# ─────────────────────────────────────────────
# CONSULTAS
# ─────────────────────────────────────────────
def list_workouts():
    """Lista todos los entrenamientos."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, sport, start_time, distance_m, total_time_sec,
                   avg_hr, max_hr, avg_pace_sec_km, calories
            FROM workouts
            ORDER BY start_time DESC
        """).fetchall()

    if not rows:
        print("No hay entrenamientos importados aún.")
        return

    print(f"\n{'ID':>4}  {'Deporte':<12} {'Fecha/Hora':<22} {'Dist':>8} {'Tiempo':>8} "
          f"{'FC med':>7} {'FC max':>7} {'Ritmo':>10} {'Kcal':>6}")
    print("─" * 95)
    for r in rows:
        id_, sport, start, dist, time_s, avg_hr, max_hr, pace, cal = r
        dt = start[:19].replace("T", " ") if start else "--"
        dist_km = f"{dist/1000:.2f} km" if dist else "--"
        print(f"{id_:>4}  {sport:<12} {dt:<22} {dist_km:>8} {fmt_time(time_s):>8} "
              f"{str(avg_hr or '--'):>7} {str(max_hr or '--'):>7} "
              f"{fmt_pace(pace):>10} {str(cal or '--'):>6}")
    print(f"\nTotal: {len(rows)} entrenamiento(s)")


def show_workout(workout_id: int):
    """Muestra el detalle de un entrenamiento."""
    with get_connection() as conn:
        w = conn.execute(
            "SELECT * FROM workouts WHERE id=?", (workout_id,)
        ).fetchone()
        if not w:
            print(f"No existe el entrenamiento con ID={workout_id}")
            return
        cols = [d[0] for d in conn.execute("SELECT * FROM workouts LIMIT 0").description]
        w = dict(zip(cols, w))

        tp_count = conn.execute(
            "SELECT COUNT(*) FROM trackpoints WHERE workout_id=?", (workout_id,)
        ).fetchone()[0]

        # HR distribution from trackpoints
        hr_dist = conn.execute("""
            SELECT
                SUM(CASE WHEN hr < 120 THEN 1 ELSE 0 END) as z1,
                SUM(CASE WHEN hr >= 120 AND hr < 140 THEN 1 ELSE 0 END) as z2,
                SUM(CASE WHEN hr >= 140 AND hr < 160 THEN 1 ELSE 0 END) as z3,
                SUM(CASE WHEN hr >= 160 AND hr < 175 THEN 1 ELSE 0 END) as z4,
                SUM(CASE WHEN hr >= 175 THEN 1 ELSE 0 END) as z5,
                COUNT(*) as total
            FROM trackpoints WHERE workout_id=? AND hr IS NOT NULL
        """, (workout_id,)).fetchone()

    print(f"\n{'═'*50}")
    print(f"  Entrenamiento #{workout_id}")
    print(f"{'═'*50}")
    print(f"  Archivo:       {w['file_name']}")
    print(f"  Deporte:       {w['sport']}")
    print(f"  Inicio:        {w['start_time']}")
    print(f"  Notas:         {w['notes'] or '--'}")
    print(f"{'─'*50}")
    print(f"  Distancia:     {w['distance_m']/1000:.3f} km")
    print(f"  Tiempo:        {fmt_time(w['total_time_sec'])}")
    print(f"  Calorías:      {w['calories']} kcal")
    print(f"{'─'*50}")
    print(f"  FC media:      {w['avg_hr']} ppm")
    print(f"  FC máxima:     {w['max_hr']} ppm")
    print(f"  Ritmo medio:   {fmt_pace(w['avg_pace_sec_km'])}")
    print(f"  Vel. media:    {w['avg_speed_ms']:.2f} m/s  ({(w['avg_speed_ms'] or 0)*3.6:.1f} km/h)")
    print(f"  Cadencia med:  {w['avg_cadence']:.0f} pasos/min" if w['avg_cadence'] else "  Cadencia:      --")
    print(f"{'─'*50}")
    print(f"  Trackpoints:   {tp_count}")

    if hr_dist and hr_dist[5] > 0:
        total = hr_dist[5]
        print(f"\n  Zonas FC (estimadas):")
        zones = ["Z1 (<120)", "Z2 (120-140)", "Z3 (140-160)", "Z4 (160-175)", "Z5 (>175)"]
        for z, count in zip(zones, hr_dist[:5]):
            pct = (count or 0) / total * 100
            bar = "█" * int(pct / 3)
            print(f"    {z:<14} {bar:<20} {pct:5.1f}%")
    print()


def show_stats():
    """Estadísticas globales de todos los entrenamientos."""
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]
        if total == 0:
            print("No hay datos aún.")
            return

        s = conn.execute("""
            SELECT
                sport,
                COUNT(*) as count,
                SUM(distance_m)/1000 as total_km,
                SUM(total_time_sec)/3600 as total_h,
                SUM(calories) as total_cal,
                AVG(avg_hr) as mean_hr,
                MAX(max_hr) as best_max_hr,
                MIN(avg_pace_sec_km) as best_pace,
                AVG(avg_pace_sec_km) as mean_pace,
                MAX(distance_m)/1000 as longest_km
            FROM workouts
            GROUP BY sport
            ORDER BY count DESC
        """).fetchall()

        monthly = conn.execute("""
            SELECT strftime('%Y-%m', start_time) as month,
                   COUNT(*) as runs,
                   SUM(distance_m)/1000 as km,
                   SUM(calories) as cal
            FROM workouts
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
        """).fetchall()

    print(f"\n{'═'*60}")
    print(f"  ESTADÍSTICAS GLOBALES  ({total} entrenamientos)")
    print(f"{'═'*60}")

    for r in s:
        sport, count, tot_km, tot_h, tot_cal, mean_hr, best_max_hr, best_pace, mean_pace, longest = r
        print(f"\n  ▶ {sport}")
        print(f"    Entrenamientos:    {count}")
        print(f"    Distancia total:   {tot_km:.1f} km")
        print(f"    Tiempo total:      {tot_h:.1f} h")
        print(f"    Calorías totales:  {tot_cal or 0} kcal")
        print(f"    FC media global:   {mean_hr:.0f} ppm" if mean_hr else "    FC media:         --")
        print(f"    FC máx. récord:    {best_max_hr} ppm" if best_max_hr else "    FC máx.:          --")
        print(f"    Ritmo medio:       {fmt_pace(mean_pace)}")
        print(f"    Mejor ritmo:       {fmt_pace(best_pace)}")
        print(f"    Salida más larga:  {longest:.2f} km")

    if monthly:
        print(f"\n{'─'*60}")
        print(f"  ÚLTIMOS 12 MESES")
        print(f"  {'Mes':<10} {'Salidas':>8} {'Km':>8} {'Kcal':>8}")
        print(f"  {'─'*36}")
        for mes, runs, km, cal in monthly:
            print(f"  {mes:<10} {runs:>8} {km:>8.1f} {cal or 0:>8}")
    print()


def export_trackpoints(workout_id: int):
    """Exporta los trackpoints de un entrenamiento a CSV."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT time, latitude, longitude, hr, cadence, speed_ms
            FROM trackpoints WHERE workout_id=?
            ORDER BY time
        """, (workout_id,)).fetchall()

    if not rows:
        print(f"No hay trackpoints para el entrenamiento {workout_id}")
        return

    fname = f"trackpoints_{workout_id}.csv"
    with open(fname, "w") as f:
        f.write("time,latitude,longitude,hr_bpm,cadence,speed_ms\n")
        for r in rows:
            f.write(",".join(str(v) if v is not None else "" for v in r) + "\n")
    print(f"✓ Exportados {len(rows)} trackpoints a: {fname}")



# ─────────────────────────────────────────────
# ELIMINAR
# ─────────────────────────────────────────────
def delete_workout(workout_id: int, confirm: bool = False):
    """Elimina un entrenamiento y sus trackpoints de la base de datos."""
    with get_connection() as conn:
        w = conn.execute(
            "SELECT file_name, sport, start_time, distance_m FROM workouts WHERE id=?",
            (workout_id,)
        ).fetchone()

    if not w:
        print(f"✗ No existe el entrenamiento con ID={workout_id}")
        return

    fname, sport, start, dist = w
    dist_km = f"{dist/1000:.2f} km" if dist else "--"
    print(f"  ID {workout_id} | {sport} | {start[:10]} | {dist_km} | {fname}")

    if not confirm:
        resp = input("¿Eliminar este entrenamiento? [s/N]: ").strip().lower()
        if resp not in ("s", "si", "sí", "y", "yes"):
            print("Cancelado.")
            return

    with get_connection() as conn:
        conn.execute("DELETE FROM trackpoints WHERE workout_id=?", (workout_id,))
        conn.execute("DELETE FROM workouts WHERE id=?", (workout_id,))

    print(f"✓ Entrenamiento #{workout_id} eliminado (incluidos sus trackpoints).")


# ─────────────────────────────────────────────
# WATCHER — vigilancia automática de carpeta
# ─────────────────────────────────────────────
def get_known_files() -> set:
    """Devuelve el conjunto de nombres de archivo ya importados."""
    with get_connection() as conn:
        rows = conn.execute("SELECT file_name FROM workouts").fetchall()
    return {r[0] for r in rows}


def scan_folder(folder: str) -> list:
    """Devuelve lista de rutas .tcx en la carpeta (recursivo)."""
    folder = Path(folder)
    return sorted(folder.rglob("*.tcx"))


def watch(folder: str = WATCH_FOLDER):
    """
    Vigila una carpeta en busca de nuevos .tcx.
    - Al arrancar: importa todos los archivos no registrados aún.
    - En bucle: cada WATCH_INTERVAL segundos comprueba si hay archivos nuevos.
    - Para detenerlo: Ctrl+C
    """
    folder = Path(folder)

    # Crear la carpeta si no existe
    folder.mkdir(parents=True, exist_ok=True)

    log.info(f"═══════════════════════════════════════")
    log.info(f"  TCX Watcher arrancado")
    log.info(f"  Carpeta : {folder.resolve()}")
    log.info(f"  Intervalo: {WATCH_INTERVAL}s  |  Log: {LOG_FILE}")
    log.info(f"  (Ctrl+C para detener)")
    log.info(f"═══════════════════════════════════════")

    # Primera pasada: importar todo lo que haya y no esté en la BD
    known = get_known_files()
    archivos = scan_folder(folder)
    nuevos = [f for f in archivos if f.name not in known]

    if archivos:
        log.info(f"[INICIO] {len(archivos)} archivo(s) encontrado(s), {len(nuevos)} nuevo(s) sin importar.")
        for f in nuevos:
            import_tcx(str(f))
    else:
        log.info(f"[INICIO] Carpeta vacía. Esperando archivos...")

    # Bucle de vigilancia
    try:
        while True:
            time.sleep(WATCH_INTERVAL)
            known = get_known_files()
            archivos_ahora = scan_folder(folder)
            nuevos = [f for f in archivos_ahora if f.name not in known]
            if nuevos:
                log.info(f"[NUEVO] {len(nuevos)} archivo(s) detectado(s):")
                for f in nuevos:
                    import_tcx(str(f))
    except KeyboardInterrupt:
        log.info("\n[STOP] Watcher detenido.")

# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────
def main():
    init_db()

    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()

    if cmd == "watch":
        folder = sys.argv[2] if len(sys.argv) >= 3 else WATCH_FOLDER
        watch(folder)

    elif cmd == "import" and len(sys.argv) >= 3:
        import_path(sys.argv[2])

    elif cmd == "list":
        list_workouts()

    elif cmd == "stats":
        show_stats()

    elif cmd == "show" and len(sys.argv) >= 3:
        show_workout(int(sys.argv[2]))

    elif cmd == "export" and len(sys.argv) >= 3:
        export_trackpoints(int(sys.argv[2]))

    elif cmd == "delete" and len(sys.argv) >= 3:
        confirm = "--confirm" in sys.argv
        delete_workout(int(sys.argv[2]), confirm=confirm)

    else:
        print(__doc__)


if __name__ == "__main__":
    main()