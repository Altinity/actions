#!/usr/bin/env python3
import argparse
import os
import re
import requests

from clickhouse_driver import Client
from datetime import timedelta

DATABASE_HOST_VAR = "CHECKS_DATABASE_HOST"
DATABASE_USER_VAR = "CHECKS_DATABASE_USER"
DATABASE_PASSWORD_VAR = "CHECKS_DATABASE_PASSWORD"


def get_tag_commit(clickhouse_tag, repo="ClickHouse/ClickHouse"):
    # Get the commit associated with the tag
    tag_api_url = f"https://api.github.com/repos/{repo}/git/refs/tags/{clickhouse_tag}"
    tag_response = requests.get(tag_api_url)
    tag_details = tag_response.json()

    if "object" not in tag_details:
        print(f"Error: Tag {clickhouse_tag} not found")
        return

    if tag_details["object"]["type"] == "tag":
        # If it's an annotated tag, get the commit it points to
        tag_object_url = tag_details["object"]["url"]
        tag_object_response = requests.get(tag_object_url)
        tag_object_details = tag_object_response.json()
        return tag_object_details["object"]["sha"]
    else:
        # If it's a lightweight tag, it points directly to the commit
        return tag_details["object"]["sha"]


def get_checks_fails(
    client: Client, job_url=None, commit_sha=None, include_broken=False
):
    """
    Get tests that did not succeed for the given job URL or commit.
    """
    assert not (job_url and commit_sha)
    if job_url:
        where_clause = f"task_url='{job_url}'"
    elif commit_sha:
        where_clause = f"commit_sha='{commit_sha}'"
    else:
        raise ValueError("Either job_url or commit_sha must be provided")

    # , check_start_time as start_time
    columns = "splitByString(' [', check_name)[1] as check_group, splitByString(' [', check_name)[2] as group_id, "
    columns += "test_name, check_status, test_status, report_url as link"
    statuses = "'FAIL', 'ERROR'"
    if include_broken:
        statuses += ", 'BROKEN'"

    query = f"""SELECT {columns}
                FROM (
                    SELECT
                    check_name,
                    test_name,
                    argMax(check_status, check_start_time) as check_status,
                    argMax(test_status, check_start_time) as test_status,
                    argMax(report_url, check_start_time) as report_url
                    FROM `gh-data`.checks
                    WHERE {where_clause}
                    GROUP BY check_name, test_name
                )
                WHERE (test_status IN ({statuses})
                OR check_status=='error')
                ORDER BY test_name, check_group
                """
    statuses = client.query_dataframe(query)
    statuses["group_id"] = statuses["group_id"].str.strip("]")
    return statuses


def get_checks_statuses(client: Client, checks_fails, job_url=None, commit_sha=None):
    """
    Get statuses of all checks for the given job URL or commit.
    """
    assert not (job_url and commit_sha), "Either job_url or commit_sha must be provided"
    if job_url:
        where_clause = f"task_url='{job_url}'"
    elif commit_sha:
        where_clause = f"commit_sha='{commit_sha}'"
    else:
        raise ValueError("Either job_url or commit_sha must be provided")

    tests = tuple(
        (row["check_group"], row["test_name"]) for _, row in checks_fails.iterrows()
    )

    query = f"""SELECT 
                  splitByString(' [', check_name)[1] as check_group, 
                  test_name,
                  argMax(check_status, check_start_time) as check_status,
                  argMax(test_status, check_start_time) as test_status,
                  argMax(report_url, check_start_time) as link
                FROM `gh-data`.checks
                WHERE {where_clause}
                AND (check_group, test_name) IN {tests}
                GROUP BY check_group, test_name
                ORDER BY test_name, check_group
                """
    statuses = client.query_dataframe(query)
    return statuses


def get_upstream_statuses(checks_fails, commit_sha=None, clickhouse_version=None):
    """
    Get statuses of all checks for the given commit or version.
    """
    assert not (
        clickhouse_version and commit_sha
    ), "Either clickhouse_version or commit_sha must be provided"
    if clickhouse_version:
        where_clause = f"head_ref='{clickhouse_version}'"
    elif commit_sha:
        where_clause = f"commit_sha='{commit_sha}'"
    else:
        raise ValueError("Either clickhouse_version or commit must be provided")

    tests = tuple(
        (row["check_group"], row["test_name"])
        for _, row in checks_fails.iterrows()
        if not row["check_group"].startswith("Sign")
        and not row["test_name"].startswith(
            ("Killed by signal", "Server died", "Check timeout expired")
        )
    )
    print("Will check status of", len(tests), "upstream tests")
    assert len(tests) > 0

    query = f"""SELECT 
                  splitByString(' [', check_name)[1] as check_group, 
                  test_name,
                  argMax(check_status, check_start_time) as check_status,
                  argMax(test_status, check_start_time) as test_status,
                  argMax(report_url, check_start_time) as link,
                  max(check_start_time) as start_time
                FROM default.checks
                WHERE {where_clause}
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


def merge_statuses(checks_fails_1, checks_fails_2, suffixes=("_1", "_2")):
    combined_df = (
        checks_fails_1.merge(
            checks_fails_2,
            on=["check_group", "test_name"],
            suffixes=suffixes,
            how="left",
        )
        .astype(str)
        .replace("nan", "N/A")
    )

    return combined_df


def compare_to_upstream(
    db_client, actions_run_url, clickhouse_version, include_broken=False
):

    checks_fails = get_checks_fails_for_job(
        db_client, actions_run_url, include_broken=include_broken
    )

    upstream_statuses = get_upstream_statuses_for_version(
        checks_fails, clickhouse_version
    )

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

    return combined_df


def compare_two_runs(
    db_client, actions_run_url_1, actions_run_url_2, include_broken=False
):

    checks_fails_1 = get_checks_fails_for_job(
        db_client, actions_run_url_1, include_broken=include_broken
    )

    checks_fails_2 = get_checks_statuses_for_job(
        db_client, actions_run_url_2, checks_fails_1
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

    return combined_df


def print_results_md(results, drop_columns=None):
    if drop_columns is None:
        drop_columns = []
    print(
        results.drop(
            columns=drop_columns,
            errors="ignore",
        ).to_markdown(index=False)
    )


def export_results_csv(results, filename):
    results.to_csv(
        f"{filename}.csv",
        index=False,
    )


def format_ref_md(ref):
    if ref.startswith("v"):
        return f"[{ref}](https://github.com/Altinity/ClickHouse/releases/tag/{ref})"
    elif ref.startswith("https://github.com/") and "/actions/runs/" in ref:
        return f"[Workflow Run ({ref.split('/')[-1]})]({ref})"
    elif len(ref) == 40:
        return (
            f"[Commit ({ref[:7]})](https://github.com/Altinity/ClickHouse/commit/{ref})"
        )
    else:
        return ref


def export_results_md(
    previous_results,
    upstream_results,
    current_ref,
    previous_ref,
    upstream_ref,
):
    # Convert bare URLs to markdown links
    for results in [previous_results, upstream_results]:
        if results is not None:
            for column in results.columns:
                if results[column].dtype != "object":
                    continue
                mask = results[column].astype(str).str.startswith("https://", na=False)
                results.loc[mask, column] = results.loc[mask, column].apply(
                    lambda x: f"[Results]({x})"
                )

    with open(f"comparison_results.md", "w") as f:
        f.write("# Comparison of test failures\n\n")
        f.write(f"Altinity Ref: {format_ref_md(current_ref)}\n\n")
        if previous_results is not None:
            f.write("## Compare with Previous Version\n\n")
            f.write(f"Previous Ref: {format_ref_md(previous_ref)}\n\n")
            f.write(previous_results.to_markdown(index=False))
            f.write("\n\n")
        if upstream_results is not None:
            f.write("## Compare with Upstream Version\n\n")
            f.write(f"Upstream Ref: {format_ref_md(upstream_ref)}\n\n")
            f.write(upstream_results.to_markdown(index=False))
            f.write("\n\n")
    print("Comparison results exported to comparison_results.md")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare CI failures with previous and upstream versions."
    )
    parser.add_argument(
        "--current-ref",
        required=True,
        help="Reference to make comparisons against. Ref can be a workflow url, commit hash, or git tag.",
    )
    parser.add_argument(
        "--previous-ref",
        help="Reference to compare with. Ref can be a workflow url, commit hash, or git tag.",
    )
    parser.add_argument(
        "--upstream-ref",
        help="Reference to compare with. Ref can be a MAJOR.MINOR version, commit hash or git tag.",
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

    if not (args.upstream_ref or args.previous_ref):
        print("Error: Either --upstream-ref or --previous-ref must be provided")
        exit(1)

    if (
        args.current_ref.startswith("https://github.com/")
        and "/actions/runs/" in args.current_ref
    ):
        current_failures = get_checks_fails(db_client, job_url=args.current_ref)
    elif args.current_ref.startswith("v"):
        current_failures = get_checks_fails(
            db_client,
            commit_sha=get_tag_commit(args.current_ref, repo="Altinity/ClickHouse"),
        )
    elif len(args.current_ref) == 40:
        current_failures = get_checks_fails(db_client, commit_sha=args.current_ref)
    else:
        print("Error: --current-ref must be a workflow url, commit hash, or git tag")
        exit(1)

    previous_failures = None
    if args.previous_ref:
        if (
            args.previous_ref.startswith("https://github.com/")
            and "/actions/runs/" in args.previous_ref
        ):
            previous_failures = get_checks_statuses(
                db_client, current_failures, job_url=args.previous_ref
            )
        elif args.previous_ref.startswith("v"):
            previous_failures = get_checks_statuses(
                db_client,
                current_failures,
                commit_sha=get_tag_commit(
                    args.previous_ref, repo="Altinity/ClickHouse"
                ),
            )
        elif len(args.previous_ref) == 40:
            previous_failures = get_checks_statuses(
                db_client, current_failures, commit_sha=args.previous_ref
            )
        else:
            print(
                "Error: --previous-ref must be a workflow url, commit hash, or git tag"
            )
            exit(1)

    upstream_failures = None
    if args.upstream_ref:
        if re.match(r"\d+\.\d+", args.upstream_ref):
            upstream_failures = get_upstream_statuses(
                current_failures, clickhouse_version=args.upstream_ref
            )
        elif args.upstream_ref.startswith("v"):
            upstream_failures = get_upstream_statuses(
                current_failures,
                commit_sha=get_tag_commit(
                    args.upstream_ref, repo="ClickHouse/ClickHouse"
                ),
            )
        elif len(args.upstream_ref) == 40:
            upstream_failures = get_upstream_statuses(
                current_failures, commit_sha=args.upstream_ref
            )
        else:
            print(
                "Error: --upstream-ref must be a MAJOR.MINOR version, commit hash or git tag"
            )
            exit(1)

    previous_combined_results = None
    if args.previous_ref:
        print("\nComparing with previous version")
        previous_combined_results = merge_statuses(
            current_failures, previous_failures, suffixes=("_current", "_previous")
        )
        print_results_md(
            previous_combined_results, drop_columns=["link_current", "link_previous"]
        )

    upstream_combined_results = None
    if args.upstream_ref:
        print("\nComparing with upstream version")
        upstream_combined_results = merge_statuses(
            current_failures, upstream_failures, suffixes=("_current", "_upstream")
        )
        print_results_md(
            upstream_combined_results, drop_columns=["link_current", "link_upstream"]
        )

    # export_results_csv(results, filename)
    export_results_md(
        previous_combined_results,
        upstream_combined_results,
        args.current_ref,
        args.previous_ref,
        args.upstream_ref,
    )


if __name__ == "__main__":
    main()
