#!/usr/bin/env python3
"""Setup script for the Jira to OpenProject migration tool.
"""

from setuptools import find_packages, setup

# Read requirements from requirements.txt file
with open("requirements.txt") as f:
    requirements = f.read().splitlines()

# Remove any comments or blank lines from requirements
requirements = [line for line in requirements if line and not line.startswith("#")]

setup(
    name="jira-to-openproject",
    version="0.1.0",
    description="Jira to OpenProject migration tool",
    author="Sebastian Mendel",
    author_email="sebastian.mendel@netresearch.de",
    url="https://github.com/netresearch/jira-to-openproject",
    packages=find_packages(),
    include_package_data=True,
    python_requires=">=3.8,<4.0",
    install_requires=requirements,
    license="MIT",  # SPDX license identifier
    entry_points={
        "console_scripts": [
            "j2o=src.main:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: System Administrators",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
)
