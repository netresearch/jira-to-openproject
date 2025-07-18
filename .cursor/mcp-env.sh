#!/bin/bash

# Source environment variables from .env.local if it exists
if [ -f "$(dirname "$0")/../.env.local" ]; then
    set -a  # automatically export all variables
    source "$(dirname "$0")/../.env.local"
    set +a  # stop automatically exporting
fi

# Execute the command passed as arguments
exec "$@" 