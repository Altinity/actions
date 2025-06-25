#! /usr/bin/env python3
import argparse
import os
import yaml
import boto3


def main():
    parser = argparse.ArgumentParser(
        description="Undeploy GitHub self-hosted runners from EC2."
    )
    parser.add_argument(
        "--config",
        default="runner_config.yaml",
        help="Path to the runner configuration file.",
    )
    parser.add_argument(
        "--repo",
        help="GitHub repository in 'owner/repo' format. If not provided, will use the repo from config file.",
    )
    parser.add_argument(
        "--labels",
        nargs="+",
        help="Specific labels to filter instances. If not provided, will terminate all instances for the repo.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be terminated without actually terminating instances.",
    )
    args = parser.parse_args()

    try:
        # Load config to get region and repo if not provided
        with open(args.config, "r") as f:
            config = yaml.safe_load(f)

        region = config["region"]
        repo = args.repo or config["repo"]

        ec2 = boto3.client("ec2", region_name=region)

        # Build filters for finding instances
        filters = [
            {"Name": "tag:GitHubRepo", "Values": [repo]},
            {
                "Name": "instance-state-name",
                "Values": ["running", "stopped", "pending"],
            },
        ]

        if args.labels:
            # Filter by specific labels
            label_filter = {"Name": "tag:GitHubLabels", "Values": args.labels}
            filters.append(label_filter)

        print(f"Searching for instances with repo: {repo}")
        if args.labels:
            print(f"Filtering by labels: {', '.join(args.labels)}")

        response = ec2.describe_instances(Filters=filters)

        instances_to_terminate = []
        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                instances_to_terminate.append(instance)

        if not instances_to_terminate:
            print("No instances found matching the criteria.")
            return

        print(f"\nFound {len(instances_to_terminate)} instance(s) to terminate:")
        for instance in instances_to_terminate:
            instance_name = "Unknown"
            instance_labels = "Unknown"
            for tag in instance.get("Tags", []):
                if tag["Key"] == "Name":
                    instance_name = tag["Value"]
                elif tag["Key"] == "GitHubLabels":
                    instance_labels = tag["Value"]

            print(f"  - {instance['InstanceId']} ({instance_name})")
            print(f"    State: {instance['State']['Name']}")
            print(f"    Labels: {instance_labels}")
            print(f"    Type: {instance['InstanceType']}")

        if args.dry_run:
            print(
                f"\nDRY RUN: Would terminate {len(instances_to_terminate)} instance(s)"
            )
            return

        # Ask for confirmation
        response = input(
            f"\nAre you sure you want to terminate {len(instances_to_terminate)} instance(s)? (yes/no): "
        )
        if response.lower() != "yes":
            print("Operation cancelled.")
            return

        # Terminate instances
        instance_ids = [instance["InstanceId"] for instance in instances_to_terminate]
        ec2.terminate_instances(InstanceIds=instance_ids)

        print(f"Successfully initiated termination of {len(instance_ids)} instance(s):")
        for instance_id in instance_ids:
            print(f"  - {instance_id}")

    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    main()
