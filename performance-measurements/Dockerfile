FROM python:3.12-alpine

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY certstream_stats.py .

ENTRYPOINT [ "python", "certstream_stats.py" ]
