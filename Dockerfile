FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY pyproject.toml README.md ./
COPY agri_ai ./agri_ai
RUN pip install --no-cache-dir ".[postgres]" && useradd --create-home agri && mkdir -p /srv/uploads && chown agri /srv/uploads
USER agri
ENV AGRI_UPLOAD_DIR=/srv/uploads
EXPOSE 8000
CMD ["uvicorn", "agri_ai.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
