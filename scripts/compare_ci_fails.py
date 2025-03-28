#!/usr/bin/env python3
import argparse
import os

from clickhouse_driver import Client
from datetime import timedelta

DATABASE_HOST_VAR = "CHECKS_DATABASE_HOST"
DATABASE_USER_VAR = "CHECKS_DATABASE_USER"
DATABASE_PASSWORD_VAR = "CHECKS_DATABASE_PASSWORD"


def get_checks_fails(client: Client, job_url: str, include_broken=False):
    """
    Get tests that did not succeed for the given job URL.
    """
    # , check_start_time as start_time
    columns = "splitByString(' [', check_name)[1] as check_group, splitByString(' [', check_name)[2] as group_id, "
    columns += "test_name, check_status, test_status"
    statuses = "'FAIL', 'ERROR'"
    if include_broken:
        statuses += ", 'BROKEN'"

    query = f"""SELECT {columns} FROM `gh-data`.checks
                WHERE task_url='{job_url}'
                AND (test_status IN ({statuses})
                OR check_status=='error')
                ORDER BY test_name, check_group
                """
    statuses = client.query_dataframe(query)
    statuses["group_id"] = statuses["group_id"].str.strip("]")
    return statuses


def get_checks_statuses(client: Client, job_url: str, checks_fails):
    """
    Get statuses of all checks for the given job URL.
    """
    tests = tuple(
        (row["check_group"], row["test_name"]) for _, row in checks_fails.iterrows()
    )

    columns = "splitByString(' [', check_name)[1] as check_group, "
    columns += "test_name, check_status, test_status"
    query = f"""SELECT {columns} FROM `gh-data`.checks
                WHERE task_url='{job_url}'
                AND (check_group, test_name) IN {tests}
                ORDER BY test_name, check_group
                """

    return client.query_dataframe(query)


def get_upstream_statuses(checks_fails, clickhouse_version):
    tests = tuple(
        (row["check_group"], row["test_name"])
        for _, row in checks_fails.iterrows()
        if not row["check_group"].startswith("Sign")
        and not row["test_name"].startswith(
            ("Killed by signal", "Server died", "Check timeout expired")
        )
    )
    print("Will check status of", len(tests), "upstream tests")
    # print('Tests:', tests)
    # return
    # --max(check_start_time) as start_time
    query = f"""SELECT 
                  splitByString(' [', check_name)[1] as check_group, 
                  test_name,
                  argMax(check_status, check_start_time) as check_status,
                  argMax(test_status, check_start_time) as test_status,
                  max(check_start_time) as start_time
                FROM default.checks
                WHERE head_ref='{clickhouse_version}'
                AND (check_group, test_name) IN {tests}
                GROUP BY check_group, test_name
                ORDER BY test_name, check_group
                """
    # print('Query:', query)

    client = Client(
        host="play.clickhouse.com",
        user="play",
        port=9440,
        secure="y",
        verify=True,
        settings={"use_numpy": True},
    )
    upstream_statuses = client.query_dataframe(query)

    # There are some "test results" that only get logged on failure,
    # Make sure that they are not accidentally included in the set of latest results,
    tolerance = timedelta(hours=3)
    latest_start_time = upstream_statuses["start_time"].max()
    upstream_statuses = upstream_statuses[
        upstream_statuses["start_time"] >= latest_start_time - tolerance
    ].drop(columns=["start_time"])

    return upstream_statuses


def compare_to_upstream(db_client, args):

    checks_fails = get_checks_fails(
        db_client, args.actions_run_url, include_broken=args.broken
    )

    upstream_statuses = get_upstream_statuses(checks_fails, args.clickhouse_version)

    combined_df = checks_fails.merge(
        upstream_statuses,
        on=["check_group", "test_name"],
        suffixes=("_altinity", "_upstream"),
        how="left",
    ).fillna(
        {
            "check_status_upstream": "N/A",
            "test_status_upstream": "N/A",
            "start_time_upstream": "N/A",
            "start_time": "N/A",
        }
    )

    print(combined_df.to_markdown(index=False))
    combined_df.to_csv(
        f"compared_fails_{args.clickhouse_version}_{args.actions_run_url.split('/')[-1]}.csv",
        index=False,
    )


def compare_two_runs(db_client, args):

    checks_fails_1 = get_checks_fails(
        db_client, args.actions_run_url, include_broken=args.broken
    )

    checks_fails_2 = get_checks_statuses(
        db_client, args.actions_run_url_2, checks_fails_1
    )
    print(len(checks_fails_1), len(checks_fails_2))

    combined_df = (
        checks_fails_1.merge(
            checks_fails_2,
            on=["check_group", "test_name"],
            suffixes=("_1", "_2"),
            how="left",
        )
        .astype(str)
        .replace("nan", "N/A")
    )

    print(combined_df.to_markdown(index=False))
    combined_df.to_csv(
        f"compared_fails_{args.actions_run_url.split('/')[-1]}_{args.actions_run_url_2.split('/')[-1]}.csv",
        index=False,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a combined CI report.")
    parser.add_argument(
        "--actions-run-url", required=True, help="URL of the actions run"
    )
    parser.add_argument(
        "--clickhouse-version",
        help="MAJOR.MINOR version of upstream ClickHouse, e.g. 24.12. Exclusive with --actions-run-url-2",
    )
    parser.add_argument(
        "--actions-run-url-2",
        help="URL of a second actions run to compare against. Exclusive with --clickhouse-version",
    )
    parser.add_argument(
        "--broken",
        action="store_true",
        help="Include BROKEN tests",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    db_client = Client(
        host=os.getenv(DATABASE_HOST_VAR),
        user=os.getenv(DATABASE_USER_VAR),
        password=os.getenv(DATABASE_PASSWORD_VAR),
        port=9440,
        secure="y",
        verify=False,
        settings={"use_numpy": True},
    )

    if args.clickhouse_version and args.actions_run_url_2:
        print("Error: --clickhouse-version and --actions-run-url-2 are exclusive")
        exit(1)

    if args.clickhouse_version:
        compare_to_upstream(db_client, args)

    elif args.actions_run_url_2:
        compare_two_runs(db_client, args)

    else:
        print("Error: --clickhouse-version or --actions-run-url-2 is required")
        exit(1)


if __name__ == "__main__":
    main()
