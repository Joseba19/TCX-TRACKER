#!/bin/sh
# Arranca el watcher en background y el servidor web en foreground

echo "──────────────────────────────────────"
echo "  TCX Tracker"
echo "  Archivos TCX : /data/Archivos"
echo "  Base de datos: /data/workouts.db"
echo "  Dashboard    : http://0.0.0.0:5000"
echo "──────────────────────────────────────"

# Cambiar al directorio de datos para que DB y log queden ahí
cd /data

# Importar todo lo que haya al arrancar y luego vigilar en background
python /app/tcx_tracker.py watch /data/Archivos &

# Servidor web en foreground (mantiene el contenedor vivo)
python /app/dashboard_server.py
