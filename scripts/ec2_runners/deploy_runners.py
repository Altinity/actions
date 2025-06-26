#! /usr/bin/env python3
import argparse
import os
import yaml
import boto3
import requests


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


def get_existing_instances(ec2, repo, labels):
    """Get existing instances that match the repo and labels."""
    filters = [
        {"Name": "tag:GitHubRepo", "Values": [repo]},
        {
            "Name": "instance-state-name",
            "Values": ["running", "pending", "stopping", "stopped"],
        },
    ]

    if labels:
        # Create a filter that matches any of the labels
        label_values = [",".join(labels)]
        filters.append({"Name": "tag:GitHubLabels", "Values": label_values})

    response = ec2.describe_instances(Filters=filters)

    instances = []
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            instances.append(instance)

    return instances


def main():
    parser = argparse.ArgumentParser(
        description="Deploy GitHub self-hosted runners on EC2."
    )
    parser.add_argument(
        "--config",
        default="runner_config.yaml",
        help="Path to the runner configuration file.",
    )
    parser.add_argument(
        "--user-data",
        default="setup_runner.sh",
        help="Path to the runner setup script.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force creation of new instances even if target count is already met.",
    )
    args = parser.parse_args()

    try:
        github_token = get_github_token()

        with open(args.config, "r") as f:
            config = yaml.safe_load(f)

        repo = config["repo"]
        region = config["region"]
        runner_configs = config["runners"]

        # Get networking configuration from config file
        vpc_id = config.get("vpc_id")
        subnet_id = config.get("subnet_id")
        security_group_id = config.get("security_group_id")

        if not vpc_id:
            raise ValueError("vpc_id not specified in config file")
        if not subnet_id:
            raise ValueError("subnet_id not specified in config file")

        with open(args.user_data, "r") as f:
            user_data_template = f.read()

        ec2 = boto3.client("ec2", region_name=region)

        # Create security group if not specified
        if not security_group_id:
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
                    print(f"Using existing security group: {security_group_id}")
                else:
                    # Create new security group
                    response = ec2.create_security_group(
                        GroupName=sg_name,
                        Description=f"Security group for GitHub runners in {repo}",
                        VpcId=vpc_id,
                    )
                    security_group_id = response["GroupId"]

                    # Add basic rules
                    ec2.authorize_security_group_ingress(
                        GroupId=security_group_id,
                        IpPermissions=[
                            {
                                "IpProtocol": "tcp",
                                "FromPort": 22,
                                "ToPort": 22,
                                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                            }
                        ],
                    )
                    print(f"Created security group: {security_group_id}")
            except Exception as e:
                print(f"Warning: Could not create security group: {e}")
                print("Continuing without security group...")
                security_group_id = None

        for runner_config in runner_configs:
            instance_type = runner_config["instance_type"]
            ami_id = runner_config["ami_id"]
            count = runner_config["count"]
            labels = runner_config["labels"]

            # Check existing instances
            existing_instances = get_existing_instances(ec2, repo, labels)
            existing_count = len(existing_instances)

            print(
                f"Found {existing_count} existing instance(s) for labels: {','.join(labels)}"
            )

            if existing_count >= count and not args.force:
                print(
                    f"Target count ({count}) already met. Skipping creation. Use --force to override."
                )
                continue

            instances_to_create = count - existing_count if not args.force else count

            if instances_to_create <= 0:
                print(f"No new instances needed for labels: {','.join(labels)}")
                continue

            print(f"Requesting registration token for {repo}...")
            reg_token = get_runner_registration_token(repo, github_token)

            user_data = user_data_template.replace(
                "${github_repo_url}", f"https://github.com/{repo}"
            )
            user_data = user_data.replace("${runner_labels}", ",".join(labels))
            user_data = user_data.replace("${runner_token}", reg_token)

            print(
                f"Launching {instances_to_create} instance(s) of type {instance_type} with labels: {','.join(labels)}"
            )

            instance_name = f"github-runner-{repo.replace('/', '-')}-{labels[0]}"

            # Build run_instances parameters
            run_params = {
                "ImageId": ami_id,
                "InstanceType": instance_type,
                "MinCount": instances_to_create,
                "MaxCount": instances_to_create,
                "UserData": user_data,
                "SubnetId": subnet_id,
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": instance_name},
                            {"Key": "GitHubRepo", "Value": repo},
                            {"Key": "GitHubLabels", "Value": ",".join(labels)},
                        ],
                    },
                ],
            }

            # Add security group if available
            if security_group_id:
                run_params["SecurityGroupIds"] = [security_group_id]

            instances = ec2.run_instances(**run_params)

            print(f"Successfully launched {len(instances['Instances'])} instance(s).")
            for i in instances["Instances"]:
                print(f"  - Instance ID: {i['InstanceId']}")

    except (ValueError, FileNotFoundError, requests.exceptions.RequestException) as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    main()
