import pytest
import os
import sys
import requests
from jira import JIRA
import pandas as pd
import yaml
from dotenv import load_dotenv


def test_python_version():
    """Test Python version is 3.13.x"""
    assert sys.version_info.major == 3
    assert sys.version_info.minor == 13


def test_required_packages():
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
    load_dotenv()
    assert os.getenv("JIRA_URL") is not None
    assert os.getenv("JIRA_API_TOKEN") is not None
    assert os.getenv("MATRIX_HOOKSHOT_WEBHOOK_URL") is not None


def test_jira_client_creation():
    """Test JIRA client can be instantiated"""
    load_dotenv()
    jira = JIRA(server=os.getenv("JIRA_URL"), token=os.getenv("JIRA_API_TOKEN"))
    assert jira is not None
