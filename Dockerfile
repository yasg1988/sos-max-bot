FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY certs/russian-trusted-ca-bundle.crt /tmp/russian-trusted-ca-bundle.crt
RUN cat /tmp/russian-trusted-ca-bundle.crt >> /etc/ssl/certs/ca-certificates.crt \
    && rm /tmp/russian-trusted-ca-bundle.crt

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py
COPY assets /app/assets

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
