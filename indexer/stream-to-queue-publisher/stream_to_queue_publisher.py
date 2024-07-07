from __future__ import annotations

import concurrent.futures
import json
import logging
import os
from time import perf_counter_ns

import numpy as np
import pika
import pika.channel
import pika.exceptions
import websocket
from dotenv import load_dotenv

logging.basicConfig()
logger = logging.getLogger("stream-to-queue-publisher")
logger.setLevel(logging.INFO)


class CertstreamBatchEnqueue:
    # Message Queue
    _host: str
    _credentials: pika.PlainCredentials
    _queue: str
    _connection: pika.BlockingConnection
    _channel: pika.channel.Channel

    # Data processing
    _thread_pool: concurrent.futures.ThreadPoolExecutor
    _batch_size: int
    _cache: list[bytes]

    # Metrics
    _counter: int
    _agg_size: int
    _processing_time: np.ndarray

    def __init__(
        self, host: str, user: str, password: str, queue: str, batch_size: int
    ) -> None:
        self._host = host
        self._credentials = pika.PlainCredentials(user, password)
        self._queue = queue
        self._connection = None

        self._thread_pool = concurrent.futures.ThreadPoolExecutor()
        self._batch_size = batch_size
        self._cache = []

        self._counter = 0
        self._agg_size = 10_000
        self._processing_time = np.empty(self._agg_size)

    def __enter__(self) -> CertstreamBatchEnqueue:
        self.__connect()

        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self._connection.close()
        self._thread_pool.shutdown()

    def __connect(self):
        if self._connection and self._connection.is_open:
            self._connection.close()

        self._connection = pika.BlockingConnection(
            pika.ConnectionParameters(self._host, credentials=self._credentials)
        )
        self._channel = self._connection.channel()
        self._channel.queue_declare(queue=self._queue, durable=True)

    def _enqueue_batch(self, batch: list[bytes]) -> None:
        try:
            certs = [json.loads(msg) for msg in batch]
            self._channel.basic_publish(
                exchange="",
                routing_key=self._queue,
                body=json.dumps(certs),
                properties=pika.BasicProperties(
                    delivery_mode=pika.DeliveryMode.Persistent
                ),
            )
        except pika.exceptions.AMQPError as e:
            logger.error(f"Failed to enqueue certificate batch: {e}")
            self.__connect()

    def _print_processing_stats(self) -> None:
        processing_time_avg = np.average(self._processing_time) / 1_000
        processing_time_med = np.median(self._processing_time) / 1_000
        processing_time_95_percentile = np.percentile(self._processing_time, 95) / 1_000
        processing_time_99_percentile = np.percentile(self._processing_time, 99) / 1_000
        logger.info(
            f"Processing time for the last {self._agg_size} certificates: avg {processing_time_avg:>5.3f}μs median {processing_time_med:>5.3f}μs 95th {processing_time_95_percentile:>5.3f}μs 99th {processing_time_99_percentile:>6.3f}μs"
        )

    def cert_callback(self, _, message) -> None:
        start = perf_counter_ns()
        self._cache.append(message)

        if len(self._cache) == self._batch_size:
            self._thread_pool.submit(self._enqueue_batch, self._cache.copy())
            self._cache.clear()

        self._processing_time[self._counter] = perf_counter_ns() - start
        self._counter += 1

        if self._counter == self._agg_size:
            self._print_processing_stats()
            self._counter = 0


def on_open(ws: websocket.WebSocketApp) -> None:
    logger.info(f"Connected to: {ws.url}")


def on_reconnect(ws: websocket.WebSocketApp) -> None:
    logger.warning(f"Reconnected to {ws.url}")


def on_close(ws: websocket.WebSocketApp, close_status_code, close_msg) -> None:
    logger.info(f"Closed connection to {ws.url}: {close_status_code=} {close_msg=}")


def on_error(_, error) -> None:
    logger.error(f"Connection error: {error}")


def main():
    load_dotenv()

    rabbitmq_host = os.getenv("RABBITMQ_HOST")
    rabbitmq_user = os.getenv("RABBITMQ_USER")
    rabbitmq_pass = os.getenv("RABBITMQ_PASSWORD")
    rabbitmq_queue = os.getenv("RABBITMQ_QUEUE_NAME")

    certstream_url = os.getenv("CERTSTREAM_URL")
    batch_size = int(os.getenv("BATCH_SIZE"))

    logger.info(f"Connecting to certstream at {certstream_url}")

    with CertstreamBatchEnqueue(
        rabbitmq_host, rabbitmq_user, rabbitmq_pass, rabbitmq_queue, batch_size
    ) as batch_enqueue:
        ws = websocket.WebSocketApp(
            certstream_url,
            on_open=on_open,
            on_reconnect=on_reconnect,
            on_close=on_close,
            on_error=on_error,
            on_message=batch_enqueue.cert_callback,
        )
        ws.run_forever(reconnect=5, ping_interval=20, skip_utf8_validation=True)


if __name__ == "__main__":
    main()
