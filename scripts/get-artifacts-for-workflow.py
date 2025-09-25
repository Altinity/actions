#! /usr/bin/env python3

import argparse
import os

import requests

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
GITHUB_REPO = "Altinity/ClickHouse"
S3_BASE_URL = "https://s3.amazonaws.com/altinity-build-artifacts"


def get_run_details(run_url: str) -> dict:
    """
    Fetch run details for a given run URL.
    """
    run_id = run_url.split("/")[-1]

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run_id}"
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        raise Exception(
            f"Failed to fetch run details: {response.status_code} {response.text}"
        )

    return response.json()


def get_full_artifact_url(s3_base_url, pr_number, branch, commit_hash, artifact_name):
    s3_base_url = s3_base_url.rstrip("/")
    if pr_number == 0 or pr_number is None:
        return f"{s3_base_url}/REFs/{branch}/{commit_hash}/{artifact_name}"
    else:
        return f"{s3_base_url}/PRs/{pr_number}/{commit_hash}/{artifact_name}"


def get_artifact_report_url(
    workflow_config, s3_base_url, build_type, pr_number, branch, commit_hash
):
    build_file = f"build_{build_type}/artifact_report_build_{build_type}.json"

    cache_details = workflow_config["cache_artifacts"].get(f"Build ({build_type})")
    if cache_details and cache_details["type"] == "success":
        print(f"Cached build found for {build_type}")
        return get_full_artifact_url(
            s3_base_url,
            cache_details["pr_number"],
            cache_details["branch"],
            cache_details["sha"],
            build_file,
        )

    print(f"No cached build found for {build_type}")
    return get_full_artifact_url(
        s3_base_url,
        pr_number,
        branch,
        commit_hash,
        build_file,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-url", type=str, required=True)
    args = parser.parse_args()

    run_details = get_run_details(args.workflow_url)

    commit_sha = run_details["head_commit"]["id"]
    branch_name = run_details["head_branch"]
    workflow_name = run_details["name"]
    if len(run_details["pull_requests"]) > 0:
        pr_number = run_details["pull_requests"][0]["number"]
    else:
        pr_number = 0

    workflow_config_url = get_full_artifact_url(
        S3_BASE_URL,
        pr_number,
        branch_name,
        commit_sha,
        f"/config_workflow/workflow_config_{workflow_name.lower()}.json",
    )

    print(workflow_config_url)

    r = requests.get(workflow_config_url)
    r.raise_for_status()
    workflow_config = r.json()

    for build in ["amd_release", "arm_release"]:
        build_url = get_artifact_report_url(
            workflow_config, S3_BASE_URL, build, pr_number, branch_name, commit_sha
        )
        n_builds = len(requests.get(build_url).json().get("build_urls", []))
        print(f"Found {n_builds} builds for {build}: {build_url}")


if __name__ == "__main__":
    main()
