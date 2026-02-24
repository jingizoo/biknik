# Minimal image for running Python in CDX Batch.
# If your environment already provides gsutil/gcloud, remove the gcloud install section.

FROM python:3.11-slim

# (Optional) Install gsutil (google-cloud-cli) for gs:// download/upload.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && mkdir -p /usr/share/keyrings \
 && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends google-cloud-cli \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/
RUN chmod +x /app/hello_batch.py

# Default program to run (Batch command can override)
ENTRYPOINT ["python", "-u", "/app/hello_batch.py", "--help"]
