#!/bin/bash
set -e

# User-provided parameters
GITHUB_REPO_URL="${github_repo_url}"
RUNNER_LABELS="${runner_labels}"
RUNNER_TOKEN="${runner_token}"

# Create a dedicated user
sudo useradd -m -s /bin/bash github
sudo usermod -aG sudo github

# Switch to the new user
sudo su - github <<'EOF'
set -e

# Install dependencies
sudo yum update -y
sudo yum install -y curl jq

# Install Docker
sudo yum install -y docker
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker github

# Download and install GitHub Actions Runner
ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
    RUNNER_ARCH="x64"
elif [ "$ARCH" = "aarch64" ]; then
    RUNNER_ARCH="arm64"
else
    echo "Unsupported architecture: $ARCH"
    exit 1
fi

RUNNER_VERSION=$(curl -s -X GET 'https://api.github.com/repos/actions/runner/releases/latest' | jq -r '.tag_name' | sed 's/v//')
RUNNER_TARBALL_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"

mkdir actions-runner && cd actions-runner
curl -o actions-runner.tar.gz -L "${RUNNER_TARBALL_URL}"
tar xzf ./actions-runner.tar.gz

# Configure the runner
./config.sh --url "${GITHUB_REPO_URL}" --token "${RUNNER_TOKEN}" --labels "${RUNNER_LABELS}" --unattended --replace

# Install and run the service
sudo ./svc.sh install
sudo ./svc.sh start
EOF 