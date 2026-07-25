FROM python:3.11-slim

# poppler-utils provides pdfinfo/pdfimages used for cross-checking; PyMuPDF
# does the heavy lifting. Fonts help PyMuPDF render label text.
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

ENV PREPRESS_STORAGE_ROOT=/srv/prepress/tenants \
    PREPRESS_TENANTS_FILE=/srv/prepress/tenants.toml \
    PREPRESS_PORT=8080
EXPOSE 8080
CMD ["uvicorn", "prepress_mcp.server:build_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
