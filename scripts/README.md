# Utility Scripts (`scripts/`)

This directory contains various utility scripts to support the migration process, development, and testing.

## Script List

*   **`cleanup_var.py`**: Cleans specified subdirectories within the `var/` directory (e.g., `var/data`, `var/logs`) or the entire `var/` directory. Useful for starting a fresh migration run.
    *   Usage: `python scripts/cleanup_var.py [--clean <dir_name|all>]`
*   **`convert_rails_to_json.py`**: Attempts to convert Ruby hash/array syntax (often found in Rails console output) within a file into standard JSON format.
    *   Usage: `python scripts/convert_rails_to_json.py <input_ruby_file> <output_json_file>`
*   **`create_custom_fields_json.py`**: Parses a specific text format listing custom fields (likely from manual extraction or older script output) and converts it into a JSON file.
    *   Usage: `python scripts/create_custom_fields_json.py <input_text_file> <output_json_file>`
*   **`extract_json.py`**: Extracts JSON arrays embedded within a larger text file (e.g., log files).
    *   Usage: `python scripts/extract_json.py <input_log_file> <output_json_file>`
*   **`extract_tmux_custom_fields.py`**: Captures output from a `tmux` session (assumed to be running a Rails console) and parses OpenProject custom field information from it.
    *   Usage: `python scripts/extract_tmux_custom_fields.py [output_json_file]`
*   **`get_complete_custom_fields.py`**: Interacts with a `tmux` session running a Rails console to execute a more detailed Ruby command for fetching complete custom field details (including possible values) and parses the output.
    *   Usage: `python scripts/get_complete_custom_fields.py [output_json_file]`
*   **`get_custom_fields_simple.py`**: Interacts with a `tmux` session running a Rails console to execute a simpler Ruby command for fetching basic custom field details and parses the output.
    *   Usage: `python scripts/get_custom_fields_simple.py [output_json_file]`
*   **`get_custom_fields.rb`**: A Ruby script intended to be run *inside* the OpenProject Rails console. It fetches all custom fields and prints them as JSON to `/tmp/openproject_custom_fields.json` within the container.
*   **`get_openproject_fields.py`**: A more robust script that attempts to retrieve OpenProject custom fields by running `get_custom_fields.rb` inside the Rails console (via `tmux` or potentially direct `docker exec`), retrieving the generated JSON file, and parsing it. Includes fallback parsing methods.
    *   Usage: `python scripts/get_openproject_fields.py [--output <output_json_file>]`
*   **`list_jira_data.py`**: Connects to the configured Jira instance and lists various metadata like projects, issue types, statuses, priorities, and custom fields, saving the output to JSON files in `var/data/jira_exports/`.
    *   Usage: `python scripts/list_jira_data.py [--list <projects|issue_types|statuses|priorities|custom_fields|all>]`
*   **`openproject_run_rails_script.sh`**: A shell script designed to copy a local Ruby script (`.rb`) into the configured OpenProject Docker container and execute it using the Rails runner. Primarily used for manually running generated migration scripts.
    *   Usage: `./scripts/openproject_run_rails_script.sh [path/to/script.rb]` (uses latest `.rb` in `output/` if no path given)
*   **`run_tests.py`**: A test runner script that discovers and runs unit tests in the 'tests' directory. Supports patterns for running specific tests.
    *   Usage: `python scripts/run_tests.py [--pattern <test_pattern>] [--verbose]`
*   **`scriptrunner_api_endpoint_field.groovy`**: A Groovy script for the Jira ScriptRunner Add-On. Creates a custom REST API endpoint in Jira (`/rest/scriptrunner/latest/custom/getAllCustomFieldsWithOptions`) that efficiently returns all custom fields and their options, overcoming limitations of the standard Jira API.
*   **`setup_var_dirs.py`**: Creates the necessary directory structure within `var/` (`data`, `logs`, `scripts`) if it doesn't exist.
    *   Usage: `python scripts/setup_var_dirs.py`
*   **`simple_rails_command.py`**: A utility to execute a single, simple command within the OpenProject Rails console via the SSH/Docker connection managed by `OpenProjectRailsClient`.
    *   Usage: `python scripts/simple_rails_command.py "Your Ruby Command"`
*   **`summarize_custom_fields.py`**: Reads JSON files containing Jira and OpenProject custom field data and prints a summary comparison.
    *   Usage: `python scripts/summarize_custom_fields.py <jira_fields.json> <op_fields.json>`
*   **`test_connection.py`**: Tests basic API connectivity to both the configured Jira and OpenProject instances using the respective clients.
    *   Usage: `python scripts/test_connection.py`
*   **`test_rails_connection.py`**: Performs a comprehensive test of the connection to the OpenProject Rails console via SSH and Docker, verifying each step (SSH connection, Docker exec, Rails console launch, command execution).
    *   Usage: `python scripts/test_rails_connection.py [--host <op_server>] [--debug]`

## Execution

Most Python scripts can be run from the project root directory within the development environment (ideally the Docker container):

```bash
docker exec -it j2o-app python scripts/<script_name.py> [arguments]
```

The shell script (`openproject_run_rails_script.sh`) should be run from the host machine if it needs to copy local files into the container.

The Groovy script (`scriptrunner_api_endpoint_field.groovy`) is intended to be installed within the Jira ScriptRunner Add-On configuration.
