FROM python:3.12-alpine

COPY ../requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY stream_to_queue_publisher.py .

CMD python stream_to_queue_publisher.py

