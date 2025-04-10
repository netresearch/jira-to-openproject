import os
import json
import re
import unicodedata
from typing import Any, Optional

from src.config import logger

def load_json_file(file_path: str) -> Optional[Any]:
    """Load data from a JSON file.

    Args:
        file_path: Path to the JSON file.

    Returns:
        Loaded JSON data or None if an error occurred.
    """
    if not os.path.exists(file_path):
        logger.debug(f"JSON file not found: {file_path}")
        return None
    try:
        with open(file_path, "r", encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON file {file_path}: {e}")
        return None
    except IOError as e:
        logger.error(f"Error reading file {file_path}: {e}")
        return None

def save_json_file(data: Any, file_path: str) -> bool:
    """Save data to a JSON file.

    Args:
        data: Data to save.
        file_path: Path to the JSON file.

    Returns:
        True if successful, False otherwise.
    """
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.debug(f"Saved data to {file_path}")
        return True
    except IOError as e:
        logger.error(f"Error writing file {file_path}: {e}")
        return False
    except TypeError as e:
        logger.error(f"Error serializing data to JSON for {file_path}: {e}")
        return False

def sanitize_identifier(input_string: str) -> str:
    """
    Sanitizes a string to be a valid OpenProject identifier.
    Rules:
    - Lowercase
    - Alphanumeric and dashes only
    - Must start and end with an alphanumeric character
    - Dashes cannot be consecutive
    - Max length (optional, not strictly enforced here but good practice)
    """
    if not input_string:
        return "default-identifier" # Or raise error

    # Normalize unicode characters
    normalized = unicodedata.normalize('NFKD', input_string).encode('ascii', 'ignore').decode('ascii')

    # Lowercase
    s = normalized.lower()

    # Replace spaces and invalid chars with dashes
    s = re.sub(r'[^a-z0-9]+', '-', s)

    # Remove leading/trailing dashes
    s = s.strip('-')

    # Replace consecutive dashes with a single dash
    s = re.sub(r'-{2,}', '-', s)

    # Ensure it's not empty after sanitization
    if not s:
        # Fallback based on original string hash or similar if needed
        return "sanitized-identifier"

    # Optional: Truncate to a max length (e.g., 100)
    # max_len = 100
    # s = s[:max_len]

    # Ensure it still doesn't end with a dash after potential truncation
    # s = s.strip('-')

    return s

def sanitize_for_filename(input_string: str) -> str:
    """
    Sanitizes a string to be safe for use as a filename.
    Removes or replaces characters that are problematic in filenames across OSes.
    """
    if not input_string:
        return "default_filename"

    # Normalize unicode
    s = unicodedata.normalize('NFKD', input_string).encode('ascii', 'ignore').decode('ascii')

    # Remove characters known to be problematic: / \ : * ? " < > |
    s = re.sub(r'[\/\\:*?"<>|]', '_', s)

    # Replace multiple spaces/underscores with a single underscore
    s = re.sub(r'[\s_]+', '_', s)

    # Remove leading/trailing underscores/spaces
    s = s.strip('_ ')

    # Limit length (e.g., 200 chars)
    s = s[:200]

    if not s:
        return "sanitized_filename"

    return s
