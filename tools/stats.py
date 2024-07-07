import argparse
import json
import locale
import logging
import os
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Optional

import matplotlib.pyplot as plt
from dotenv import load_dotenv
from elasticsearch import Elasticsearch
from elasticsearch_dsl import Q, Search
from elasticsearch_dsl.query import Query
from matplotlib.ticker import ScalarFormatter

logging.basicConfig()
logger = logging.getLogger("stats")
logger.setLevel(logging.INFO)

DEFAULT_COLOR = "grey"
DEFAULT_LABEL = "Other"

# All operators currently approved by Google: https://www.gstatic.com/ct/log_list/v3/log_list.json
OPERATOR_PATTERNS = [
    r"^(Google)",
    r"^(Cloudflare)",
    r"^(DigiCert)",
    r"^(Sectigo)",
    r"^(Let's Encrypt)",
    r"^(Trust\s?Asia)",
]

# Pattern to parse a certificate subject
# group 1: C, group 2: CN, group 3: L, group 4: O
SUBJECT_PATTERN = r"\/c=(\w{2})\/cn=([\w\s\.,'-]+)(?:\/l=([\s\w]+))?\/o=([\w\s\.,'-]+)"


# ***************************
# * Helpers                 *
# ***************************
@dataclass
class TimeRangeFilter:
    start: datetime
    end: datetime
    field: str

    def to_range_filter(self) -> dict:
        return {
            "range": {
                self.field: {
                    # convert seconds to milliseconds
                    "gte": int(self.start.timestamp() * 1_000),
                    "lt": int(self.end.timestamp() * 1_000),
                }
            }
        }

    def to_query(self) -> Query:
        args = {
            self.field: {
                # convert seconds to milliseconds
                "gte": int(self.start.timestamp() * 1_000),
                "lt": int(self.end.timestamp() * 1_000),
            }
        }
        return Q("range", **args)

    def human(self) -> str:
        """Human readable representaion of date range"""
        actual_end_date = (self.end - timedelta(days=1)).date()
        return f"{self.start.date().isoformat()} - {actual_end_date.isoformat()}"


@dataclass
class PlotOptions:
    show_legend: bool = False
    horizontal_grid: bool = False
    bottom_padding: Optional[float] = None
    y_scale: Literal["linear", "log"] = "linear"
    figsize: tuple[int, int] = (10, 6)
    interactive: bool = False
    format: Literal["png", "pdf"] = "png"


def _name_to_label_and_color(name: str) -> tuple[str, str]:
    """Label and color code for Root CA name or CT Log operator, returns a default value if no match is found

    For available color codes see: https://matplotlib.org/stable/gallery/color/named_colors.html
    """
    if "Amazon" in name:
        return "Amazon", "darkorange"
    elif "Certainly" in name:
        return "Certainly", "firebrick"
    elif "Cloudflare" in name:
        return "Cloudflare", "orange"
    elif "cPanel" in name:
        return "cPanel", "gold"
    elif "DigiCert" in name:
        return "DigiCert", "dodgerblue"
    elif "GlobalSign" in name:
        return "GlobalSign", "coral"
    elif "GoDaddy" in name:
        return "GoDaddy", "aquamarine"
    elif "Google" in name:
        return "Google", "crimson"
    elif "IdenTrust" in name:
        return "IdenTrust", "violet"
    elif "Microsoft" in name:
        return "Microsoft", "springgreen"
    elif "Let's Encrypt" in name:
        return "Let's Encrypt", "darkblue"
    elif "Sectigo" in name:
        return "Sectigo", "green"
    elif "Trust Asia" in name:
        return "Trust Asia", "black"
    elif "ZeroSSL" in name:
        return "ZeroSSL", "darkviolet"

    return DEFAULT_LABEL, DEFAULT_COLOR


def _lifetime_to_label_and_color(lifetime: int) -> tuple[str, str]:
    """Convert lifetime a label and a color

    For available color codes see: https://matplotlib.org/stable/gallery/color/named_colors.html
    """
    days = lifetime / 86_400

    if days <= 30:
        return "0 - 30 days", "fuchsia"
    elif days <= 100:
        return "31 - 100 days", "green"
    elif days <= 200:
        return "101 - 200 days", "navy"
    elif days <= 370:
        return "201 - 370 days", "royalblue"
    else:
        return "> 370 days", "firebrick"


# ***************************
# * Elastic Queries         *
# ***************************
def _stats_agg(
    elastic: Elasticsearch,
    index_name: str,
    field_name: str,
    queries: list[Query],
) -> tuple[dict[str, float], dict[str, float]]:
    s = Search(using=elastic, index=index_name)

    if len(queries) == 1:
        s = s.query(queries[0])
    elif len(queries) > 1:
        bool_query = Q("bool", must=queries)
        s = s.query(bool_query)

    stats_agg_name = "stats_agg"
    percentiles_agg_name = "percentiles_agg"

    s.aggs.metric(stats_agg_name, "extended_stats", field=field_name)
    s.aggs.metric(
        percentiles_agg_name,
        "percentiles",
        field=field_name,
        percents=[2, 25, 50, 75, 98],
    )

    s = s[:0]  # set size to 0

    logger.debug(json.dumps(s.to_dict(), indent=4))

    res = s.execute()
    logger.info(f"Retreived statistics for field {field_name} in {res['took']}ms")

    return res.aggs[stats_agg_name], res.aggs[percentiles_agg_name]["values"]  # type: ignore


def _terms_bucket_agg(
    elastic: Elasticsearch,
    index_name: str,
    field_name: str,
    size: int,
    queries: list[Query],
) -> dict[str, int]:
    s = Search(using=elastic, index=index_name)

    agg_name = f"{field_name}_aggregation"

    if len(queries) == 1:
        s = s.query(queries[0])
    elif len(queries) > 1:
        bool_query = Q("bool", must=queries)
        s = s.query(bool_query)

    s.aggs.bucket(agg_name, "terms", field=field_name, size=size)
    s = s[:0]  # set size to 0

    logger.debug(json.dumps(s.to_dict(), indent=4))

    res = s.execute()
    buckets = res.aggs[agg_name]["buckets"]

    logger.info(
        f"Retreived {len(buckets)} buckets for aggregation {agg_name} in {res['took']}ms"  # type: ignore
    )

    return {bucket["key"]: bucket["doc_count"] for bucket in buckets}  # type: ignore


def _histogram_bucket_agg(
    elastic: Elasticsearch,
    index_name: str,
    field_name: str,
    queries: list[Query],
) -> dict[str, int]:
    s = Search(using=elastic, index=index_name)

    agg_name = f"{field_name}_aggregation"

    if len(queries) == 1:
        s = s.query(queries[0])
    elif len(queries) > 1:
        bool_query = Q("bool", must=queries)
        s = s.query(bool_query)

    s.aggs.bucket(
        agg_name,
        "date_histogram",
        field=field_name,
        calendar_interval="day",
        format="yyyy-MM-dd",
    )
    s = s[:0]  # set size to 0

    logger.debug(json.dumps(s.to_dict(), indent=4))

    res = s.execute()
    buckets = res.aggs[agg_name]["buckets"]

    logger.info(
        f"Retreived {len(buckets)} buckets for aggregation {agg_name} in {res['took']}ms"  # type: ignore
    )

    return {bucket["key_as_string"]: bucket["doc_count"] for bucket in buckets}  # type: ignore


# ***************************
# * Plotting                *
# ***************************
def plot_boxplot(
    title: str,
    xlabel: str,
    ylabel: str,
    low: float,
    q1: float,
    med: float,
    q3: float,
    high: float,
    n: float,
    plot_options: PlotOptions,
    time_range_filter: Optional[TimeRangeFilter] = None,
) -> None:
    logger.debug(f"{low=:.2f} {q1=:.2f} {med=:.2f} {q3=:.2f} {high=:.2f} {n=:_}")

    _, ax = plt.subplots()
    locale.setlocale(locale.LC_ALL, "de_DE.utf8")
    boxes = [
        {
            "label": f"{xlabel} (n = {locale.format_string("%.0f", n, grouping=True)})",
            "whislo": low,  # Bottom whisker position
            "q1": q1,  # First quartile (25th percentile)
            "med": med,  # Median (50th percentile)
            "q3": q3,  # Third quartile (75th percentile)
            "whishi": high,  # Top whisker position
            "fliers": [],  # Outliers
        }
    ]
    ax.bxp(boxes, showfliers=False)
    ax.set_ylabel(ylabel)

    if time_range_filter:
        ax.set_title(f"{title} ({time_range_filter.human()})")
    else:
        ax.set_title(title)

    if plot_options.horizontal_grid:
        ax.grid(axis="y")

    filename = (
        f"{re.sub(r"[^a-z0-9]", "-", title.lower(), flags=re.I)}.{plot_options.format}"
    )
    plt.savefig(filename)
    logger.info(f"Wrote boxplot '{title}' to {filename}")

    if plot_options.interactive:
        plt.show()


def plot_double_boxplot(
    title: str,
    xlabel_1: str,
    xlabel_2: str,
    ylabel: str,
    low_1: float,
    q1_1: float,
    med_1: float,
    q3_1: float,
    high_1: float,
    n_1: float,
    low_2: float,
    q1_2: float,
    med_2: float,
    q3_2: float,
    high_2: float,
    n_2: float,
    plot_options: PlotOptions,
    time_range_filter: Optional[TimeRangeFilter] = None,
) -> None:
    logger.debug(
        f"{xlabel_1:<20}: {low_1=:.2f} {q1_1=:.2f} {med_1=:.2f} {q3_1=:.2f} {high_1=:.2f} {n_1=:_}"
    )
    logger.debug(
        f"{xlabel_2:<20}: {low_2=:.2f} {q1_2=:.2f} {med_2=:.2f} {q3_2=:.2f} {high_2=:.2f} {n_2=:_}"
    )

    _, ax = plt.subplots()
    locale.setlocale(locale.LC_ALL, "de_DE.utf8")
    boxes = [
        {
            "label": f"{xlabel_1}\nn = {locale.format_string("%.0f", n_1, grouping=True)}",
            "whislo": low_1,  # Bottom whisker position
            "q1": q1_1,  # First quartile (25th percentile)
            "med": med_1,  # Median (50th percentile)
            "q3": q3_1,  # Third quartile (75th percentile)
            "whishi": high_1,  # Top whisker position
            "fliers": [],  # Outliers
        },
        {
            "label": f"{xlabel_2}\nn = {locale.format_string("%.0f", n_2, grouping=True)}",
            "whislo": low_2,  # Bottom whisker position
            "q1": q1_2,  # First quartile (25th percentile)
            "med": med_2,  # Median (50th percentile)
            "q3": q3_2,  # Third quartile (75th percentile)
            "whishi": high_2,  # Top whisker position
            "fliers": [],  # Outliers
        },
    ]
    ax.bxp(boxes, showfliers=False)
    ax.set_ylabel(ylabel)

    if time_range_filter:
        ax.set_title(f"{title} ({time_range_filter.human()})")
    else:
        ax.set_title(title)

    if plot_options.horizontal_grid:
        ax.grid(axis="y")

    filename = (
        f"{re.sub(r"[^a-z0-9]", "-", title.lower(), flags=re.I)}.{plot_options.format}"
    )
    plt.savefig(filename)
    logger.info(f"Wrote boxplot '{title}' to {filename}")

    if plot_options.interactive:
        plt.show()


def plot_barchart(
    x_values: Collection[str],
    y_values: Collection[int],
    title: str,
    x_label: str,
    y_label: str,
    x_processor: Callable[[list[str]], tuple[list[str], list[str], list[str]]],
    plot_options: PlotOptions,
    limit: int = 10,
    time_range_filter: Optional[TimeRangeFilter] = None,
) -> None:
    if len(x_values) != len(y_values):
        raise ValueError(f"Values for x and y axis must have the same number of items!")

    x, y = _filter_values(x_values, y_values, limit)
    x, colors, labels = x_processor(x)

    for x_val, y_val, color in zip(x, y, colors, strict=True):
        logger.debug(f"{x_val:>46} [{color:<11}]: {y_val:_}")

    fig, ax = plt.subplots(figsize=plot_options.figsize)

    if plot_options.show_legend:
        ax.bar(x, y, color=colors, label=labels)
        ax.legend(title=x_label)
    else:
        ax.bar(x, y, color=colors)

    if time_range_filter:
        ax.set_title(f"{title} ({time_range_filter.human()})")
    else:
        ax.set_title(title)

    locale.setlocale(locale.LC_ALL, "de_DE.utf8")
    ax.set_ylabel(
        f"{y_label} (n = {locale.format_string("%.0f", sum(y_values), grouping=True)})"
    )
    ax.set_xlabel(
        f"{x_label} (n = {locale.format_string("%.0f", len(x_values), grouping=True)})"
    )

    ax.set_yscale(plot_options.y_scale)
    if plot_options.y_scale != "log":
        ax.yaxis.set_major_formatter(ScalarFormatter(useOffset=False, useLocale=True))
        ax.yaxis.get_major_formatter().set_scientific(False)

    # Rotate the x-axis labels for long labels
    plt.xticks(rotation=20, ha="right")

    if plot_options.horizontal_grid:
        ax.grid(axis="y")

    if plot_options.bottom_padding:
        fig.subplots_adjust(bottom=plot_options.bottom_padding)

    filename = (
        f"{re.sub(r"[^a-z0-9]", "-", title.lower(), flags=re.I)}.{plot_options.format}"
    )
    plt.savefig(filename)
    logger.info(f"Wrote barchart '{title}' to {filename}")

    if plot_options.interactive:
        plt.show()


def plot_lifetime_barchart(
    x_values: Collection[str],
    y_values: Collection[int],
    title: str,
    x_label: str,
    y_label: str,
    plot_options: PlotOptions,
    limit: int = 10,
    time_range_filter: Optional[TimeRangeFilter] = None,
) -> None:
    if len(x_values) != len(y_values):
        raise ValueError(f"Values for x and y axis must have the same number of items!")

    x, y = _filter_values(x_values, y_values, limit)

    colors = [DEFAULT_COLOR] * len(x_values)
    labels = [DEFAULT_LABEL] * len(x_values)
    for i, lifetime in enumerate(x_values):
        label, color = _lifetime_to_label_and_color(int(lifetime))
        colors[i] = color

        if label in labels:
            # add redundant labels with a leading unserscore to prevent duplicate entries in the legend
            labels[i] = f"_{label}"
        else:
            labels[i] = label

    for x_val, y_val, color in zip(x, y, colors, strict=True):
        logger.debug(f"{x_val:>46} [{color:<11}]: {y_val:_}")

    fig, ax = plt.subplots(figsize=plot_options.figsize)
    locale.setlocale(locale.LC_ALL, "de_DE.utf8")

    processed_x_values = list(
        map(lambda x: f"{locale.format_string("%.0f", x, grouping=True)}s", x)
    )

    if plot_options.show_legend:
        ax.bar(processed_x_values, y, color=colors, label=labels)
        ax.legend(title=x_label)
        # Really dirty and hardcoded hack to change the order of the legend
        # see also: https://www.statology.org/matplotlib-legend-order/
        handles, labels = plt.gca().get_legend_handles_labels()
        # specify order of items in legend
        order = [4, 0, 3, 1, 2]
        plt.legend([handles[idx] for idx in order], [labels[idx] for idx in order])
    else:
        ax.bar(processed_x_values, y, color=colors)

    if time_range_filter:
        ax.set_title(f"{title} ({time_range_filter.human()})")
    else:
        ax.set_title(title)

    locale.setlocale(locale.LC_ALL, "de_DE.utf8")
    ax.set_ylabel(
        f"{y_label} (n = {locale.format_string("%.0f", sum(y_values), grouping=True)})"
    )
    ax.set_xlabel(
        f"{x_label} (n = {locale.format_string("%.0f", len(x_values), grouping=True)})"
    )

    ax.set_yscale(plot_options.y_scale)
    if plot_options.y_scale != "log":
        ax.yaxis.set_major_formatter(ScalarFormatter(useOffset=False, useLocale=True))
        ax.yaxis.get_major_formatter().set_scientific(False)

    # https://www.geeksforgeeks.org/adding-value-labels-on-a-matplotlib-bar-chart/
    for i in range(len(x)):
        plt.text(i, y[i], f"{int(x[i]) / 86400:.0f}d", ha="center")

    plt.xticks(rotation=20, ha="right")

    if plot_options.horizontal_grid:
        ax.grid(axis="y")

    if plot_options.bottom_padding:
        fig.subplots_adjust(bottom=plot_options.bottom_padding)

    filename = (
        f"{re.sub(r"[^a-z0-9]", "-", title.lower(), flags=re.I)}.{plot_options.format}"
    )
    plt.savefig(filename)
    logger.info(f"Wrote barchart '{title}' to {filename}")

    if plot_options.interactive:
        plt.show()


# ***************************
# * Data processing         *
# ***************************
def _filter_values(
    x_values: Collection[str], y_values: Collection[int], limit: int = 10
) -> tuple[list[str], list[int]]:
    assert len(x_values) == len(y_values)

    # convert sequences to lists
    if not isinstance(x_values, list):
        x_values = list(x_values)

    if not isinstance(y_values, list):
        y_values = list(y_values)

    # trim lists if necessary
    if len(x_values) > limit:
        cutoff_y_values = y_values[limit - 1 :]
        other_y_values = sum(cutoff_y_values)

        x_values = x_values[:limit]
        y_values = y_values[:limit]

        y_values[limit - 1] = other_y_values
        x_values[limit - 1] = f"Other ({len(cutoff_y_values)})"

    return x_values, y_values


def _default_processor(collection: list[str]) -> tuple[list[str], list[str], list[str]]:
    colors = ["blue"] * len(collection)
    labels = [DEFAULT_LABEL] * len(collection)

    return collection, colors, labels


def _process_ctlog_names(ctlogs: list[str]) -> tuple[list[str], list[str], list[str]]:
    colors = [DEFAULT_COLOR] * len(ctlogs)
    labels = [DEFAULT_LABEL] * len(ctlogs)

    # remove quotes around log names
    ctlogs = list(map(lambda x: re.sub(r"'(\w+)'", r"\1", x, flags=re.I), ctlogs))

    # remove log suffixes
    ctlogs = list(map(lambda x: re.sub(r"\slog$", "", x, flags=re.I), ctlogs))

    for i, logname in enumerate(ctlogs):
        label, color = _name_to_label_and_color(logname)
        colors[i] = color

        if label in labels:
            # add redundant labels with a leading unserscore to prevent duplicate entries in the legend
            labels[i] = f"_{label}"
        else:
            labels[i] = label

    return ctlogs, colors, labels


def _process_root_ca_names(
    root_cas: list[str],
) -> tuple[list[str], list[str], list[str]]:
    colors = [DEFAULT_COLOR] * len(root_cas)
    labels = [DEFAULT_LABEL] * len(root_cas)
    ca_names = [""] * len(root_cas)

    for i, ca_name in enumerate(root_cas):
        if match := re.match(SUBJECT_PATTERN, ca_name, flags=re.I):
            cn = match.group(2)
            o = match.group(4)

            if len(cn) > 3:
                ca_names[i] = cn
            else:
                ca_names[i] = f"{o} {cn}"

            label, color = _name_to_label_and_color(o)
            colors[i] = color

            if label in labels:
                # add redundant labels with a leading unserscore to prevent duplicate entries in the legend
                labels[i] = f"_{label}"
            else:
                labels[i] = label
        else:
            ca_names[i] = ca_name
            label, color = _name_to_label_and_color(ca_name)
            colors[i] = color

            if label in labels:
                # add redundant labels with a leading unserscore to prevent duplicate entries in the legend
                labels[i] = f"_{label}"
            else:
                labels[i] = label

    return ca_names, colors, labels


def _agg_root_cas(buckets: dict[str, int]) -> dict[str, int]:
    aggregated_cas: dict[str, int] = dict()

    for ca_name, certificates in buckets.items():
        if match := re.match(SUBJECT_PATTERN, ca_name, flags=re.I):
            idx = match.group(4)
        else:
            idx = ca_name

        aggregated_cas.setdefault(idx, 0)
        aggregated_cas[idx] += certificates

    return dict(sorted(aggregated_cas.items(), key=lambda item: item[1], reverse=True))


def _agg_ctlogs(buckets: dict[str, int]) -> dict[str, int]:
    aggregated_ctlogs: dict[str, int] = dict()

    for log_name, certificates in buckets.items():
        operator = log_name

        for operator_pattern in OPERATOR_PATTERNS:
            if match := re.match(operator_pattern, log_name, flags=re.I):
                operator = match.group(1)
                break

        # Trust Asia uses two different spellings of their name ffs -_-
        if operator == "TrustAsia":
            operator = "Trust Asia"

        aggregated_ctlogs.setdefault(operator, 0)
        aggregated_ctlogs[operator] += certificates

    return dict(
        sorted(aggregated_ctlogs.items(), key=lambda item: item[1], reverse=True)
    )


# ***************************
# * Main logic              *
# ***************************
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        default=False,
        help="Show interactive plots",
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        default=False,
        help="Activate debug logging",
    )
    parser.add_argument(
        "-f", "--format", choices=["png", "pdf"], default="png", help="Output format"
    )
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    load_dotenv()
    elastic_url = os.getenv("ELASTIC_URL")
    elastic_api_key = os.getenv("ELASTIC_API_KEY")
    ctlog_index_name = os.getenv("CTLOG_INDEX_NAME")

    assert ctlog_index_name != None

    es = Elasticsearch(elastic_url, api_key=elastic_api_key, request_timeout=60)
    ci = es.info()
    logger.info(
        f"Connected to {ci['name']} ({ci['cluster_name']}) at {elastic_url} running elasticsearch v{ci['version']['number']}"
    )

    time_range_filter = TimeRangeFilter(
        datetime(2024, 6, 19, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2024, 7, 3, 0, 0, 0, tzinfo=timezone.utc),
        "seen",
    )
    range_filter = time_range_filter.to_query()
    logger.info(f"Time range is {time_range_filter.human()}")

    precert_filter = Q("match", update_type="PrecertLogEntry")
    x509cert_filter = Q("match", update_type="X509LogEntry")

    boxplot_options = PlotOptions(
        horizontal_grid=True, interactive=args.interactive, format=args.format
    )
    bar_chart_options = PlotOptions(
        bottom_padding=0.18,
        show_legend=True,
        interactive=args.interactive,
        format=args.format,
    )
    bar_chart_log_options = PlotOptions(
        bottom_padding=0.18,
        show_legend=True,
        y_scale="log",
        interactive=args.interactive,
        format=args.format,
    )

    # Bar chart CT log entries per day
    buckets = _histogram_bucket_agg(es, ctlog_index_name, "seen", [range_filter])
    plot_barchart(
        buckets.keys(),
        buckets.values(),
        "CT log entries per day",
        "Days",
        "PrecertLogEntries & X509LogEntries",
        _default_processor,
        PlotOptions(
            bottom_padding=0.18,
            show_legend=False,
            interactive=args.interactive,
            format=args.format,
        ),
        limit=14,
        time_range_filter=time_range_filter,
    )

    # Bar chart Unique CT log entries in old index
    old_range = TimeRangeFilter(
        datetime(2024, 5, 16, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2024, 6, 17, 0, 0, 0, tzinfo=timezone.utc),
        "seen",
    )
    buckets = _histogram_bucket_agg(es, "ctlog-prod", "seen", [old_range.to_query()])
    plot_barchart(
        buckets.keys(),
        buckets.values(),
        "Unique CT log entries",
        "Days",
        "CT log entries",
        _default_processor,
        PlotOptions(
            bottom_padding=0.18,
            figsize=(16, 6),
            interactive=args.interactive,
            format=args.format,
        ),
        limit=32,
        time_range_filter=old_range,
    )

    # Combined boxplot encoded certificate sizes
    stats_1, percentiles_1 = _stats_agg(
        es, ctlog_index_name, "encoded_size", [range_filter, precert_filter]
    )
    stats_2, percentiles_2 = _stats_agg(
        es, ctlog_index_name, "encoded_size", [range_filter, x509cert_filter]
    )
    plot_double_boxplot(
        "Encoded certificate size",
        "PrecertLogEntries",
        "X509LogEntries",
        "Byte",
        percentiles_1["2.0"],
        percentiles_1["25.0"],
        percentiles_1["50.0"],
        percentiles_1["75.0"],
        percentiles_1["98.0"],
        stats_1["count"],
        percentiles_2["2.0"],
        percentiles_2["25.0"],
        percentiles_2["50.0"],
        percentiles_2["75.0"],
        percentiles_2["98.0"],
        stats_2["count"],
        boxplot_options,
        time_range_filter=time_range_filter,
    )

    # Boxplot precertificate lifetime
    stats, percentiles = _stats_agg(
        es, ctlog_index_name, "lifetime", [range_filter, precert_filter]
    )
    plot_boxplot(
        "Precertificate lifetime",
        "Precertificates",
        "Lifetime in days",
        percentiles["2.0"] / 86400,
        percentiles["25.0"] / 86400,
        percentiles["50.0"] / 86400,
        percentiles["75.0"] / 86400,
        percentiles["98.0"] / 86400,
        stats["count"],
        boxplot_options,
        time_range_filter=time_range_filter,
    )

    # Bar chart: Certificate chain length
    buckets = _terms_bucket_agg(
        es, ctlog_index_name, "chain_length", 10, [range_filter]
    )
    plot_barchart(
        list(map(str, buckets.keys())),
        buckets.values(),
        "Certificate chain length",
        "Chain length excluding leaf",
        "Certificates",
        _default_processor,
        PlotOptions(
            bottom_padding=0.18,
            y_scale="log",
            interactive=args.interactive,
            format=args.format,
        ),
        limit=12,
        time_range_filter=time_range_filter,
    )

    # Bar chart: Logged certificates per CT log
    buckets = _terms_bucket_agg(
        es, ctlog_index_name, "ctlog_source_name", 100, [range_filter]
    )
    plot_barchart(
        buckets.keys(),
        buckets.values(),
        "Logged certificates per CT log",
        "Certificate Transparency Logs",
        "PrecertLogEntries & X509LogEntries",
        _process_ctlog_names,
        PlotOptions(
            bottom_padding=0.18,
            show_legend=True,
            interactive=args.interactive,
            format=args.format,
        ),
        limit=12,
        time_range_filter=time_range_filter,
    )

    # Bar chart: Logged certificates per CT log operator
    aggregated_ctlogs = _agg_ctlogs(buckets)
    plot_barchart(
        aggregated_ctlogs.keys(),
        aggregated_ctlogs.values(),
        "Logged certificates per CT log operator",
        "Log operators",
        "PrecertLogEntries & X509LogEntries",
        _process_ctlog_names,
        PlotOptions(
            bottom_padding=0.18,
            y_scale="log",
            interactive=args.interactive,
            format=args.format,
        ),
        time_range_filter=time_range_filter,
    )

    # Bar chart: Logged precertificates per CT log
    buckets = _terms_bucket_agg(
        es,
        ctlog_index_name,
        "ctlog_source_name",
        100,
        [range_filter, precert_filter],
    )
    plot_barchart(
        buckets.keys(),
        buckets.values(),
        "Logged precertificates per CT log",
        "Certificate Transparency Logs",
        "PrecertLogEntries",
        _process_ctlog_names,
        bar_chart_options,
        limit=12,
        time_range_filter=time_range_filter,
    )

    # Bar chart: Logged precertificates per CT log operator
    aggregated_ctlogs = _agg_ctlogs(buckets)
    plot_barchart(
        aggregated_ctlogs.keys(),
        aggregated_ctlogs.values(),
        "Logged precertificates per CT log operator",
        "Log operators",
        "PrecertLogEntries",
        _process_ctlog_names,
        PlotOptions(
            bottom_padding=0.18,
            y_scale="log",
            interactive=args.interactive,
            format=args.format,
        ),
        time_range_filter=time_range_filter,
    )

    # Bar chart: Issued precertificates per root CA certificate
    buckets = _terms_bucket_agg(
        es, ctlog_index_name, "root_ca_name", 1000, [range_filter, precert_filter]
    )
    plot_barchart(
        buckets.keys(),
        buckets.values(),
        "Issued precertificates per root CA certificate",
        "Root certificates",
        "Issued precertificates",
        _process_root_ca_names,
        PlotOptions(
            bottom_padding=0.28,
            show_legend=True,
            interactive=args.interactive,
            format=args.format,
        ),
        limit=12,
        time_range_filter=time_range_filter,
    )

    # Bar chart: Issued precertificates per root CA
    aggregated_cas = _agg_root_cas(buckets)
    plot_barchart(
        aggregated_cas.keys(),
        aggregated_cas.values(),
        "Issued precertificates per root CA",
        "Root Certificate Authorities",
        "Issued precertificates",
        _process_root_ca_names,
        PlotOptions(
            bottom_padding=0.18,
            y_scale="log",
            interactive=args.interactive,
            format=args.format,
        ),
        limit=12,
        time_range_filter=time_range_filter,
    )

    # Bar chart: Top precertificate lifetimes
    top_n = 14
    buckets = _terms_bucket_agg(
        es, ctlog_index_name, "lifetime", top_n, [range_filter, precert_filter]
    )
    plot_lifetime_barchart(
        buckets.keys(),
        buckets.values(),
        f"Top {top_n} precertificate lifetimes",
        "Precertificate lifetime",
        "Precertificates",
        bar_chart_log_options,
        limit=top_n,
        time_range_filter=time_range_filter,
    )


if __name__ == "__main__":
    main()
