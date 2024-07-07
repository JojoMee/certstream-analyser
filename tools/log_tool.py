import argparse
import base64
import sys

import httpx

GOOGLE_LOG_LIST = "https://www.gstatic.com/ct/log_list/v3/log_list.json"
GOOGLE_ALL_LOGS_LIST = "https://www.gstatic.com/ct/log_list/v3/all_logs_list.json"
APPLE_LOG_LIST = "https://valid.apple.com/ct/log_list/current_log_list.json"


def fetch_google_log_list(all_logs: bool = False) -> list[dict]:
    url = GOOGLE_ALL_LOGS_LIST if all_logs else GOOGLE_LOG_LIST
    res = httpx.get(url).raise_for_status().json()

    operators = res["operators"]
    print(
        f"Fetched Google log list v{res['version']} from {res['log_list_timestamp']} with {len(operators)} operators"
    )

    return operators


def fetch_apple_log_list() -> list[dict]:
    res = httpx.get(APPLE_LOG_LIST).raise_for_status().json()

    operators = res["operators"]
    print(f"Fetched Apple log list v{res['version']} with {len(operators)} operators")

    return operators


def fetch_sth(log_url: str) -> dict | None:
    try:
        return httpx.get(log_url + "ct/v1/get-sth").raise_for_status().json()
    except httpx.HTTPStatusError:
        return None
    except httpx.RequestError as e:
        print(f"Failed fo fetch sth for {log_url} with {type(e).__name__}: {e}")
        return None


def print_operator_logs(operator: dict) -> None:
    logs = operator["logs"]
    print(f"{operator["name"]} ({len(logs)} logs):")

    for log in logs:
        # skip test logs
        if "state" not in log:
            continue

        state = next(iter(log["state"].keys()))
        sth = (
            fetch_sth(log["url"])
            if state in ["usable", "qualified", "retired", "readonly"]
            else None
        )

        print(
            f"    [{state:<9}] {log['description']:<30} {log['url']:<50} {sth['tree_size'] if sth else '':<10} {base64.b64decode(log['log_id']).hex()}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-l",
        "--list",
        choices=["google", "apple"],
        default="google",
        help="List to fetch CT logs from",
    )
    args = parser.parse_args()

    match args.list.lower():
        case "google":
            operators = fetch_google_log_list()
        case "apple":
            operators = fetch_apple_log_list()
        case _:
            print(
                f"Invalid list name {args.l}! Supported values are 'google' or 'apple'"
            )
            sys.exit(1)

    for operator in operators:
        print_operator_logs(operator)


if __name__ == "__main__":
    main()
