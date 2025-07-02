#!/usr/bin/env python3

import sys
import traceback

sys.path.append('src')
from clients.openproject_client import OpenProjectClient


def test_project_creation():
    # Initialize client
    op_client = OpenProjectClient()

    # Test the exact command that's failing
    project_name = 'AIDA TYPO3 10.4 Upgrade'
    identifier = 'aida-typo3-10-4-upgrade'
    description = ''

    # Escape for Ruby
    def ruby_escape(s):
        if not s:
            return ''
        return s.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')

    name_escaped = ruby_escape(project_name)
    desc_escaped = ruby_escape(description)

    print(f'Testing creation of project: {project_name}')
    print(f'Escaped name: {name_escaped}')
    print(f'Identifier: {identifier}')

    # Test step by step
    print("\n=== Testing step 1: Basic project creation ===")
    try:
        step1_script = f"Project.create!(name: '{name_escaped}', identifier: '{identifier}', description: '{desc_escaped}', public: false)"
        print(f'Step 1 command: {step1_script}')
        result1 = op_client.execute_query_to_json_file(step1_script)
        print(f'Step 1 result: {repr(result1)}')
        print(f'Step 1 result type: {type(result1)}')
    except Exception as e:
        print(f'Step 1 Exception: {e}')
        traceback.print_exc()
        return

    # If project creation succeeded, test modules
    if isinstance(result1, dict) and result1.get('id'):
        print(f"\n=== Project created successfully with ID {result1['id']} ===")
        project_id = result1['id']

        print("\n=== Testing step 2: Setting modules ===")
        try:
            step2_script = f"p = Project.find({project_id}); p.enabled_module_names = ['work_package_tracking', 'wiki']; p.save!; p.as_json"
            print(f'Step 2 command: {step2_script}')
            result2 = op_client.execute_query_to_json_file(step2_script)
            print(f'Step 2 result: {repr(result2)}')
            print(f'Step 2 result type: {type(result2)}')
        except Exception as e:
            print(f'Step 2 Exception: {e}')
            traceback.print_exc()
    else:
        print(f"\n=== Project creation failed ===")
        print("Let's test if identifier already exists:")

        try:
            check_script = f"Project.where(identifier: '{identifier}').count"
            check_result = op_client.execute_query_to_json_file(check_script)
            print(f'Identifier check result: {repr(check_result)}')

            if check_result and str(check_result).strip() != '0':
                print("ERROR: Project with this identifier already exists!")

                # Try to get details
                details_script = f"Project.find_by(identifier: '{identifier}').as_json"
                details_result = op_client.execute_query_to_json_file(details_script)
                print(f'Existing project details: {repr(details_result)}')
            else:
                print("Identifier is available, checking for validation errors...")

                # Try creating without the ! to see validation errors
                validate_script = f"p = Project.new(name: '{name_escaped}', identifier: '{identifier}', description: '{desc_escaped}', public: false); p.valid? ? 'VALID' : p.errors.full_messages"
                validate_result = op_client.execute_query_to_json_file(validate_script)
                print(f'Validation check: {repr(validate_result)}')

        except Exception as e:
            print(f'Check Exception: {e}')
            traceback.print_exc()


if __name__ == '__main__':
    test_project_creation()
