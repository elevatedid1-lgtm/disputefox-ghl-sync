FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/dfghl.sqlite3 PORT=8080
WORKDIR /app
COPY dfghl ./dfghl
# No VOLUME/USER here: Railway rejects VOLUME in Dockerfiles and mounts its
# volumes as root. Attach a volume at /data in the host's settings instead.
RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "-m", "dfghl", "serve"]
