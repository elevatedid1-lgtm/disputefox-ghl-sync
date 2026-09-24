FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/dfghl.sqlite3 PORT=8080
WORKDIR /app
COPY dfghl ./dfghl
RUN useradd --system --uid 10001 app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz')"
CMD ["python", "-m", "dfghl", "serve"]
