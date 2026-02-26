FROM python:3.12-slim

WORKDIR /app

# Dependencias Python
RUN pip install --no-cache-dir flask

# Copiar código
COPY tcx_tracker.py .
COPY dashboard_server.py .
COPY dashboard.html .
COPY start.sh .

RUN chmod +x start.sh

# Crear carpetas de datos
RUN mkdir -p /data/Archivos

EXPOSE 5000

CMD ["./start.sh"]
