FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt requirements.lock ./
# the lock is a constraints file: requirements.txt picks what is installed,
# requirements.lock pins the versions CI tested
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock
COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app
ENV SQLITE_PATH=/data/manager.db
# non-root; /data is created and chowned before USER so a fresh named volume
# (which inherits the image dir's ownership) is writable
RUN useradd --system --uid 10001 --no-create-home app \
 && mkdir -p /data && chown app:app /data
USER app
# --ws-max-size must cover one adapter AGENT_MESSAGE frame: a full omp reply
# (aligned with the adapter's STREAM_LIMIT_BYTES default, 64 MiB). uvicorn's
# 16 MiB default closes the WS (1009) on bigger replies.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-max-size 67108864"]
