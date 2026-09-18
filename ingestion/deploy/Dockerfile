FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates poppler-utils tesseract-ocr \
       tesseract-ocr-eng tesseract-ocr-hin \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 cag \
    && useradd --uid 10001 --gid 10001 --no-create-home cag

WORKDIR /opt/cag
COPY ingestion/requirements.txt ./ingestion/requirements.txt
RUN python -m pip install -r ingestion/requirements.txt
COPY ingestion/ ./ingestion/

USER 10001:10001
ENTRYPOINT ["python", "/opt/cag/ingestion/deploy/worker-entrypoint.py"]
CMD ["python", "-m", "ingestion.catalogue", "--data-dir", "/data/files/cag/pipeline", "run"]