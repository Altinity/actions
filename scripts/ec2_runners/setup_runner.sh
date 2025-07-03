#!/bin/bash
set -e

# User-provided parameters
GITHUB_REPO_URL="${github_repo_url}"
RUNNER_LABELS="${runner_labels}"
RUNNER_TOKEN="${runner_token}"
RUNNER_NAME="${runner_name}"

echo "=== GitHub Runner Setup Starting ==="
echo "Repo URL: ${GITHUB_REPO_URL}"
echo "Labels: ${RUNNER_LABELS}"
echo "Timestamp: $(date)"

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Detect OS and package manager
log "Detecting operating system..."
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_NAME="$NAME"
    OS_VERSION="$VERSION_ID"
    log "OS: $OS_NAME $OS_VERSION"
else
    log "Could not detect OS from /etc/os-release"
    OS_NAME="Unknown"
fi

# Determine the correct user based on OS
if [[ "$OS_NAME" == *"Ubuntu"* ]]; then
    RUNNER_USER="ubuntu"
    log "Detected Ubuntu - using 'ubuntu' user"
else
    RUNNER_USER="ec2-user"
    log "Detected Amazon Linux/CentOS - using 'ec2-user'"
fi

# Determine package manager
if command_exists apt-get; then
    PKG_MANAGER="apt"
    log "Using apt package manager (Ubuntu/Debian)"
elif command_exists dnf; then
    PKG_MANAGER="dnf"
    log "Using dnf package manager (Amazon Linux 2023/Fedora)"
elif command_exists yum; then
    PKG_MANAGER="yum"
    log "Using yum package manager (Amazon Linux 2/CentOS)"
else
    log "ERROR: No supported package manager found"
    exit 1
fi

# Update system packages
log "Updating system packages..."
case $PKG_MANAGER in
    apt)
        apt-get update -y
        apt-get upgrade -y
        ;;
    dnf)
        dnf update -y
        dnf upgrade -y
        ;;
    yum)
        yum update -y
        yum upgrade -y
        ;;
esac

# Install required packages
log "Installing required packages..."
case $PKG_MANAGER in
    apt)
        apt-get install -y jq curl fail2ban

        # Install Docker from official repository for newer version
        log "Installing Docker from official repository..."
        apt-get install -y ca-certificates curl gnupg lsb-release

        # Add Docker's official GPG key
        mkdir -p /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg

        # Add Docker repository
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

        # Update package list and install Docker
        apt-get update
        apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin

        log "Installing github cli..."
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
        apt-get update
        apt-get install -y gh
        ;;
    dnf)
        dnf install -y docker jq curl fail2ban
        ;;
    yum)
        yum install -y docker jq curl fail2ban
        ;;
esac

# Verify packages installed
log "Verifying package installation..."
if ! command_exists curl; then
    log "ERROR: curl not installed"
    exit 1
fi
if ! command_exists jq; then
    log "ERROR: jq not installed"
    exit 1
fi

if ! command_exists python3; then
    log "ERROR: python3 not installed"
    exit 1
fi

# Verify pip is available
if ! command_exists pip3; then
    log "ERROR: pip3 not installed"
    exit 1
fi

# Check Python version
log "Python version: $(python3 --version 2>&1)"

# Check disk space
log "Checking available disk space..."
df -h /
log "Disk space summary:"
df -h | grep -E "(Filesystem|/$)" || true

log "Checking fail2ban..."
fail2ban-client status
systemctl status fail2ban || true

# Install and configure Docker
log "Installing and configuring Docker..."
case $PKG_MANAGER in
    apt)
        systemctl start docker
        systemctl enable docker
        ;;
    dnf|yum)
        systemctl start docker
        systemctl enable docker
        ;;
esac

# Verify Docker is running
log "Verifying Docker installation..."
if ! command_exists docker; then
    log "ERROR: Docker not installed"
    exit 1
fi

# Test Docker
if ! docker --version >/dev/null 2>&1; then
    log "ERROR: Docker not working properly"
    exit 1
fi

log "Docker installation verified"

# Add runner user to docker group so they can run docker without sudo
log "Adding $RUNNER_USER to docker group..."
usermod -a -G docker $RUNNER_USER

# Set up SSH keys for runner user
log "Setting up SSH keys for $RUNNER_USER..."
if [ ! -d /home/$RUNNER_USER/.ssh ]; then
    mkdir -p /home/$RUNNER_USER/.ssh
    chown $RUNNER_USER:$RUNNER_USER /home/$RUNNER_USER/.ssh
    chmod 700 /home/$RUNNER_USER/.ssh
fi

# Generate SSH key if it doesn't exist
if [ ! -f /home/$RUNNER_USER/.ssh/id_rsa ]; then
    log "Generating SSH key for $RUNNER_USER..."
    su - $RUNNER_USER -c "ssh-keygen -t rsa -b 4096 -f /home/$RUNNER_USER/.ssh/id_rsa -N ''"
    chown $RUNNER_USER:$RUNNER_USER /home/$RUNNER_USER/.ssh/id_rsa*
fi

# Add SSH key to authorized_keys
if [ ! -f /home/$RUNNER_USER/.ssh/authorized_keys ]; then
    touch /home/$RUNNER_USER/.ssh/authorized_keys
    chown $RUNNER_USER:$RUNNER_USER /home/$RUNNER_USER/.ssh/authorized_keys
    chmod 600 /home/$RUNNER_USER/.ssh/authorized_keys
fi

# Add the public key to authorized_keys if not already there
PUBLIC_KEY=$(cat /home/$RUNNER_USER/.ssh/id_rsa.pub)
if ! grep -q "$PUBLIC_KEY" /home/$RUNNER_USER/.ssh/authorized_keys; then
    echo "$PUBLIC_KEY" >> /home/$RUNNER_USER/.ssh/authorized_keys
fi

log "SSH keys configured for $RUNNER_USER"

# Detect architecture
log "Detecting architecture..."
ARCH=$(uname -m)
case $ARCH in
    x86_64)
        RUNNER_ARCH="x64"
        ;;
    aarch64)
        RUNNER_ARCH="arm64"
        ;;
    *)
        log "ERROR: Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

log "Architecture: $ARCH -> $RUNNER_ARCH"

# Custom setup steps will be inserted here
${custom_setup_steps}

# Set up post-job cleanup script
log "Setting up post-job cleanup script..."

# Create cleanup script that runs after each job
cat > /usr/local/bin/runner-cleanup.sh << 'EOF'
#!/bin/bash
set -e

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

log "Starting post-job cleanup..."

# Clean up Docker resources (this can be done as runner user)
if command -v docker >/dev/null 2>&1; then
    log "Cleaning up Docker resources..."
    # Remove unused containers, networks, images
    docker system prune -f 2>/dev/null || true
    # Remove build cache
    docker builder prune -f 2>/dev/null || true
    # Note: We don't prune volumes here as they might be in use
fi

# Clean up GitHub Actions workspace
# The script runs in the security context of the runner service
if [ -d "/home/ubuntu/actions-runner/_work" ]; then
    log "Cleaning up GitHub Actions workspace..."

    # Find and remove _temp directories (these are safe to remove)
    find /home/ubuntu/actions-runner/_work -type d -name "_temp" -exec rm -rf {} + 2>/dev/null || true

    # Find and remove _instances directories
    # Use sudo for these as they have root ownership from Docker bind mounts
    find /home/ubuntu/actions-runner/_work -type d -name "_instances" -exec sudo rm -rf {} + 2>/dev/null || true

    # Remove log files
    find /home/ubuntu/actions-runner/_work -type f -name "*.log" -delete 2>/dev/null || true

    # Remove any empty directories (but be careful not to remove the main structure)
    find /home/ubuntu/actions-runner/_work -type d -empty -not -path "/home/ubuntu/actions-runner/_work" -delete 2>/dev/null || true

    # Fix ownership of remaining files to ubuntu user
    sudo chown -R ubuntu:ubuntu /home/ubuntu/actions-runner/_work 2>/dev/null || true
fi

# Clean up temporary files in user directories
log "Cleaning up temporary files..."
rm -rf /tmp/* 2>/dev/null || true
rm -rf /var/tmp/* 2>/dev/null || true

# Clean up zram devices to avoid "Device or resource busy" errors
log "Cleaning up zram devices..."
if command -v zramctl >/dev/null 2>&1; then
    # List all zram devices and reset them
    zramctl list | awk 'NR>1 {print $1}' | while read device; do
        if [ -n "$device" ]; then
            log "Resetting zram device: $device"
            zramctl reset "$device" 2>/dev/null || true
        fi
    done
fi

log "Post-job cleanup completed"
EOF

# Make cleanup script executable
chmod +x /usr/local/bin/runner-cleanup.sh

# Configure sudo access for the cleanup script
echo "$RUNNER_USER ALL=(ALL) NOPASSWD: /usr/local/bin/runner-cleanup.sh" > /etc/sudoers.d/runner-cleanup

log "Post-job cleanup script configured"

# Get latest runner version
log "Getting latest runner version..."
RUNNER_VERSION=$(curl -s -X GET 'https://api.github.com/repos/actions/runner/releases/latest' | jq -r '.tag_name' | sed 's/v//')
if [ -z "$RUNNER_VERSION" ] || [ "$RUNNER_VERSION" = "null" ]; then
    log "ERROR: Could not get runner version"
    exit 1
fi

log "Runner version: $RUNNER_VERSION"

# Download and install runner
log "Downloading GitHub Actions runner..."
RUNNER_TARBALL_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"

# Create runner directory and set ownership
mkdir -p /home/$RUNNER_USER/actions-runner
chown $RUNNER_USER:$RUNNER_USER /home/$RUNNER_USER/actions-runner

# Debug: Check directory exists and permissions
log "Checking runner directory..."
ls -la /home/$RUNNER_USER/actions-runner || log "Directory does not exist"

# Download runner as runner user
log "Downloading runner tarball..."
if ! su - $RUNNER_USER -c "cd /home/$RUNNER_USER/actions-runner && curl -L -o actions-runner.tar.gz '$RUNNER_TARBALL_URL'"; then
    log "ERROR: Failed to download runner"
    exit 1
fi

# Debug: Check if file was downloaded
log "Checking downloaded file..."
su - $RUNNER_USER -c "ls -la /home/$RUNNER_USER/actions-runner/actions-runner.tar.gz" || log "File not found"

# Extract runner as runner user
log "Extracting runner..."
if ! su - $RUNNER_USER -c "cd /home/$RUNNER_USER/actions-runner && tar xzf actions-runner.tar.gz"; then
    log "ERROR: Failed to extract runner"
    exit 1
fi

# Debug: Check extracted files
log "Checking extracted files..."
su - $RUNNER_USER -c "ls -la /home/$RUNNER_USER/actions-runner/" || log "Cannot list directory"

# Configure runner as runner user
log "Configuring runner..."

if ! su - $RUNNER_USER -c "cd /home/$RUNNER_USER/actions-runner && ./config.sh --url '$GITHUB_REPO_URL' --token '$RUNNER_TOKEN' --labels '$RUNNER_LABELS' --name '$RUNNER_NAME' --unattended --replace"; then
    log "ERROR: Failed to configure runner"
    exit 1
fi

# Configure runner to run cleanup after each job using official hooks
log "Configuring post-job cleanup using GitHub Actions hooks..."

# Create .env file in the runner directory to set the hook
cat > /home/$RUNNER_USER/actions-runner/.env << EOF
# GitHub Actions runner hooks
ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/usr/local/bin/runner-cleanup.sh
EOF

chown $RUNNER_USER:$RUNNER_USER /home/$RUNNER_USER/actions-runner/.env
chmod 600 /home/$RUNNER_USER/actions-runner/.env

log "Post-job cleanup configured using ACTIONS_RUNNER_HOOK_JOB_COMPLETED"

# Install runner service with sudo (required)
log "Installing runner service..."
if ! su - $RUNNER_USER -c "cd /home/$RUNNER_USER/actions-runner && sudo ./svc.sh install"; then
    log "ERROR: Failed to install runner service"
    exit 1
fi

# Start runner service with sudo
log "Starting runner service..."
if ! su - $RUNNER_USER -c "cd /home/$RUNNER_USER/actions-runner && sudo ./svc.sh start"; then
    log "ERROR: Failed to start runner service"
    exit 1
fi

# Verify service is running
log "Verifying runner service..."
if ! systemctl is-active --quiet actions.runner.*; then
    log "ERROR: Runner service is not running"
    systemctl status actions.runner.* || true
    exit 1
fi

# Debug: Check runner status and logs
log "Checking runner status..."
systemctl status actions.runner.* || true

log "Checking recent runner logs..."
journalctl -u actions.runner.* --no-pager -n 20 || true

# Check if runner is connected to GitHub
log "Checking runner connection to GitHub..."
sleep 10  # Give runner time to connect
if systemctl is-active --quiet actions.runner.*; then
    log "Runner service is active"
    # Check if runner is actually connected
    if su - $RUNNER_USER -c "cd /home/$RUNNER_USER/actions-runner && ./run.sh --version"; then
        log "Runner binary is working"
    else
        log "WARNING: Runner binary may have issues"
    fi
else
    log "ERROR: Runner service is not active"
fi

log "=== GitHub Runner Setup Completed Successfully ==="
log "Runner service is running"
log "Check status with: systemctl status actions.runner.*"
log "Check logs with: journalctl -u actions.runner.* -f"
