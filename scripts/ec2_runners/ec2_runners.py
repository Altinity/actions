#!/usr/bin/env python3
import argparse
import os
import yaml
import boto3
import requests
import time
import sys
from pathlib import Path

# Add the parent directory to Python path to find the lib module
script_dir = Path(__file__).absolute()
parent_dir = script_dir.parent.parent  # Go up two levels to reach scripts/
sys.path.append(str(parent_dir))

from lib.actions import Action

Action.set_logger("ec2_runners")


def get_github_token():
    """Retrieves the GitHub token from the environment variable."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN environment variable not set.")
    return token


def get_runner_registration_token(github_repo, token):
    """Gets a registration token from the GitHub API."""
    url = (
        f"https://api.github.com/repos/{github_repo}/actions/runners/registration-token"
    )
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    response = requests.post(url, headers=headers)
    response.raise_for_status()
    return response.json()["token"]


def get_github_runners(repo, token):
    """Gets runners from the GitHub API."""
    url = f"https://api.github.com/repos/{repo}/actions/runners"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()["runners"]


def remove_github_runner(repo, token, runner_id):
    """Removes a runner from GitHub."""
    url = f"https://api.github.com/repos/{repo}/actions/runners/{runner_id}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    response = requests.delete(url, headers=headers)
    if response.status_code == 204:
        return True
    else:
        print(f"Failed to remove runner {runner_id}: {response.status_code}")
        return False


def get_github_runners_by_labels(repo, token, target_labels):
    """Get GitHub runners that have any of the target labels."""
    runners = get_github_runners(repo, token)
    matching_runners = []

    for runner in runners:
        runner_labels = [label.get("name") for label in runner.get("labels", [])]
        if all(label in runner_labels for label in target_labels):
            matching_runners.append(runner)

    return matching_runners


def get_existing_instances(ec2, repo, labels):
    """Get instances that correspond to GitHub runners with specific labels."""
    # Get GitHub runners with these labels
    github_token = get_github_token()
    matching_runners = get_github_runners_by_labels(repo, github_token, labels)

    # Get all instances for this repo
    filters = [
        {"Name": "tag:GitHubRepo", "Values": [repo]},
        {
            "Name": "instance-state-name",
            "Values": ["running", "pending", "stopping", "stopped"],
        },
    ]
    response = ec2.describe_instances(Filters=filters)

    all_instances = []
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            all_instances.append(instance)

    # Match instances to GitHub runners by name
    matching_instances = []
    for instance in all_instances:
        instance_name = None
        for tag in instance.get("Tags", []):
            if tag["Key"] == "Name":
                instance_name = tag["Value"]
                break

        if instance_name and any(
            runner.get("name") == instance_name for runner in matching_runners
        ):
            matching_instances.append(instance)

    return matching_instances


def deploy_runners(args):
    """Deploy GitHub self-hosted runners on EC2."""
    try:
        with Action("Getting GitHub token") as action:
            github_token = get_github_token()
            action.note("GitHub token retrieved successfully")

        with Action("Loading configuration") as action:
            with open(args.config, "r") as f:
                config = yaml.safe_load(f)

            repo = config["repo"]
            region = config["region"]
            runner_configs = config["runners"]
            default_disk_size = config.get(
                "default_disk_size", 20
            )  # Default to 20 GB if not specified
            action.note(f"Repository: {repo}")
            action.note(f"Region: {region}")
            action.note(f"Runner configurations: {len(runner_configs)}")
            action.note(f"Default disk size: {default_disk_size} GB")

        # Get networking configuration from config file
        vpc_id = config.get("vpc_id")
        subnet_id = config.get("subnet_id")
        security_group_id = config.get("security_group_id")

        if not vpc_id:
            raise ValueError("vpc_id not specified in config file")
        if not subnet_id:
            raise ValueError("subnet_id not specified in config file")

        with Action("Loading user data script") as action:
            with open(args.user_data, "r") as f:
                user_data_template = f.read()
            action.note(f"User data script loaded: {args.user_data}")

        ec2 = boto3.client("ec2", region_name=region)

        # Create security group if not specified
        if not security_group_id:
            with Action("Setting up security group") as action:
                # Create a basic security group for runners
                sg_name = f"github-runner-sg-{repo.replace('/', '-')}"
                try:
                    # Check if security group already exists
                    sgs = ec2.describe_security_groups(
                        Filters=[
                            {"Name": "group-name", "Values": [sg_name]},
                            {"Name": "vpc-id", "Values": [vpc_id]},
                        ]
                    )
                    if sgs["SecurityGroups"]:
                        security_group_id = sgs["SecurityGroups"][0]["GroupId"]
                        action.note(
                            f"Using existing security group: {security_group_id}"
                        )
                    else:
                        # Create new security group
                        response = ec2.create_security_group(
                            GroupName=sg_name,
                            Description=f"Security group for GitHub runners in {repo}",
                            VpcId=vpc_id,
                        )
                        security_group_id = response["GroupId"]

                        # Add comprehensive rules for GitHub runners
                        action.note(f"Creating security group: {security_group_id}")

                        # Inbound rules
                        inbound_rules = [
                            # SSH access
                            {
                                "IpProtocol": "tcp",
                                "FromPort": 22,
                                "ToPort": 22,
                                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                            },
                            # ICMP (ping) for connectivity testing
                            {
                                "IpProtocol": "icmp",
                                "FromPort": -1,
                                "ToPort": -1,
                                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                            },
                            # HTTP for package downloads
                            {
                                "IpProtocol": "tcp",
                                "FromPort": 80,
                                "ToPort": 80,
                                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                            },
                            # HTTPS for GitHub API and secure downloads
                            {
                                "IpProtocol": "tcp",
                                "FromPort": 443,
                                "ToPort": 443,
                                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                            },
                        ]

                        ec2.authorize_security_group_ingress(
                            GroupId=security_group_id, IpPermissions=inbound_rules
                        )

                        # Try to add outbound rules, but don't fail if they already exist
                        try:
                            outbound_rules = [
                                {
                                    "IpProtocol": "-1",
                                    "FromPort": -1,
                                    "ToPort": -1,
                                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                }
                            ]

                            ec2.authorize_security_group_egress(
                                GroupId=security_group_id, IpPermissions=outbound_rules
                            )
                        except Exception as e:
                            if "Duplicate" in str(e):
                                action.note(
                                    "Outbound rules already exist (this is fine)"
                                )
                            else:
                                action.note(
                                    f"Warning: Could not add outbound rules: {e}"
                                )

                        action.note(f"Created security group: {security_group_id}")
                except Exception as e:
                    action.note(f"Error creating security group: {e}")
                    action.note("Cannot continue without proper security group")
                    return

        for runner_config in runner_configs:
            instance_type = runner_config["instance_type"]
            ami_id = runner_config["ami_id"]
            count = runner_config["count"]
            labels = runner_config["labels"]
            disk_size = runner_config.get("disk_size", default_disk_size)

            with Action(f"Processing runner config: {instance_type}") as action:
                action.note(f"Instance type: {instance_type}")
                action.note(f"AMI: {ami_id}")
                action.note(f"Target count: {count}")
                action.note(f"Disk size: {disk_size} GB")
                action.note(f"Labels: {', '.join(labels)}")

                # Check existing instances
                existing_instances = get_existing_instances(ec2, repo, labels)
                existing_count = len(existing_instances)

                action.note(f"Found {existing_count} existing instance(s)")

                if existing_count >= count and not args.force:
                    action.note(
                        f"Target count ({count}) already met. Skipping creation. Use --force to override."
                    )
                    continue

                instances_to_create = (
                    count - existing_count if not args.force else count
                )

                if instances_to_create <= 0:
                    action.note(f"No new instances needed")
                    continue

                action.note(f"Will create {instances_to_create} new instance(s)")

                # Create unique instance name with timestamp and index
                timestamp = int(time.time())
                for i in range(instances_to_create):
                    with Action(
                        f"Creating instance {i+1}/{instances_to_create}"
                    ) as instance_action:
                        reg_token = get_runner_registration_token(repo, github_token)

                        instance_name = f"github-ec2-runner-{repo.replace('/', '-')}-{instance_type}-{timestamp}-{i+1}"

                        user_data = (
                            user_data_template.replace(
                                "${github_repo_url}", f"https://github.com/{repo}"
                            )
                            .replace("${runner_labels}", ",".join(labels))
                            .replace("${runner_token}", reg_token)
                            .replace("${runner_name}", instance_name)
                        )

                        instance_action.note(f"Instance name: {instance_name}")

                        # Get the AMI's block device mapping to find the correct root device name
                        ami_info = ec2.describe_images(ImageIds=[ami_id])
                        if not ami_info["Images"]:
                            raise ValueError(f"AMI {ami_id} not found")

                        ami_block_mappings = ami_info["Images"][0].get(
                            "BlockDeviceMappings", []
                        )
                        root_device_name = None

                        # Find the root device (usually the first EBS device)
                        for mapping in ami_block_mappings:
                            if mapping.get("Ebs"):
                                root_device_name = mapping["DeviceName"]
                                break

                        if not root_device_name:
                            # Fallback to common device names
                            root_device_name = "/dev/xvda"
                            instance_action.note(
                                f"Could not determine root device from AMI, using default: {root_device_name}"
                            )
                        else:
                            instance_action.note(
                                f"Using root device from AMI: {root_device_name}"
                            )

                        # Build run_instances parameters
                        run_params = {
                            "ImageId": ami_id,
                            "InstanceType": instance_type,
                            "MinCount": 1,
                            "MaxCount": 1,
                            "UserData": user_data,
                            "SubnetId": subnet_id,
                            "BlockDeviceMappings": [
                                {
                                    "DeviceName": root_device_name,
                                    "Ebs": {
                                        "VolumeSize": disk_size,
                                        "VolumeType": "gp3",  # Use GP3 for better performance
                                        "DeleteOnTermination": True,
                                    },
                                }
                            ],
                            "TagSpecifications": [
                                {
                                    "ResourceType": "instance",
                                    "Tags": [
                                        {"Key": "Name", "Value": instance_name},
                                        {"Key": "GitHubRepo", "Value": repo},
                                    ],
                                },
                            ],
                        }

                        # Add security group if available
                        if security_group_id:
                            run_params["SecurityGroupIds"] = [security_group_id]

                        instances = ec2.run_instances(**run_params)

                        instance_action.note(
                            f"Successfully launched: {instances['Instances'][0]['InstanceId']}"
                        )

    except (ValueError, FileNotFoundError, requests.exceptions.RequestException) as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


def undeploy_runners(args):
    """Undeploy GitHub self-hosted runners from EC2."""
    try:
        with Action("Getting GitHub token") as action:
            github_token = get_github_token()
            action.note("GitHub token retrieved successfully")

        with Action("Loading configuration") as action:
            with open(args.config, "r") as f:
                config = yaml.safe_load(f)

            repo = args.repo or config["repo"]
            region = config["region"]
            labels = args.labels or []
            action.note(f"Repository: {repo}")
            action.note(f"Region: {region}")
            if labels:
                action.note(f"Label filter: {labels}")
            else:
                action.note("No label filter - will find all instances")

        ec2 = boto3.client("ec2", region_name=region)

        # If no labels specified, get all instances for the repo
        if not labels:
            with Action("Finding all instances for repository") as action:
                filters = [
                    {"Name": "tag:GitHubRepo", "Values": [repo]},
                    {
                        "Name": "instance-state-name",
                        "Values": ["running", "pending", "stopping", "stopped"],
                    },
                ]
                response = ec2.describe_instances(Filters=filters)
                instances = []
                for reservation in response["Reservations"]:
                    for instance in reservation["Instances"]:
                        instances.append(instance)
                action.note(f"Found {len(instances)} instances")
        else:
            with Action("Finding instances with specific labels") as action:
                instances = get_existing_instances(ec2, repo, labels)
                action.note(f"Found {len(instances)} instances")

        if not instances:
            with Action("No instances found") as action:
                action.note(f"Repository: {repo}")
                if labels:
                    action.note(f"Labels: {labels}")
            return

        with Action("Preparing to terminate instances") as action:
            action.note(f"Found {len(instances)} instance(s) to terminate:")
            for instance in instances:
                instance_name = "Unknown"
                for tag in instance.get("Tags", []):
                    if tag["Key"] == "Name":
                        instance_name = tag["Value"]
                        break
                action.note(f"  - {instance['InstanceId']} ({instance_name})")

            if not args.force:
                response = input("\nDo you want to terminate these instances? (y/N): ")
                if response.lower() != "y":
                    action.note("Operation cancelled by user")
                    return

        # Get GitHub runners to match with instances
        with Action("Fetching GitHub runners") as action:
            github_runners = get_github_runners(repo, github_token)

            # Create a mapping of runner names to runner IDs
            runner_map = {}
            for runner in github_runners:
                runner_name = runner.get("name", "")
                if "github-ec2-runner" in runner_name:
                    runner_map[runner_name] = runner["id"]
            action.note(f"Found {len(runner_map)} GitHub runners to deregister")

        # Terminate instances and deregister runners
        terminated_count = 0
        deregistered_count = 0

        for instance in instances:
            instance_id = instance["InstanceId"]
            instance_name = "Unknown"
            for tag in instance.get("Tags", []):
                if tag["Key"] == "Name":
                    instance_name = tag["Value"]
                    break

            with Action(f"Terminating instance: {instance_id}") as action:
                action.note(f"Instance name: {instance_name}")

                # Try to deregister the runner from GitHub first
                if instance_name in runner_map:
                    runner_id = runner_map[instance_name]
                    action.note(f"Deregistering runner from GitHub (ID: {runner_id})")
                    if remove_github_runner(repo, github_token, runner_id):
                        action.note("✅ Runner deregistered successfully")
                        deregistered_count += 1
                    else:
                        action.note("⚠️  Failed to deregister runner, but continuing")
                else:
                    action.note("⚠️  No matching GitHub runner found")

                # Terminate the EC2 instance
                try:
                    ec2.terminate_instances(InstanceIds=[instance_id])
                    action.note("✅ Instance terminated successfully")
                    terminated_count += 1
                except Exception as e:
                    action.note(f"❌ Failed to terminate instance: {e}")

        with Action("Summary") as action:
            action.note(f"Instances terminated: {terminated_count}/{len(instances)}")
            action.note(f"Runners deregistered: {deregistered_count}/{len(instances)}")

            if terminated_count > 0:
                action.note(
                    "Note: It may take a few minutes for runners to disappear from GitHub."
                )

    except (ValueError, FileNotFoundError, requests.exceptions.RequestException) as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


def list_runners(args):
    """List GitHub runners and EC2 instances."""
    try:
        with Action("Getting GitHub token") as action:
            github_token = get_github_token()
            action.note("GitHub token retrieved successfully")

        with Action("Loading configuration") as action:
            with open(args.config, "r") as f:
                config = yaml.safe_load(f)

            repo = args.repo or config["repo"]
            region = config["region"]
            action.note(f"Repository: {repo}")
            action.note(f"Region: {region}")

        with Action("Fetching GitHub runners") as action:
            # Get GitHub runners
            github_runners = get_github_runners(repo, github_token)
            action.note(f"Total runners: {len(github_runners)}")

            # Filter for EC2 runners
            ec2_runners = []
            for runner in github_runners:
                if "github-ec2-runner" in runner.get("name", ""):
                    ec2_runners.append(runner)

            action.note(f"EC2 runners: {len(ec2_runners)}")

            for runner in ec2_runners:
                status = runner.get("status", "unknown")
                status_icon = (
                    "🟢"
                    if status == "online"
                    else "🔴" if status == "offline" else "🟡"
                )

                # Extract labels
                label_names = [
                    label.get("name", "") for label in runner.get("labels", [])
                ]

                action.note(f"{status_icon} {runner['name']}")
                action.note(f"   Status: {status}")
                action.note(f"   Labels: {', '.join(label_names)}")

                # Show recent activity if available
                if runner.get("busy"):
                    action.note(f"   ⚡ Currently busy")

        with Action("Fetching EC2 instances") as action:
            # Check EC2 instances
            ec2 = boto3.client("ec2", region_name=region)

            # Get instances for this repo
            filters = [
                {"Name": "tag:GitHubRepo", "Values": [repo]},
                {
                    "Name": "instance-state-name",
                    "Values": ["running", "pending", "stopping", "stopped"],
                },
            ]

            response = ec2.describe_instances(Filters=filters)
            instances = []
            for reservation in response["Reservations"]:
                for instance in reservation["Instances"]:
                    instances.append(instance)

            action.note(f"EC2 instances: {len(instances)}")
            for instance in instances:
                state = instance["State"]["Name"]
                state_icon = (
                    "🟢" if state == "running" else "🟡" if state == "pending" else "🔴"
                )

                # Get tags
                tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
                name = tags.get("Name", "Unknown")

                action.note(f"{state_icon} {name} ({instance['InstanceId']})")
                action.note(f"   State: {state}")
                action.note(f"   Type: {instance['InstanceType']}")
                if instance.get("PublicIpAddress"):
                    action.note(f"   IP: {instance['PublicIpAddress']}")

    except Exception as e:
        print(f"Error: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Manage GitHub self-hosted runners on EC2.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Deploy runners
  python3 ec2_runners.py deploy

  # Deploy with force (override existing count)
  python3 ec2_runners.py deploy --force

  # Undeploy specific labels
  python3 ec2_runners.py undeploy --labels arm64 test

  # List all runners
  python3 ec2_runners.py list

  # Undeploy with force (no confirmation)
  python3 ec2_runners.py undeploy --force
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Deploy command
    deploy_parser = subparsers.add_parser("deploy", help="Deploy GitHub runners")
    deploy_parser.add_argument(
        "--config",
        default="runner_config.yaml",
        help="Path to the runner configuration file.",
    )
    deploy_parser.add_argument(
        "--user-data",
        default="setup_runner.sh",
        help="Path to the runner setup script.",
    )
    deploy_parser.add_argument(
        "--force",
        action="store_true",
        help="Force creation of new instances even if target count is already met.",
    )

    # Undeploy command
    undeploy_parser = subparsers.add_parser("undeploy", help="Undeploy GitHub runners")
    undeploy_parser.add_argument(
        "--config",
        default="runner_config.yaml",
        help="Path to the runner configuration file.",
    )
    undeploy_parser.add_argument(
        "--repo",
        help="GitHub repository in 'owner/repo' format. If not provided, will use the repo from config file.",
    )
    undeploy_parser.add_argument(
        "--labels",
        nargs="+",
        help="Specific labels to filter instances. If not provided, will terminate all instances for the repo.",
    )
    undeploy_parser.add_argument(
        "--force",
        action="store_true",
        help="Force termination without confirmation.",
    )

    # List command
    list_parser = subparsers.add_parser(
        "list", help="List GitHub runners and EC2 instances"
    )
    list_parser.add_argument(
        "--config",
        default="runner_config.yaml",
        help="Path to the runner configuration file.",
    )
    list_parser.add_argument(
        "--repo",
        help="GitHub repository in 'owner/repo' format. If not provided, will use the repo from config file.",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "deploy":
        deploy_runners(args)
    elif args.command == "undeploy":
        undeploy_runners(args)
    elif args.command == "list":
        list_runners(args)


if __name__ == "__main__":
    main()
