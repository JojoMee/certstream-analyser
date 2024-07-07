FROM python:3.12-alpine

COPY ../requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cert_indexer.py .
COPY index_config.json .
COPY ca.crt .

CMD python cert_indexer.py
