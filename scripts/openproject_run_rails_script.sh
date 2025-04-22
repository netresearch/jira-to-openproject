#!/bin/bash

# OpenProject Rails Console Socket Runner
# This script uses pry-remote to run Ruby code in the OpenProject Rails environment via a socket connection

# Load configuration from .env file if it exists
if [ -f .env ]; then
  echo "Loading configuration from .env file"
  source .env
fi

# Load configuration from .env.local for overrides if it exists
if [ -f .env.local ]; then
  echo "Loading override configuration from .env.local file"
  source .env.local
fi

# Default configuration - will be overridden by .env or .env.local if provided
: ${J2O_OPENPROJECT_SERVER:="localhost"}
: ${J2O_OPENPROJECT_CONTAINER:="openproject-web-1"}
: ${J2O_OPENPROJECT_RAILS_PATH:="/app"}
: ${J2O_OPENPROJECT_PRY_PORT:="9876"}

# Print command function for debugging
print_cmd() {
  echo "EXECUTING: $@"
  "$@"
  return $?
}

# Check if script file is provided as parameter
if [ "$1" ]; then
  SCRIPT_FILE="$1"
else
  # If no parameter, try to find the latest ruby script
  SCRIPT_FILE=$(ls -t output/*.rb 2>/dev/null | head -1)
fi

# Check if script file exists
if [ ! -f "$SCRIPT_FILE" ]; then
  echo "Error: Script file not found: $SCRIPT_FILE"
  echo "Usage: $0 [path/to/script.rb]"
  echo "If no script is provided, the most recent Ruby script in the output directory will be used."
  echo ""
  echo "You can also set the following environment variables in .env or .env.local:"
  echo "J2O_OPENPROJECT_SERVER      - Hostname of the OpenProject server (default: localhost)"
  echo "J2O_OPENPROJECT_CONTAINER   - Name of the Docker container (default: openproject-web-1)"
  echo "J2O_OPENPROJECT_RAILS_PATH  - Path to Rails application in container (default: /app)"
  echo "J2O_OPENPROJECT_PRY_PORT    - Port for the pry-remote connection (default: 9876)"
  echo ""
  echo "Note: Values in .env.local will override those in .env"
  exit 1
fi

echo "Using script: $SCRIPT_FILE"

# Check if pry-remote is installed, if not, install it
echo "Ensuring pry-remote is installed in the container..."
CHECK_PRY_CMD="docker exec $J2O_OPENPROJECT_CONTAINER bash -c \"cd $J2O_OPENPROJECT_RAILS_PATH && bundle list | grep pry-remote || echo 'not-installed'\""
PRY_CHECK=$(ssh "$J2O_OPENPROJECT_SERVER" "$CHECK_PRY_CMD")

if [[ "$PRY_CHECK" == *"not-installed"* ]]; then
  echo "Installing pry-remote in the container..."
  INSTALL_PRY_CMD="docker exec $J2O_OPENPROJECT_CONTAINER bash -c \"cd $J2O_OPENPROJECT_RAILS_PATH && bundle add pry-remote --group=development\""
  ssh "$J2O_OPENPROJECT_SERVER" "$INSTALL_PRY_CMD"

  if [ $? -ne 0 ]; then
    echo "Error: Failed to install pry-remote. Please install it manually by adding 'pry-remote' to the Gemfile."
    exit 1
  fi
fi

# Start the pry-remote server in a background process
echo "Starting pry-remote server in the container..."
START_PRY_CMD="docker exec -d $J2O_OPENPROJECT_CONTAINER bash -c \"cd $J2O_OPENPROJECT_RAILS_PATH && bundle exec rails runner 'require \"pry-remote\"; binding.remote_pry(port: $J2O_OPENPROJECT_PRY_PORT)'\""
ssh "$J2O_OPENPROJECT_SERVER" "$START_PRY_CMD"

# Give the server a moment to start up
echo "Waiting for pry-remote server to start..."
sleep 3

# Set up SSH port forwarding
echo "Setting up SSH port forwarding from localhost:$J2O_OPENPROJECT_PRY_PORT to container..."
SSH_FORWARD_CMD="ssh -f -N -L $J2O_OPENPROJECT_PRY_PORT:localhost:$J2O_OPENPROJECT_PRY_PORT $J2O_OPENPROJECT_SERVER"
$SSH_FORWARD_CMD

if [ $? -ne 0 ]; then
  echo "Error: Failed to set up SSH port forwarding."
  exit 1
fi

# Create a temporary file with the script content
TMP_SCRIPT="/tmp/pry_commands_$$.rb"
echo "Preparing script commands..."
cat > "$TMP_SCRIPT" << EOL
# Loading script content
begin
  puts "Executing script: $SCRIPT_FILE"
  $(cat "$SCRIPT_FILE")
  puts "\nScript execution completed successfully!"
rescue => e
  puts "Error during script execution: #{e.message}"
  puts e.backtrace
ensure
  # Exit pry-remote session
  exit
end
EOL

# Run the script using the pry-remote client
echo "Connecting to pry-remote and executing script..."
cat "$TMP_SCRIPT" | nc localhost $J2O_OPENPROJECT_PRY_PORT

# Clean up temporary script
rm "$TMP_SCRIPT"

# Kill SSH port forwarding
echo "Cleaning up SSH port forwarding..."
ps aux | grep "ssh -f -N -L $J2O_OPENPROJECT_PRY_PORT" | grep -v grep | awk '{print $2}' | xargs -r kill

echo "Script execution completed. Connection closed."
