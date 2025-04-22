import os
import sys

import pandas as pd
import pytest
import requests
import yaml
from dotenv import load_dotenv
from jira import JIRA


def test_python_version():
    """Test Python version is 3.12.x"""
    assert sys.version_info.major == 3
    assert sys.version_info.minor == 12


def _test_required_packages():
    """Test all required packages are installed"""
    # Test requests
    response = requests.get("https://httpbin.org/get")
    assert response.status_code == 200

    # Test pandas
    df = pd.DataFrame({"test": [1, 2, 3]})
    assert len(df) == 3

    # Test yaml
    test_yaml = yaml.safe_load("key: value")
    assert test_yaml["key"] == "value"


def test_env_file():
    """Test .env file exists and can be loaded"""
    # Try loading from .env first, then .env.test if needed
    load_dotenv()

    # Check for prefixed or non-prefixed variables
    jira_url = os.getenv("JIRA_URL") or os.getenv("J2O_JIRA_URL")
    jira_token = os.getenv("JIRA_API_TOKEN") or os.getenv("J2O_JIRA_API_TOKEN")
    webhook_url = os.getenv("MATRIX_HOOKSHOT_WEBHOOK_URL") or os.getenv(
        "J2O_MATRIX_HOOKSHOT_WEBHOOK_URL"
    )

    # If variables still not found, try loading from .env.test specifically
    if jira_url is None:
        load_dotenv(".env.test")
        jira_url = os.getenv("JIRA_URL") or os.getenv("J2O_JIRA_URL")
        jira_token = os.getenv("JIRA_API_TOKEN") or os.getenv("J2O_JIRA_API_TOKEN")
        webhook_url = os.getenv("MATRIX_HOOKSHOT_WEBHOOK_URL") or os.getenv(
            "J2O_MATRIX_HOOKSHOT_WEBHOOK_URL"
        )

    assert (
        jira_url is not None
    ), "Neither JIRA_URL nor J2O_JIRA_URL found in environment"
    assert (
        jira_token is not None
    ), "Neither JIRA_API_TOKEN nor J2O_JIRA_API_TOKEN found in environment"
    assert (
        webhook_url is not None
    ), "Neither MATRIX_HOOKSHOT_WEBHOOK_URL nor J2O_MATRIX_HOOKSHOT_WEBHOOK_URL found in environment"


@pytest.mark.skip(reason="No Jira instance available in test environment")
def test_jira_client_creation():
    """Test JIRA client can be instantiated"""
    load_dotenv()

    # If JIRA_URL is not set, try loading from .env.test
    if os.getenv("JIRA_URL") is None:
        load_dotenv(".env.test")

    jira = JIRA(server=os.getenv("JIRA_URL"), token_auth=os.getenv("JIRA_API_TOKEN"))
    assert jira is not None
