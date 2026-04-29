FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py .

VOLUME ["/data"]
EXPOSE 9099

ENV SIGNAL_API_URL=http://signal-api:8080
ENV DB_PATH=/data/bridge.db
ENV PORT=9099

CMD ["python", "bridge.py"]
