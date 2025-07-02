# GitHub EC2 Runners

A Python script for deploying and managing GitHub self-hosted runners on AWS EC2 instances. This tool automates the creation, configuration, and cleanup of EC2 instances that serve as GitHub Actions runners.

## Prerequisites

- Python 3.8+
- AWS CLI configured with appropriate permissions
- GitHub Personal Access Token with `repo` scope
- AWS resources: VPC, Subnet, and optionally a Security Group

### Required AWS Permissions

Your AWS credentials need permissions for:

- EC2: Create, describe, terminate instances
- Security Groups: Create, describe, modify
- IAM: Read instance profiles (if using IAM roles)

### Required GitHub Permissions

Your GitHub token needs:

- `repo` scope for repository-level runners
- `admin:org` scope for organization-level runners

## Installation

1. Clone the repository and navigate to the scripts directory:

```bash
cd scripts/ec2_runners
```

1. Install Python dependencies:

```bash
pip install -r requirements.txt
```

1. Set up your configuration (see Configuration section below)

## Configuration

### Environment Variables

To avoid exposing sensitive AWS resource IDs in your config file, use environment variables:

```bash
# Required AWS resources
export AWS_VPC_ID="vpc-xxxxxxxxx"
export AWS_SUBNET_ID="subnet-xxxxxxxxx"

# Optional: specify existing security group
export AWS_SECURITY_GROUP_ID="sg-xxxxxxxxx"

# Required GitHub token
export GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"
```

### Configuration Files

- `runner_config.yaml` - Your actual configuration (should be in .gitignore)
- `runner_config.example.yaml` - Template with environment variables (safe for version control)

### Configuration Format

```yaml
repo: "owner/repo"                    # GitHub repository or organization
region: "${AWS_REGION:us-east-1}"     # AWS region with default
vpc_id: "${AWS_VPC_ID}"              # VPC ID (required)
subnet_id: "${AWS_SUBNET_ID}"        # Subnet ID (required)
# security_group_id: "${AWS_SECURITY_GROUP_ID}"  # Optional: auto-created if not specified

# Default disk configuration (in GB)
default_disk_size: 40

runners:
  - instance_type: "t4g.xlarge"      # EC2 instance type
    ami_id: "ami-xxxxxxxxx"          # AMI ID
    count: 2                         # Number of runners to deploy
    disk_size: 100                   # Optional: override default disk size
    labels:                          # GitHub runner labels
      - self-hosted
      - Linux
      - ARM64
      - custom-label
```

The instance type and ami will automatically be added to the labels: `type-ec2-${instance_type}, ${ami_id}`

### Environment Variable Syntax

The config supports these patterns:

- `${VAR_NAME}` - Required environment variable
- `${VAR_NAME:default_value}` - Environment variable with default fallback

Examples:
```yaml
vpc_id: "${AWS_VPC_ID}"                    # Must be set
region: "${AWS_REGION:us-east-1}"          # Defaults to us-east-1 if not set
```

## Usage

### Basic Commands

```bash
# Deploy all configured runners
python3 ec2_runners.py deploy

# List all runners (GitHub + EC2 status)
python3 ec2_runners.py list

# Undeploy all runners for the repository
python3 ec2_runners.py undeploy

# Undeploy specific runners by labels
python3 ec2_runners.py undeploy --labels arm64 test
```
