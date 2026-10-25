FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/qingyu

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install .

# /data must exist and be owned by the runtime user before USER, so a fresh
# named volume mounted there inherits that ownership instead of root.
RUN useradd --system --create-home --home-dir /home/qingyu --shell /usr/sbin/nologin qingyu \
    && mkdir -p /data \
    && chown qingyu:qingyu /data

USER qingyu

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
