FROM python:3.11-slim

WORKDIR /app

# Bibliotecas de sistema que o Docling precisa por baixo dos panos
# (processamento de imagem/PDF). Se o build reclamar de outra faltando,
# é normal na primeira tentativa — só adicionar na lista.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
