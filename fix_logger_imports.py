#!/usr/bin/env python3
"""Fix logger import issues across the codebase."""

import os
import re
from pathlib import Path

def fix_logger_imports():
    """Fix logger import issues in all Python files."""
    
    # Get the project root
    project_root = Path(__file__).parent
    
    # Find all Python files
    python_files = []
    for root, dirs, files in os.walk(project_root):
        # Skip certain directories
        if any(skip in root for skip in ['.git', '__pycache__', '.venv', 'venv', '.pytest_cache']):
            continue
            
        for file in files:
            if file.endswith('.py'):
                python_files.append(Path(root) / file)
    
    fixed_files = []
    
    for file_path in python_files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Check if file has the problematic import
            if 'logger = configure_logging("INFO", None)' in content:
                # Replace the import pattern
                new_content = re.sub(
                    r'from src import config\s*\n(.*?)\nlogger = config\.logger',
                    r'from src.display import configure_logging\n\1\nlogger = configure_logging("INFO", None)',
                    content,
                    flags=re.DOTALL
                )
                
                # Also handle cases where config is imported differently
                new_content = re.sub(
                    r'import src\.config as config\s*\n(.*?)\nlogger = config\.logger',
                    r'from src.display import configure_logging\n\1\nlogger = configure_logging("INFO", None)',
                    new_content,
                    flags=re.DOTALL
                )
                
                # Handle cases where config is imported at module level
                new_content = re.sub(
                    r'logger = config\.logger',
                    r'logger = configure_logging("INFO", None)',
                    new_content
                )
                
                # Add the import if it's not already there
                if 'from src.display import configure_logging' not in new_content:
                    # Find the last import statement and add after it
                    lines = new_content.split('\n')
                    for i, line in enumerate(lines):
                        if line.strip().startswith('import ') or line.strip().startswith('from '):
                            continue
                        elif line.strip() == '':
                            continue
                        else:
                            # Insert the import before this line
                            lines.insert(i, 'from src.display import configure_logging')
                            break
                    new_content = '\n'.join(lines)
                
                # Write the fixed content back
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                
                fixed_files.append(file_path)
                print(f"Fixed: {file_path}")
                
        except Exception as e:
            print(f"Error processing {file_path}: {e}")
    
    print(f"\nFixed {len(fixed_files)} files:")
    for file_path in fixed_files:
        print(f"  - {file_path}")

if __name__ == "__main__":
    fix_logger_imports() 