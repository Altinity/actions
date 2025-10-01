#! /usr/bin/env python3
import os
import argparse
import requests
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

def main():
    parser = argparse.ArgumentParser(description="Plot GitHub Actions job timelines for a workflow run.")
    parser.add_argument("--owner", help="Repository owner/org (default Altinity)", default="Altinity")
    parser.add_argument("--repo", help="Repository name (default ClickHouse)", default="ClickHouse")
    parser.add_argument("--run-id", required=True, help="Workflow run ID (e.g. 1234567890)")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub token (default from env GITHUB_TOKEN)")
    parser.add_argument("--start-side-threshold", type=float, default=0.75,
                        help="Jobs starting after this fraction of total run get labels at start side (default 0.75)")
    args = parser.parse_args()

    if not args.token:
        raise SystemExit("GitHub token not provided (set --token or env GITHUB_TOKEN)")

    # ---------- Fetch jobs ----------
    headers = {"Authorization": f"Bearer {args.token}", "Accept": "application/vnd.github+json"}
    url = f"https://api.github.com/repos/{args.owner}/{args.repo}/actions/runs/{args.run_id}/jobs"

    jobs = []
    while url:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        for job in data.get("jobs", []):
            if job.get("started_at") and job.get("completed_at"):
                jobs.append({
                    "name": job["name"],
                    "started_at": pd.to_datetime(job["started_at"]),
                    "completed_at": pd.to_datetime(job["completed_at"]),
                })
        # pagination
        url = None
        link = resp.headers.get("Link", "")
        if link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part[part.find("<")+1:part.find(">")]

    df = pd.DataFrame(jobs)
    if df.empty:
        raise SystemExit("No jobs with start/completion timestamps found for this run.")

    df.sort_values("started_at", inplace=True, ascending=False)

    # ---------- Relative times ----------
    t0 = df["started_at"].min()
    df["start_rel"] = (df["started_at"] - t0).dt.total_seconds() / 60.0
    df["end_rel"]   = (df["completed_at"] - t0).dt.total_seconds() / 60.0
    df["duration"]  = df["end_rel"] - df["start_rel"]
    total_run = df["end_rel"].max()

    start_side_threshold = args.start_side_threshold

    # ---------- Plot ----------
    fig_h = max(4, len(df) * 0.35)
    fig, ax = plt.subplots(figsize=(12, fig_h))
    bar_h = 0.5
    label_offset = 0.005 * total_run

    for i, row in enumerate(df.itertuples()):
        ax.barh(i, row.duration, left=row.start_rel, height=bar_h,
                align="center", color="steelblue", edgecolor="k")

        label = row.name
        # 1) If job starts very late
        if row.start_rel > start_side_threshold * total_run:
            x = max(0.0, row.start_rel - label_offset)
            ha, color = "right", "black"
        # 2) Short job: outside to the right
        else:
            x = row.end_rel + label_offset
            ha, color = "left", "black"

        ax.text(x, i, label, va="center", ha=ha, fontsize=8, color=color, clip_on=False)

    ax.set_yticks(range(len(df)))
    ax.set_yticklabels(df["name"], fontsize=8)

    # Format x-axis as H:MM
    def minutes_to_hmm(x, pos):
        total_seconds = int(round(x * 60))
        hrs = total_seconds // 3600
        mins = (total_seconds % 3600) // 60
        return f"{hrs}:{mins:02d}"

    ax.xaxis.set_major_formatter(FuncFormatter(minutes_to_hmm))
    ax.set_xlabel("Time since workflow start (H:MM)")
    ax.set_title(f"GitHub Actions Jobs Timeline — Run {args.run_id}")

    # Padding
    pad = max(1.0, 0.05 * max(1.0, total_run))
    ax.set_xlim(left=0, right=df["end_rel"].max() + pad)

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
