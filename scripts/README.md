# Utility Scripts (`scripts/`)

This directory contains various utility scripts to support the migration process, development, and testing. All scripts are up to date with the current codebase.

## Script List

*   **`cleanup_var.py`**: Cleans specified subdirectories within the `var/` directory (e.g., `var/data`, `var/logs`) or the entire `var/` directory. Useful for starting a fresh migration run.
    *   Usage: `python scripts/cleanup_var.py [--clean <dir_name|all>]`
*   **`convert_rails_to_json.py`**: Converts Ruby hash/array syntax (from Rails console output) within a file into standard JSON format.
    *   Usage: `python scripts/convert_rails_to_json.py <input_ruby_file> <output_json_file>`
*   **`create_custom_fields_json.py`**: Parses a text format listing custom fields and converts it into a JSON file.
    *   Usage: `python scripts/create_custom_fields_json.py <input_text_file> <output_json_file>`
*   **`extract_json.py`**: Extracts JSON arrays embedded within a larger text file (e.g., log files).
    *   Usage: `python scripts/extract_json.py <input_log_file> <output_json_file>`
*   **`extract_tmux_custom_fields.py`**: Captures output from a `tmux` session (Rails console) and parses OpenProject custom field information.
    *   Usage: `python scripts/extract_tmux_custom_fields.py [output_json_file]`
*   **`get_complete_custom_fields.py`**: Interacts with a `tmux` session running a Rails console to fetch complete custom field details (including possible values) and parses the output.
    *   Usage: `python scripts/get_complete_custom_fields.py [output_json_file]`
*   **`get_custom_fields_simple.py`**: Fetches basic custom field details from a Rails console session.
    *   Usage: `python scripts/get_custom_fields_simple.py [output_json_file]`
*   **`get_custom_fields.rb`**: Ruby script to be run inside the OpenProject Rails console. Fetches all custom fields and prints them as JSON to `/tmp/openproject_custom_fields.json`.
*   **`get_openproject_fields.py`**: Runs `get_custom_fields.rb` inside the Rails console (via `tmux` or `docker exec`), retrieves the generated JSON file, and parses the output.
    *   Usage: `python scripts/get_openproject_fields.py [--output <output_json_file>]`
*   **`list_jira_data.py`**: Connects to Jira and lists metadata like projects, issue types, statuses, priorities, and custom fields, saving the output to JSON files in `var/data/jira_exports/`.
    *   Usage: `python scripts/list_jira_data.py [--list <projects|issue_types|statuses|priorities|custom_fields|all>]`
*   **`openproject_run_rails_script.sh`**: Shell script to copy a local Ruby script (`.rb`) into the OpenProject Docker container and execute it using the Rails runner.
    *   Usage: `./scripts/openproject_run_rails_script.sh [path/to/script.rb]`
*   **`run_tests.py`**: Test runner script that discovers and runs unit tests in the 'tests' directory. Supports patterns for running specific tests.
    *   Usage: `python scripts/run_tests.py [--pattern <test_pattern>] [--verbose]`
*   **`scriptrunner_api_endpoint_field.groovy`**: Groovy script for the Jira ScriptRunner Add-On. Creates a custom REST API endpoint in Jira for efficient custom field and option extraction.
*   **`setup_var_dirs.py`**: Creates the necessary directory structure within `var/` (`data`, `logs`, `scripts`) if it doesn't exist.
    *   Usage: `python scripts/setup_var_dirs.py`
*   **`simple_rails_command.py`**: Executes a single command within the OpenProject Rails console via SSH/Docker.
    *   Usage: `python scripts/simple_rails_command.py "Your Ruby Command"`
*   **`summarize_custom_fields.py`**: Reads JSON files containing Jira and OpenProject custom field data and prints a summary comparison.
    *   Usage: `python scripts/summarize_custom_fields.py <jira_fields.json> <op_fields.json>`
*   **`test_connection.py`**: Tests basic API connectivity to both Jira and OpenProject using the respective clients.
    *   Usage: `python scripts/test_connection.py`
*   **`test_rails_connection.py`**: Tests the connection to the OpenProject Rails console via SSH and Docker.
    *   Usage: `python scripts/test_rails_connection.py [--host <op_server>] [--debug]`

## Execution

Most Python scripts can be run from the project root directory within the development environment (ideally the Docker container):

```bash
docker exec -it j2o-app python scripts/<script_name.py> [arguments]
```

The shell script (`openproject_run_rails_script.sh`) should be run from the host machine if it needs to copy local files into the container.

The Groovy script (`scriptrunner_api_endpoint_field.groovy`) is intended to be installed within the Jira ScriptRunner Add-On configuration.
