from __future__ import annotations

import argparse
from datetime import datetime
from time import perf_counter_ns

import numpy as np
import websocket
from rich.console import Console
from rich.table import Table


class CertstreamStats:
    def __init__(self, num_measurements: int = 100):
        self.batch_size = 1000
        self.cert_size = np.empty(self.batch_size)
        self.i = 0
        self.start = perf_counter_ns()

        self.m = 0
        self.num_measurements = num_measurements
        self.cert_rate = np.zeros(self.num_measurements)
        self.data_rate = np.zeros(self.num_measurements)
        self.msg_size_avg = np.zeros(self.num_measurements)

    def __enter__(self) -> CertstreamStats:
        self.start_time = datetime.now().astimezone()
        print(
            f"Performing {self.num_measurements} measurements on batches of {self.batch_size} certificates"
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        now = datetime.now().astimezone()
        duration = now - self.start_time
        print()
        print(f"Start time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"End time:   {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"Duration:   {duration.total_seconds():.2f}s")

    def cert_callback(self, ws: websocket.WebSocket, message) -> None:
        self.cert_size[self.i] = len(message)
        self.i += 1

        if self.i == self.batch_size:
            delta_s = (perf_counter_ns() - self.start) / 1_000_000_000
            cert_rate = self.batch_size / delta_s
            data_rate = self.cert_size.sum() / delta_s / 1_000_000

            self.cert_rate[self.m] = cert_rate
            self.data_rate[self.m] = data_rate
            self.msg_size_avg[self.m] = np.average(self.cert_size)

            self.m += 1
            self.i = 0
            self.start = perf_counter_ns()

            print(
                f"[{self.m:>4}/{self.num_measurements}] {cert_rate:>4.0f}c/s {data_rate:>5.2f}MB/s (avg size: {np.average(self.cert_size):.0f}B σ={np.std(self.cert_size):.0f})"
            )

        if self.m == self.num_measurements:
            self.print_measurements()
            ws.close()

    def print_measurements(self) -> None:
        table = Table(
            title=f"Statistics for {self.num_measurements} measurements on batches of {self.batch_size} certificates"
        )
        table.add_column("Metric")
        table.add_column("Unit")
        table.add_column("Avg", justify="right")
        table.add_column("Std", justify="right")
        table.add_column("Med", justify="right")
        table.add_column("95th", justify="right")
        table.add_column("99th", justify="right")

        # Certificate rate
        cert_rate_avg = np.average(self.cert_rate)
        cert_rate_std = np.std(self.cert_rate)
        cert_rate_med = np.median(self.cert_rate)
        cert_rate_95_percentile = np.percentile(self.cert_rate, 95)
        cert_rate_99_percentile = np.percentile(self.cert_rate, 99)
        table.add_row(
            "Certificate rate",
            "c/s",
            f"{cert_rate_avg:.0f}",
            f"{cert_rate_std:.0f}",
            f"{cert_rate_med:.0f}",
            f"{cert_rate_95_percentile:.0f}",
            f"{cert_rate_99_percentile:.0f}",
        )

        # Message Size
        msg_size_avg = np.average(self.msg_size_avg)
        msg_size_std = np.std(self.msg_size_avg)
        msg_size_med = np.median(self.msg_size_avg)
        msg_size_95_pertcentile = np.percentile(self.msg_size_avg, 95)
        msg_size_99_pertcentile = np.percentile(self.msg_size_avg, 99)
        table.add_row(
            "Message size",
            "Byte",
            f"{msg_size_avg:.0f}",
            f"{msg_size_std:.0f}",
            f"{msg_size_med:.0f}",
            f"{msg_size_95_pertcentile:.0f}",
            f"{msg_size_99_pertcentile:.0f}",
        )

        # Data rate
        data_rate_avg = np.average(self.data_rate)
        data_rate_std = np.std(self.data_rate)
        data_rate_med = np.median(self.data_rate)
        data_rate_95_percentile = np.percentile(self.data_rate, 95)
        data_rate_99_percentile = np.percentile(self.data_rate, 99)
        table.add_row(
            "Data rate",
            "MB/s",
            f"{data_rate_avg:.2f}",
            f"{data_rate_std:.2f}",
            f"{data_rate_med:.2f}",
            f"{data_rate_95_percentile:.2f}",
            f"{data_rate_99_percentile:.2f}",
        )

        # Max. time per certificate
        max_time_avg = 1_000 / cert_rate_avg
        max_time_med = 1_000 / cert_rate_med
        max_time_95 = 1_000 / cert_rate_95_percentile
        max_time_99 = 1_000 / cert_rate_99_percentile
        table.add_row(
            "Max. time/certificate",
            "ms",
            f"{max_time_avg:.2f}",
            f"",
            f"{max_time_med:.2f}",
            f"{max_time_95:.2f}",
            f"{max_time_99:.2f}",
        )

        console = Console()
        console.print()
        console.print(table)

    def on_connect(self, ws: websocket.WebSocket):
        print(f"Connected to {ws.url}")

    def on_error(self, _, error) -> None:
        print(f"Connection error: {error}")


def load_args() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "measurements", metavar="M", type=int, help="Number of measurements to take"
    )

    args = parser.parse_args()
    return args.measurements


def main() -> None:
    measurements = load_args()

    with CertstreamStats(measurements) as cs_stats:
        ws = websocket.WebSocketApp(
            "ws://certstream-server:8080/full-stream",
            on_open=cs_stats.on_connect,
            on_error=cs_stats.on_error,
            on_message=cs_stats.cert_callback,
        )
        ws.run_forever(reconnect=5, ping_interval=20, skip_utf8_validation=True)


if __name__ == "__main__":
    main()
