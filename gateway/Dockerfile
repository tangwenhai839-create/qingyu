FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/qingyu

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

RUN pip install .

# /data is the archive_data mount point. Staging rejects that path
# (os.path.ismount → archive_root_is_unsafe), so Compose sets ARCHIVE_ROOT
# to /data/archive. Both directories must exist and be owned by the runtime
# user before USER. A fresh named volume copies them and inherits that
# ownership instead of root.
RUN useradd --system --create-home --home-dir /home/qingyu --shell /usr/sbin/nologin qingyu \
    && mkdir -p /data/archive \
    && chown qingyu:qingyu /data /data/archive

USER qingyu

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
