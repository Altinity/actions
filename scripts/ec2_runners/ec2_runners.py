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

from lib.actions import Action, OperationResult

Action.set_logger("ec2_runners")

RUNNER_NAME_PREFIX = "gh-ec2-runner"

class EC2RunnerError(Exception):
    """Base exception for EC2 runner operations."""

    pass


class ConfigurationError(EC2RunnerError):
    """Configuration-related errors."""

    pass


class GitHubAPIError(EC2RunnerError):
    """GitHub API-related errors."""

    pass


class AWSAPIError(EC2RunnerError):
    """AWS API-related errors."""

    pass


def get_github_token():
    """Retrieves the GitHub token from the environment variable."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise ConfigurationError("GITHUB_TOKEN environment variable not set.")
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
    try:
        response = requests.post(url, headers=headers)
        response.raise_for_status()
        return response.json()["token"]
    except requests.exceptions.RequestException as e:
        raise GitHubAPIError(f"Failed to get registration token: {e}")


def get_github_runners(repo, token):
    """Gets runners from the GitHub API."""
    url = f"https://api.github.com/repos/{repo}/actions/runners"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()["runners"]
    except requests.exceptions.RequestException as e:
        raise GitHubAPIError(f"Failed to get runners: {e}")


def remove_github_runner(repo, token, runner_id):
    """Removes a runner from GitHub."""
    url = f"https://api.github.com/repos/{repo}/actions/runners/{runner_id}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        response = requests.delete(url, headers=headers)
        if response.status_code == 204:
            return True
        else:
            raise GitHubAPIError(
                f"Failed to remove runner {runner_id}: {response.status_code}"
            )
    except requests.exceptions.RequestException as e:
        raise GitHubAPIError(f"Failed to remove runner {runner_id}: {e}")


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
        instance_name = get_instance_name_from_tags(instance)
        if instance_name:
            for runner in matching_runners:
                if runner.get("name") == instance_name:
                    matching_instances.append(instance)
                    break
        else:
            print(f"Instance {instance['InstanceId']} has no name tag")
            matching_instances.append(instance)

    return matching_instances


def create_security_group(ec2, repo, vpc_id):
    """Create or get existing security group for GitHub runners."""
    sg_name = f"github-runner-sg-{repo.replace('/', '-')}"

    # Check if security group already exists
    sgs = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [sg_name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )
    if sgs["SecurityGroups"]:
        security_group_id = sgs["SecurityGroups"][0]["GroupId"]
        return security_group_id, f"Using existing security group: {security_group_id}"

    # Create new security group
    response = ec2.create_security_group(
        GroupName=sg_name,
        Description=f"Security group for GitHub runners in {repo}",
        VpcId=vpc_id,
    )
    security_group_id = response["GroupId"]

    # Add comprehensive rules for GitHub runners
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
            pass  # Outbound rules already exist (this is fine)
        else:
            raise e

    return security_group_id, f"Created security group: {security_group_id}"


def get_root_device_name(ec2, ami_id):
    """Get the root device name for an AMI."""
    ami_info = ec2.describe_images(ImageIds=[ami_id])
    if not ami_info["Images"]:
        raise ValueError(f"AMI {ami_id} not found")

    ami_block_mappings = ami_info["Images"][0].get("BlockDeviceMappings", [])

    # Find the root device (usually the first EBS device)
    for mapping in ami_block_mappings:
        if mapping.get("Ebs"):
            return mapping["DeviceName"]

    # Fallback to common device names
    return "/dev/xvda"


def create_runner_instance(
    ec2,
    repo,
    github_token,
    runner_config,
    user_data_template,
    subnet_id,
    security_group_id,
    timestamp,
    index,
    args,
    global_setup_steps=None,
):
    """Create a single runner instance."""
    instance_type = runner_config["instance_type"]
    ami_id = runner_config["ami_id"]
    labels = runner_config["labels"]
    disk_size = runner_config.get("disk_size", 20)

    reg_token = get_runner_registration_token(repo, github_token)
    instance_name = (
        f"{RUNNER_NAME_PREFIX}-{repo.split('/')[1]}-{instance_type}-{timestamp}-{index+1}"
    )
    assert len(instance_name) <= 64, "Instance name must be at most 64 characters"

    # Prepare setup steps for this runner
    runner_setup_steps = runner_config.get("setup_steps", [])
    all_setup_steps = []

    # Add global setup steps first
    if global_setup_steps:
        all_setup_steps.extend(global_setup_steps)

    # Add runner-specific setup steps
    all_setup_steps.extend(runner_setup_steps)

    # Convert setup steps to shell script format
    setup_script = ""
    if all_setup_steps:
        setup_script = "\n# Custom setup steps\n"
        for step in all_setup_steps:
            step_name = step.get("name", "Custom step")
            commands = step.get("commands", [])
            if commands:
                setup_script += f'\nlog "Running: {step_name}"\n'
                for command in commands:
                    setup_script += f"{command}\n"
                setup_script += f'log "Completed: {step_name}"\n'

    user_data = (
        user_data_template.replace("${github_repo_url}", f"https://github.com/{repo}")
        .replace("${runner_labels}", ",".join(labels))
        .replace("${runner_token}", reg_token)
        .replace("${runner_name}", instance_name)
        .replace("${custom_setup_steps}", setup_script)
    )

    # Get the AMI's root device name
    root_device_name = get_root_device_name(ec2, ami_id)

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
                    {"Key": "Purpose", "Value": "github-runner"},
                ],
            },
        ],
    }

    # Add security group if available
    if security_group_id:
        run_params["SecurityGroupIds"] = [security_group_id]

    instances = ec2.run_instances(**run_params)
    return instances["Instances"][0]["InstanceId"], instance_name


def deploy_runners(args):
    """Deploy GitHub self-hosted runners on EC2."""
    result = OperationResult()

    try:
        with Action("Getting GitHub token", ignore_fail=False) as action:
            github_token = get_github_token()
            action.success("GitHub token retrieved successfully")

        with Action("Loading configuration", ignore_fail=False) as action:
            config = load_config(args.config)
            repo = config["repo"]
            region = config["region"]
            runner_configs = config["runners"]
            default_disk_size = config.get("default_disk_size", 40)
            global_setup_steps = config.get("setup_steps", [])
            action.note(f"Repository: {repo}")
            action.note(f"Region: {region}")
            action.note(f"Runner configurations: {len(runner_configs)}")
            action.note(f"Default disk size: {default_disk_size} GB")
            action.note(f"Global setup steps: {len(global_setup_steps)}")

        # Get networking configuration from config file
        vpc_id, subnet_id = validate_networking_config(config)
        security_group_id = config.get("security_group_id")

        with Action("Loading user data script", ignore_fail=False) as action:
            try:
                with open(args.user_data, "r") as f:
                    user_data_template = f.read()
                action.success(f"User data script loaded: {args.user_data}")
            except FileNotFoundError:
                action.error(f"User data script not found: {args.user_data}")
                raise ConfigurationError(
                    f"User data script not found: {args.user_data}"
                )

        ec2 = boto3.client("ec2", region_name=region)

        # Create security group if not specified
        if not security_group_id:
            with Action("Setting up security group", ignore_fail=False) as action:
                try:
                    security_group_id, message = create_security_group(
                        ec2, repo, vpc_id
                    )
                    action.success(message)
                    result.add_success("Security group created")
                except Exception as e:
                    action.error(f"Error creating security group: {e}")
                    result.add_failure(f"Security group creation failed: {e}")
                    raise AWSAPIError(
                        f"Cannot continue without proper security group: {e}"
                    )

        # Process each runner configuration
        for runner_config in runner_configs:
            instance_type = runner_config["instance_type"]
            count = runner_config["count"]
            labels = runner_config["labels"]
            disk_size = runner_config.get("disk_size", default_disk_size)

            with Action(f"Processing runner config: {instance_type}") as action:
                action.note(f"Instance type: {instance_type}")
                action.note(f"AMI: {runner_config['ami_id']}")
                action.note(f"Target count: {count}")
                action.note(f"Disk size: {disk_size} GB")
                action.note(f"Labels: {', '.join(labels)}")

                # Check existing instances
                try:
                    existing_instances = get_existing_instances(ec2, repo, labels)
                    existing_count = len(existing_instances)
                    action.note(f"Found {existing_count} existing instance(s)")
                except Exception as e:
                    action.warning(f"Failed to check existing instances: {e}")
                    existing_count = 0

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

                # Create instances
                timestamp = int(time.time())
                config_success_count = 0

                for i in range(instances_to_create):
                    with Action(
                        f"Creating instance {i+1}/{instances_to_create}"
                    ) as instance_action:
                        try:
                            instance_id, instance_name = create_runner_instance(
                                ec2,
                                repo,
                                github_token,
                                runner_config,
                                user_data_template,
                                subnet_id,
                                security_group_id,
                                timestamp,
                                i,
                                args,
                                global_setup_steps,
                            )
                            instance_action.success(f"Instance name: {instance_name}")
                            instance_action.success(
                                f"Successfully launched: {instance_id}"
                            )
                            result.add_success(
                                f"Instance {instance_name} ({instance_id}) created"
                            )
                            config_success_count += 1
                        except Exception as e:
                            instance_action.error(f"Failed to create instance: {e}")
                            result.add_failure(f"Instance creation failed: {e}")

                # Summary for this config
                if config_success_count == instances_to_create:
                    action.success(
                        f"All {instances_to_create} instances created successfully"
                    )
                elif config_success_count > 0:
                    action.warning(
                        f"Partial success: {config_success_count}/{instances_to_create} instances created"
                    )
                else:
                    action.error(f"Failed to create any instances for {instance_type}")

        # Final summary
        with Action("Deployment Summary") as action:
            action.note(result.summary())
            if result.warnings:
                action.note("Warnings:")
                for warning in result.warnings:
                    action.note(f"  {warning}")
            if result.errors:
                action.note("Errors:")
                for error in result.errors:
                    action.note(f"  {error}")

        # Return appropriate exit code
        if not result.is_success():
            sys.exit(1)
        else:
            return

    except (ConfigurationError, GitHubAPIError, AWSAPIError) as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        sys.exit(1)


def find_instances_to_terminate(ec2, repo, labels):
    """Find instances that should be terminated based on repo and labels."""
    if not labels:
        # Get all instances for the repo
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
        return instances
    else:
        # Get instances with specific labels
        return get_existing_instances(ec2, repo, labels)


def get_runner_mapping(repo, github_token):
    """Get mapping of runner names to runner IDs from GitHub."""
    github_runners = get_github_runners(repo, github_token)

    # Create a mapping of runner names to runner IDs
    runner_map = {}
    for runner in github_runners:
        runner_name = runner.get("name", "")
        if RUNNER_NAME_PREFIX in runner_name:
            runner_map[runner_name] = runner["id"]

    return runner_map


def terminate_single_instance(ec2, instance, runner_map, repo, github_token):
    """Terminate a single instance and deregister its runner."""
    instance_id = instance["InstanceId"]
    instance_name = get_instance_name_from_tags(instance)

    deregistered = False

    # Try to deregister the runner from GitHub first
    if instance_name in runner_map:
        runner_id = runner_map[instance_name]
        if remove_github_runner(repo, github_token, runner_id):
            deregistered = True

    # Terminate the EC2 instance
    try:
        ec2.terminate_instances(InstanceIds=[instance_id])
        return True, deregistered
    except Exception as e:
        return False, deregistered


def undeploy_runners(args):
    """Undeploy GitHub self-hosted runners from EC2."""
    result = OperationResult()

    try:
        with Action("Getting GitHub token", ignore_fail=False) as action:
            github_token = get_github_token()
            action.success("GitHub token retrieved successfully")
            result.add_success("GitHub token retrieved")

        with Action("Loading configuration", ignore_fail=False) as action:
            config = load_config(args.config)
            repo = args.repo or config["repo"]
            region = config["region"]
            labels = args.labels or []
            action.note(f"Repository: {repo}")
            action.note(f"Region: {region}")
            if labels:
                action.note(f"Label filter: {labels}")
            else:
                action.note("No label filter - will find all instances")
            result.add_success("Configuration loaded")

        ec2 = boto3.client("ec2", region_name=region)

        # Find instances to terminate
        with Action("Finding instances to terminate") as action:
            try:
                instances = find_instances_to_terminate(ec2, repo, labels)
                action.note(f"Found {len(instances)} instances")
                result.add_success(f"Found {len(instances)} instances to terminate")
            except Exception as e:
                action.error(f"Failed to find instances: {e}")
                result.add_failure(f"Failed to find instances: {e}")
                raise AWSAPIError(f"Failed to find instances: {e}")

        if not instances:
            with Action("No instances found") as action:
                action.note(f"Repository: {repo}")
                if labels:
                    action.note(f"Labels: {labels}")
                action.success("No instances to terminate")
            return

        with Action("Preparing to terminate instances") as action:
            action.note(f"Found {len(instances)} instance(s) to terminate:")
            for instance in instances:
                instance_name = get_instance_name_from_tags(instance)
                action.note(f"  - {instance['InstanceId']} ({instance_name})")

            if not args.force:
                response = input("\nDo you want to terminate these instances? (y/N): ")
                if response.lower() != "y":
                    action.note("Operation cancelled by user")
                    result.add_success("Operation cancelled by user")
                    return

        # Get GitHub runners to match with instances
        with Action("Fetching GitHub runners") as action:
            try:
                runner_map = get_runner_mapping(repo, github_token)
                action.note(f"Found {len(runner_map)} GitHub runners to deregister")
                result.add_success(f"Found {len(runner_map)} GitHub runners")
            except Exception as e:
                action.warning(f"Failed to fetch GitHub runners: {e}")
                result.add_warning(f"Failed to fetch GitHub runners: {e}")
                runner_map = {}

        # Enrich instances with runner_id
        for instance in instances:
            instance_name = get_instance_name_from_tags(instance)
            instance["runner_id"] = runner_map.get(instance_name)
            instance["instance_name"] = instance_name

        counter = {"terminated": 0, "deregistered": 0}
        total = len(instances)
        polling_interval = 30  # seconds
        timeout_minutes = getattr(args, "wait_timeout", 30)
        timeout_seconds = timeout_minutes * 60
        start_time = time.time()

        def deregister_and_terminate(instance, force_note=None):
            instance_id = instance["InstanceId"]
            instance_name = instance["instance_name"]
            runner_id = instance["runner_id"]
            with Action(f"Terminating instance: {instance_id}") as action:
                if force_note:
                    action.note(force_note)
                action.note(f"Instance name: {instance_name}")
                if runner_id:
                    action.note(f"Deregistering runner from GitHub (ID: {runner_id})")
                    try:
                        if remove_github_runner(repo, github_token, runner_id):
                            action.success("Runner deregistered successfully")
                            counter["deregistered"] += 1
                            result.add_success(f"Runner {instance_name} deregistered")
                        else:
                            action.warning(
                                "Failed to deregister runner, but continuing"
                            )
                            result.add_warning(
                                f"Failed to deregister runner {instance_name}"
                            )
                    except Exception as e:
                        action.warning(f"Failed to deregister runner: {e}")
                        result.add_warning(
                            f"Failed to deregister runner {instance_name}: {e}"
                        )
                else:
                    action.note("No matching GitHub runner found")
                try:
                    ec2.terminate_instances(InstanceIds=[instance_id])
                    action.success("Instance terminated successfully")
                    counter["terminated"] += 1
                    result.add_success(
                        f"Instance {instance_name} ({instance_id}) terminated"
                    )
                except Exception as e:
                    action.error(f"Failed to terminate instance: {e}")
                    result.add_failure(
                        f"Failed to terminate instance {instance_name}: {e}"
                    )

        # Main rolling termination loop
        remaining = instances.copy()
        while remaining:
            now = time.time()
            if args.wait and not args.force and (now - start_time) < timeout_seconds:
                # Poll status for all remaining runners
                with Action(
                    f"Polling runner status ({len(remaining)} remaining)"
                ) as action:
                    try:
                        github_runners = get_github_runners(repo, github_token)
                        runner_status_map = {r["id"]: r for r in github_runners}
                    except Exception as e:
                        action.warning(f"Failed to fetch runner status: {e}")
                        runner_status_map = {}
                    next_remaining = []
                    for instance in remaining:
                        runner_id = instance["runner_id"]
                        instance_name = instance["instance_name"]
                        busy = False
                        if runner_id and runner_id in runner_status_map:
                            busy = runner_status_map[runner_id].get("busy", False)
                        if not busy:
                            deregister_and_terminate(instance)
                        else:
                            next_remaining.append(instance)
                            action.note(f"Runner {instance_name} is still busy")
                    remaining = next_remaining
                    action.note(
                        f"Progress: {counter['terminated']}/{total} terminated, {len(remaining)} still busy"
                    )
                if remaining:
                    time.sleep(polling_interval)
            else:
                # Either not waiting, forced, or timeout reached: terminate all remaining
                force_note = None
                if args.wait and not args.force and remaining:
                    force_note = "Timeout reached or forced, terminating regardless of busy status."
                for instance in remaining:
                    deregister_and_terminate(instance, force_note=force_note)
                break

        with Action("Summary") as action:
            action.note(f"Instances terminated: {counter['terminated']}/{total}")
            action.note(f"Runners deregistered: {counter['deregistered']}/{total}")
            action.note(result.summary())
            if counter["terminated"] > 0:
                action.note(
                    "Note: It may take a few minutes for runners to disappear from GitHub."
                )
            if not result.is_success():
                sys.exit(1)
            return

    except (ConfigurationError, GitHubAPIError, AWSAPIError) as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        sys.exit(1)


def display_github_runners(github_runners):
    """Display GitHub runners information."""
    # Filter for EC2 runners
    ec2_runners = []
    for runner in github_runners:
        if RUNNER_NAME_PREFIX in runner.get("name", ""):
            ec2_runners.append(runner)

    print(f"EC2 runners: {len(ec2_runners)}")

    for runner in ec2_runners:
        status = runner.get("status", "unknown")
        status_icon = (
            "🟢" if status == "online" else "🔴" if status == "offline" else "🟡"
        )

        # Extract labels
        label_names = [label.get("name", "") for label in runner.get("labels", [])]

        print(f"{status_icon} {runner['name']}")
        print(
            f"   Status: {status}"
            + (" ⚡ Currently busy" if runner.get("busy") else "")
        )
        print(f"   Labels: {', '.join(label_names)}")


def display_ec2_instances(instances):
    """Display EC2 instances information."""
    print(f"EC2 instances: {len(instances)}")
    for instance in instances:
        state = instance["State"]["Name"]
        state_icon = (
            "🟢" if state == "running" else "🟡" if state == "pending" else "🔴"
        )

        # Get tags
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        name = tags.get("Name", "Unknown")

        print(f"{state_icon} {name} ({instance['InstanceId']})")
        print(f"   State: {state}")
        print(f"   Type: {instance['InstanceType']}")
        if instance.get("PublicIpAddress"):
            print(f"   IP: {instance['PublicIpAddress']}")


def list_runners(args):
    """List GitHub runners and EC2 instances."""
    try:
        with Action("Getting GitHub token") as action:
            github_token = get_github_token()
            action.note("GitHub token retrieved successfully")

        with Action("Loading configuration") as action:
            config = load_config(args.config)
            repo = args.repo or config["repo"]
            region = config["region"]
            action.note(f"Repository: {repo}")
            action.note(f"Region: {region}")

        with Action("Fetching GitHub runners") as action:
            # Get GitHub runners
            github_runners = get_github_runners(repo, github_token)
            action.note(f"Total runners: {len(github_runners)}")
            display_github_runners(github_runners)

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

            display_ec2_instances(instances)

    except Exception as e:
        print(f"Error: {e}")


def load_config(config_path):
    """Load and validate configuration from YAML file with environment variable support."""
    with open(config_path, "r") as f:
        content = f.read()

    # Replace environment variables in the content
    import re

    def replace_env_var(match):
        var_name = match.group(1)
        default_value = match.group(2) if match.group(2) else None
        env_value = os.getenv(var_name)
        if env_value is not None:
            return env_value
        elif default_value is not None:
            return default_value
        else:
            raise ValueError(f"Environment variable {var_name} is required but not set")

    # Replace ${VAR_NAME} or ${VAR_NAME:default_value} patterns
    content = re.sub(r"\$\{([^:}]+)(?::([^}]*))?\}", replace_env_var, content)

    config = yaml.safe_load(content)

    # Validate required fields
    required_fields = ["repo", "region", "runners"]
    for field in required_fields:
        if field not in config:
            raise ValueError(f"Missing required field '{field}' in config file")

    # Add automatic labels to runner configs
    for runner_config in config["runners"]:
        runner_config["labels"].extend(
            [
                f"type-ec2-{runner_config['instance_type']}",
                runner_config["ami_id"],
            ]
        )

    return config


def get_instance_name_from_tags(instance):
    """Extract instance name from tags."""
    for tag in instance.get("Tags", []):
        if tag["Key"] == "Name":
            return tag["Value"]
    return None


def validate_networking_config(config):
    """Validate networking configuration."""
    vpc_id = config.get("vpc_id")
    subnet_id = config.get("subnet_id")

    if not vpc_id:
        raise ValueError("vpc_id not specified in config file")
    if not subnet_id:
        raise ValueError("subnet_id not specified in config file")

    return vpc_id, subnet_id


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
    undeploy_parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for runners to become idle before terminating.",
    )
    undeploy_parser.add_argument(
        "--wait-timeout",
        type=int,
        default=30,
        help="Timeout in minutes for waiting for runners to become idle.",
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
        sys.exit(0)

    try:
        if args.command == "deploy":
            deploy_runners(args)
        elif args.command == "undeploy":
            undeploy_runners(args)
        elif args.command == "list":
            list_runners(args)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
