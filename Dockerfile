# Imagen base recomendada para Cloud Run + OpenSSL 3
FROM python:3.10-slim-bookworm

# Evita prompts en apt
ENV DEBIAN_FRONTEND=noninteractive

# Instalar dependencias nativas requeridas por Azure Speech SDK
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libasound2 \
    libgomp1 \
    libssl3 \
  && rm -rf /var/lib/apt/lists/*

# Crear directorio app
WORKDIR /app


# Copiar requirements e instalar
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código
COPY . /app
# si usas credenciales por archivo:
ENV GOOGLE_CREDENTIALS_FILE=/app/credentials.json

# Opcional: logging de Azure Speech para debug (puedes quitarlo luego)
ENV AZURE_SPEECH_LOGGING_ENABLE=1

# Exponer puerto para Cloud Run
ENV PORT=8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
