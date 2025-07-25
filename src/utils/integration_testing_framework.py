#!/usr/bin/env python3
"""Integration Testing Framework for Jira to OpenProject Migration."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union, Callable
from uuid import uuid4

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader


class TestEnvironment(Enum):
    """Test environment types."""
    MOCK = "mock"
    DOCKER = "docker"
    REAL = "real"


class TestScope(Enum):
    """Test scope levels."""
    UNIT = "unit"
    COMPONENT = "component"
    INTEGRATION = "integration"
    END_TO_END = "end_to_end"
    PERFORMANCE = "performance"


class TestResult(Enum):
    """Test result status."""
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class TestConfig:
    """Configuration for integration tests."""
    environment: TestEnvironment = TestEnvironment.MOCK
    scope: TestScope = TestScope.INTEGRATION
    timeout_seconds: int = 300
    max_retries: int = 3
    parallel_workers: int = 4
    cleanup_on_exit: bool = True
    generate_reports: bool = True
    performance_threshold_ms: int = 5000
    data_volume: str = "medium"  # small, medium, large
    enable_mocking: bool = True
    enable_docker: bool = False
    enable_real_apis: bool = False


@dataclass
class TestData:
    """Test data configuration."""
    jira_projects: int = 2
    jira_issues_per_project: int = 50
    jira_users: int = 10
    jira_attachments: int = 20
    jira_comments: int = 100
    op_projects: int = 2
    op_work_packages: int = 50
    op_users: int = 10
    custom_fields: int = 5
    workflows: int = 3


@dataclass
class TestReport:
    """Test execution report."""
    test_id: str
    test_name: str
    scope: TestScope
    environment: TestEnvironment
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: Optional[int] = None
    result: TestResult = TestResult.PASSED
    error_message: Optional[str] = None
    performance_metrics: Dict[str, Any] = field(default_factory=dict)
    data_metrics: Dict[str, Any] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)


@dataclass
class TestSuite:
    """Test suite configuration."""
    name: str
    description: str
    config: TestConfig
    test_data: TestData
    tests: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    setup_script: Optional[str] = None
    teardown_script: Optional[str] = None


class TestEnvironmentManager:
    """Manages test environment setup and teardown."""

    def __init__(self, config: TestConfig, work_dir: Path):
        self.config = config
        self.work_dir = work_dir
        self.temp_dirs: List[Path] = []
        self.docker_containers: List[str] = []
        self.processes: List[subprocess.Popen] = []

    async def setup(self) -> None:
        """Set up the test environment."""
        logging.info("Setting up test environment...")
        
        # Create work directory
        self.work_dir.mkdir(parents=True, exist_ok=True)
        
        # Set up environment based on type
        if self.config.environment == TestEnvironment.DOCKER:
            await self._setup_docker_environment()
        elif self.config.environment == TestEnvironment.REAL:
            await self._setup_real_environment()
        else:
            await self._setup_mock_environment()

    async def teardown(self) -> None:
        """Tear down the test environment."""
        logging.info("Tearing down test environment...")
        
        # Stop processes
        for process in self.processes:
            try:
                process.terminate()
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            except Exception as e:
                logging.warning(f"Error stopping process: {e}")

        # Stop Docker containers
        if self.config.environment == TestEnvironment.DOCKER:
            await self._teardown_docker_environment()

        # Clean up temp directories
        if self.config.cleanup_on_exit:
            for temp_dir in self.temp_dirs:
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    logging.warning(f"Error cleaning up {temp_dir}: {e}")

    async def _setup_mock_environment(self) -> None:
        """Set up mock environment."""
        # Create mock data directories
        mock_data_dir = self.work_dir / "mock_data"
        mock_data_dir.mkdir(exist_ok=True)
        self.temp_dirs.append(mock_data_dir)

        # Create mock configuration
        mock_config = {
            "jira": {
                "url": "https://mock-jira.example.com",
                "username": "mock_user",
                "password": "mock_password"
            },
            "openproject": {
                "url": "https://mock-openproject.example.com",
                "api_token": "mock_token"
            }
        }
        
        config_file = mock_data_dir / "mock_config.json"
        with open(config_file, 'w') as f:
            json.dump(mock_config, f, indent=2)

    async def _setup_docker_environment(self) -> None:
        """Set up Docker environment."""
        if not self.config.enable_docker:
            raise RuntimeError("Docker environment requested but not enabled")

        # Start Jira container
        jira_container = await self._start_jira_container()
        self.docker_containers.append(jira_container)

        # Start OpenProject container
        op_container = await self._start_openproject_container()
        self.docker_containers.append(op_container)

        # Wait for services to be ready
        await self._wait_for_services()

    async def _setup_real_environment(self) -> None:
        """Set up real environment."""
        if not self.config.enable_real_apis:
            raise RuntimeError("Real environment requested but not enabled")

        # Validate real service connectivity
        await self._validate_real_services()

    async def _start_jira_container(self) -> str:
        """Start Jira Docker container."""
        container_name = f"jira-test-{uuid4().hex[:8]}"
        
        cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            "-p", "8080:8080",
            "-e", "JIRA_HOME=/var/atlassian/jira",
            "-e", "JIRA_OPTS=-Datlassian.license.message=disabled",
            "atlassian/jira-software:latest"
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start Jira container: {result.stderr}")
        
        return container_name

    async def _start_openproject_container(self) -> str:
        """Start OpenProject Docker container."""
        container_name = f"openproject-test-{uuid4().hex[:8]}"
        
        cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            "-p", "8081:80",
            "-e", "OPENPROJECT_SECRET_KEY_BASE=test_secret",
            "-e", "OPENPROJECT_HOST__NAME=localhost:8081",
            "openproject/community:latest"
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start OpenProject container: {result.stderr}")
        
        return container_name

    async def _wait_for_services(self) -> None:
        """Wait for Docker services to be ready."""
        # Wait for Jira
        await self._wait_for_service("http://localhost:8080", "Jira")
        
        # Wait for OpenProject
        await self._wait_for_service("http://localhost:8081", "OpenProject")

    async def _wait_for_service(self, url: str, service_name: str) -> None:
        """Wait for a service to be ready."""
        import aiohttp
        
        for attempt in range(30):  # 30 attempts, 2 seconds each = 60 seconds
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=2) as response:
                        if response.status == 200:
                            logging.info(f"{service_name} is ready")
                            return
            except Exception:
                pass
            
            await asyncio.sleep(2)
        
        raise RuntimeError(f"{service_name} failed to start within timeout")

    async def _validate_real_services(self) -> None:
        """Validate real service connectivity."""
        # This would validate connectivity to real Jira and OpenProject instances
        # Implementation depends on the specific real environment configuration
        pass

    async def _teardown_docker_environment(self) -> None:
        """Tear down Docker environment."""
        for container in self.docker_containers:
            try:
                subprocess.run(["docker", "stop", container], check=True)
                subprocess.run(["docker", "rm", container], check=True)
            except subprocess.CalledProcessError as e:
                logging.warning(f"Error stopping container {container}: {e}")


class TestDataGenerator:
    """Generates test data for integration tests."""

    def __init__(self, config: TestData, work_dir: Path):
        self.config = config
        self.work_dir = work_dir
        self.data_dir = work_dir / "test_data"
        self.data_dir.mkdir(exist_ok=True)

    async def generate_all_data(self) -> Dict[str, Any]:
        """Generate all test data."""
        logging.info("Generating test data...")
        
        data = {
            "jira": await self._generate_jira_data(),
            "openproject": await self._generate_openproject_data(),
            "migration_config": await self._generate_migration_config(),
            "metadata": {
                "generated_at": datetime.now(UTC).isoformat(),
                "config": self.config.__dict__
            }
        }
        
        # Save data to files
        await self._save_data(data)
        
        return data

    async def _generate_jira_data(self) -> Dict[str, Any]:
        """Generate Jira test data."""
        projects = []
        issues = []
        users = []
        
        # Generate projects
        for i in range(self.config.jira_projects):
            project_key = f"TEST{i+1}"
            project = {
                "id": str(1000 + i),
                "key": project_key,
                "name": f"Test Project {i+1}",
                "projectTypeKey": "software",
                "simplified": False,
                "style": "classic",
                "isPrivate": False,
                "lead": {
                    "self": f"https://jira.example.com/rest/api/2/user?username=admin",
                    "name": "admin",
                    "key": "admin",
                    "emailAddress": "admin@example.com",
                    "displayName": "Administrator",
                    "active": True,
                    "timeZone": "UTC"
                }
            }
            projects.append(project)
            
            # Generate issues for this project
            for j in range(self.config.jira_issues_per_project):
                issue = {
                    "id": str(10000 + i * self.config.jira_issues_per_project + j),
                    "key": f"{project_key}-{j+1}",
                    "fields": {
                        "summary": f"Test Issue {j+1} in {project_key}",
                        "description": f"This is a test issue {j+1} in project {project_key}",
                        "project": project,
                        "issuetype": {
                            "id": "10001",
                            "name": "Bug",
                            "subtask": False
                        },
                        "status": {
                            "id": "10000",
                            "name": "To Do",
                            "statusCategory": {
                                "id": 2,
                                "key": "new",
                                "colorName": "blue-gray"
                            }
                        },
                        "priority": {
                            "id": "3",
                            "name": "Medium"
                        },
                        "assignee": {
                            "name": f"user{(j % self.config.jira_users) + 1}",
                            "emailAddress": f"user{(j % self.config.jira_users) + 1}@example.com",
                            "displayName": f"User {(j % self.config.jira_users) + 1}",
                            "active": True
                        },
                        "reporter": {
                            "name": "admin",
                            "emailAddress": "admin@example.com",
                            "displayName": "Administrator",
                            "active": True
                        },
                        "created": "2024-01-01T10:00:00.000+0000",
                        "updated": "2024-01-01T10:00:00.000+0000"
                    }
                }
                issues.append(issue)
        
        # Generate users
        for i in range(self.config.jira_users):
            users.append({
                "id": str(2000 + i),
                "name": f"user{i+1}",
                "emailAddress": f"user{i+1}@example.com",
                "displayName": f"User {i+1}",
                "active": True,
                "timeZone": "UTC"
            })
        
        return {
            "projects": projects,
            "issues": issues,
            "users": users,
            "custom_fields": self._generate_custom_fields(),
            "workflows": self._generate_workflows()
        }

    async def _generate_openproject_data(self) -> Dict[str, Any]:
        """Generate OpenProject test data."""
        projects = []
        work_packages = []
        users = []
        
        # Generate projects
        for i in range(self.config.op_projects):
            identifier = f"test{i+1}"
            project = {
                "id": i + 1,
                "name": f"Test Project {i+1}",
                "identifier": identifier,
                "description": {
                    "raw": f"Test project {i+1} for integration testing"
                },
                "status": "on_track",
                "is_public": True,
                "created_at": "2024-01-01T10:00:00Z",
                "updated_at": "2024-01-01T10:00:00Z"
            }
            projects.append(project)
            
            # Generate work packages for this project
            for j in range(self.config.op_work_packages):
                work_package = {
                    "id": j + 1,
                    "subject": f"Test Work Package {j+1}",
                    "description": {"raw": f"Description for work package {j+1}"},
                    "project_id": project["id"],
                    "type": "Task",
                    "status": "New",
                    "priority": "Normal",
                    "author": {
                        "id": 1,
                        "name": "admin",
                        "login": "admin"
                    },
                    "assigned_to": {
                        "id": (j % self.config.op_users) + 1,
                        "name": f"user{(j % self.config.op_users) + 1}",
                        "login": f"user{(j % self.config.op_users) + 1}"
                    },
                    "created_at": "2024-01-01T10:00:00Z",
                    "updated_at": "2024-01-01T10:00:00Z"
                }
                work_packages.append(work_package)
        
        # Generate users
        for i in range(self.config.op_users):
            users.append({
                "id": i + 1,
                "login": f"user{i+1}",
                "email": f"user{i+1}@example.com",
                "firstName": f"User{i+1}",
                "lastName": "Test",
                "admin": i == 0,  # First user is admin
                "status": "active"
            })
        
        return {
            "projects": projects,
            "work_packages": work_packages,
            "users": users
        }

    def _generate_custom_fields(self) -> List[Dict[str, Any]]:
        """Generate custom fields."""
        fields = []
        field_types = ["text", "number", "date", "select", "multiselect"]
        
        for i in range(self.config.custom_fields):
            field_type = field_types[i % len(field_types)]
            field = {
                "id": f"customfield_{10000 + i}",
                "name": f"Custom Field {i+1}",
                "type": field_type,
                "required": i % 3 == 0,
                "searcherKey": f"textsearcher" if field_type == "text" else f"{field_type}searcher"
            }
            
            if field_type in ["select", "multiselect"]:
                field["allowedValues"] = [
                    {"value": f"Option {j+1}"} for j in range(3)
                ]
            
            fields.append(field)
        
        return fields

    def _generate_workflows(self) -> List[Dict[str, Any]]:
        """Generate workflows."""
        workflows = []
        
        for i in range(self.config.workflows):
            workflow = {
                "id": i + 1,
                "name": f"Workflow {i+1}",
                "description": f"Test workflow {i+1}",
                "steps": [
                    {"id": 1, "name": "Open"},
                    {"id": 2, "name": "In Progress"},
                    {"id": 3, "name": "Resolved"},
                    {"id": 4, "name": "Closed"}
                ],
                "transitions": [
                    {"from": 1, "to": 2, "name": "Start Progress"},
                    {"from": 2, "to": 3, "name": "Resolve Issue"},
                    {"from": 3, "to": 4, "name": "Close Issue"}
                ]
            }
            workflows.append(workflow)
        
        return workflows

    async def _generate_migration_config(self) -> Dict[str, Any]:
        """Generate migration configuration."""
        return {
            "jira": {
                "url": "https://jira-test.example.com",
                "username": "test_user",
                "password": "test_password",
                "project_key": "TEST1"
            },
            "openproject": {
                "url": "https://openproject-test.example.com",
                "api_token": "test_token",
                "project_identifier": "test1"
            },
            "migration": {
                "include_attachments": True,
                "include_comments": True,
                "map_users": True,
                "default_user": "admin",
                "batch_size": 100,
                "max_concurrent": 4
            },
            "validation": {
                "pre_migration": True,
                "in_flight": True,
                "post_migration": True
            }
        }

    async def _save_data(self, data: Dict[str, Any]) -> None:
        """Save test data to files."""
        # Save Jira data
        jira_file = self.data_dir / "jira_data.json"
        with open(jira_file, 'w') as f:
            json.dump(data["jira"], f, indent=2)
        
        # Save OpenProject data
        op_file = self.data_dir / "openproject_data.json"
        with open(op_file, 'w') as f:
            json.dump(data["openproject"], f, indent=2)
        
        # Save migration config
        config_file = self.data_dir / "migration_config.json"
        with open(config_file, 'w') as f:
            json.dump(data["migration_config"], f, indent=2)
        
        # Save metadata
        metadata_file = self.data_dir / "metadata.json"
        with open(metadata_file, 'w') as f:
            json.dump(data["metadata"], f, indent=2)


class PerformanceMonitor:
    """Monitors performance during tests."""

    def __init__(self):
        self.metrics: Dict[str, List[float]] = {}
        self.start_times: Dict[str, float] = {}

    def start_timer(self, name: str) -> None:
        """Start a performance timer."""
        self.start_times[name] = time.time()

    def stop_timer(self, name: str) -> float:
        """Stop a performance timer and return duration."""
        if name not in self.start_times:
            raise ValueError(f"Timer '{name}' was not started")
        
        duration = time.time() - self.start_times[name]
        
        if name not in self.metrics:
            self.metrics[name] = []
        
        self.metrics[name].append(duration)
        del self.start_times[name]
        
        return duration

    def get_metrics(self) -> Dict[str, Any]:
        """Get performance metrics."""
        result = {}
        
        for name, durations in self.metrics.items():
            if durations:
                result[name] = {
                    "count": len(durations),
                    "total_ms": sum(durations) * 1000,
                    "avg_ms": (sum(durations) / len(durations)) * 1000,
                    "min_ms": min(durations) * 1000,
                    "max_ms": max(durations) * 1000,
                    "p95_ms": sorted(durations)[int(len(durations) * 0.95)] * 1000
                }
        
        return result

    def check_thresholds(self, thresholds: Dict[str, float]) -> Dict[str, bool]:
        """Check if metrics meet performance thresholds."""
        results = {}
        metrics = self.get_metrics()
        
        for name, threshold_ms in thresholds.items():
            if name in metrics:
                avg_ms = metrics[name]["avg_ms"]
                results[name] = avg_ms <= threshold_ms
            else:
                results[name] = False
        
        return results


class TestReporter:
    """Generates test reports."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.reports: List[TestReport] = []

    def add_report(self, report: TestReport) -> None:
        """Add a test report."""
        self.reports.append(report)

    def generate_reports(self) -> Dict[str, Path]:
        """Generate all test reports."""
        logging.info("Generating test reports...")
        
        report_files = {}
        
        # Generate JSON report
        json_file = self.output_dir / "test_results.json"
        self._generate_json_report(json_file)
        report_files["json"] = json_file
        
        # Generate HTML report
        html_file = self.output_dir / "test_results.html"
        self._generate_html_report(html_file)
        report_files["html"] = html_file
        
        # Generate JUnit XML report
        xml_file = self.output_dir / "test_results.xml"
        self._generate_junit_report(xml_file)
        report_files["junit"] = xml_file
        
        # Generate summary report
        summary_file = self.output_dir / "test_summary.txt"
        self._generate_summary_report(summary_file)
        report_files["summary"] = summary_file
        
        return report_files

    def _generate_json_report(self, output_file: Path) -> None:
        """Generate JSON report."""
        report_data = {
            "summary": self._get_summary_stats(),
            "reports": [report.__dict__ for report in self.reports],
            "generated_at": datetime.now(UTC).isoformat()
        }
        
        with open(output_file, 'w') as f:
            json.dump(report_data, f, indent=2, default=str)

    def _generate_html_report(self, output_file: Path) -> None:
        """Generate HTML report."""
        template = """
<!DOCTYPE html>
<html>
<head>
    <title>Integration Test Results</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .summary { background: #f5f5f5; padding: 15px; border-radius: 5px; margin-bottom: 20px; }
        .test { border: 1px solid #ddd; margin: 10px 0; padding: 10px; border-radius: 5px; }
        .passed { border-left: 5px solid #4CAF50; }
        .failed { border-left: 5px solid #f44336; }
        .skipped { border-left: 5px solid #ff9800; }
        .error { border-left: 5px solid #9c27b0; }
        .metrics { background: #e3f2fd; padding: 10px; margin: 10px 0; border-radius: 3px; }
    </style>
</head>
<body>
    <h1>Integration Test Results</h1>
    
    <div class="summary">
        <h2>Summary</h2>
        <p><strong>Total Tests:</strong> {{ summary.total }}</p>
        <p><strong>Passed:</strong> {{ summary.passed }}</p>
        <p><strong>Failed:</strong> {{ summary.failed }}</p>
        <p><strong>Success Rate:</strong> {{ "%.1f"|format(summary.success_rate) }}%</p>
        <p><strong>Total Duration:</strong> {{ "%.2f"|format(summary.total_duration) }}s</p>
    </div>
    
    <h2>Test Details</h2>
    {% for report in reports %}
    <div class="test {{ report.result.value }}">
        <h3>{{ report.test_name }}</h3>
        <p><strong>Result:</strong> {{ report.result.value.upper() }}</p>
        <p><strong>Duration:</strong> {{ "%.2f"|format(report.duration_ms/1000) if report.duration_ms else "N/A" }}s</p>
        <p><strong>Scope:</strong> {{ report.scope.value }}</p>
        <p><strong>Environment:</strong> {{ report.environment.value }}</p>
        {% if report.error_message %}
        <p><strong>Error:</strong> {{ report.error_message }}</p>
        {% endif %}
        {% if report.performance_metrics %}
        <div class="metrics">
            <h4>Performance Metrics</h4>
            <pre>{{ report.performance_metrics | tojson(indent=2) }}</pre>
        </div>
        {% endif %}
    </div>
    {% endfor %}
</body>
</html>
        """
        
        env = Environment()
        template_obj = env.from_string(template)
        
        html_content = template_obj.render(
            summary=self._get_summary_stats(),
            reports=self.reports
        )
        
        with open(output_file, 'w') as f:
            f.write(html_content)

    def _generate_junit_report(self, output_file: Path) -> None:
        """Generate JUnit XML report."""
        xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n'
        xml_content += '<testsuites>\n'
        xml_content += f'  <testsuite name="Integration Tests" tests="{len(self.reports)}">\n'
        
        for report in self.reports:
            xml_content += f'    <testcase name="{report.test_name}" classname="{report.scope.value}">\n'
            
            if report.result == TestResult.FAILED:
                xml_content += f'      <failure message="{report.error_message or "Test failed"}">\n'
                xml_content += f'        {report.error_message or "Test failed"}\n'
                xml_content += '      </failure>\n'
            elif report.result == TestResult.ERROR:
                xml_content += f'      <error message="{report.error_message or "Test error"}">\n'
                xml_content += f'        {report.error_message or "Test error"}\n'
                xml_content += '      </error>\n'
            elif report.result == TestResult.SKIPPED:
                xml_content += f'      <skipped message="{report.error_message or "Test skipped"}"/>\n'
            
            xml_content += '    </testcase>\n'
        
        xml_content += '  </testsuite>\n'
        xml_content += '</testsuites>\n'
        
        with open(output_file, 'w') as f:
            f.write(xml_content)

    def _generate_summary_report(self, output_file: Path) -> None:
        """Generate summary text report."""
        summary = self._get_summary_stats()
        
        with open(output_file, 'w') as f:
            f.write("Integration Test Results Summary\n")
            f.write("=" * 40 + "\n\n")
            f.write(f"Total Tests: {summary['total']}\n")
            f.write(f"Passed: {summary['passed']}\n")
            f.write(f"Failed: {summary['failed']}\n")
            f.write(f"Skipped: {summary['skipped']}\n")
            f.write(f"Errors: {summary['errors']}\n")
            f.write(f"Success Rate: {summary['success_rate']:.1f}%\n")
            f.write(f"Total Duration: {summary['total_duration']:.2f}s\n\n")
            
            f.write("Test Results by Scope:\n")
            f.write("-" * 25 + "\n")
            for scope in TestScope:
                scope_reports = [r for r in self.reports if r.scope == scope]
                if scope_reports:
                    passed = sum(1 for r in scope_reports if r.result == TestResult.PASSED)
                    f.write(f"{scope.value}: {passed}/{len(scope_reports)} passed\n")
            
            f.write("\nTest Results by Environment:\n")
            f.write("-" * 30 + "\n")
            for env in TestEnvironment:
                env_reports = [r for r in self.reports if r.environment == env]
                if env_reports:
                    passed = sum(1 for r in env_reports if r.result == TestResult.PASSED)
                    f.write(f"{env.value}: {passed}/{len(env_reports)} passed\n")

    def _get_summary_stats(self) -> Dict[str, Any]:
        """Get summary statistics."""
        total = len(self.reports)
        passed = sum(1 for r in self.reports if r.result == TestResult.PASSED)
        failed = sum(1 for r in self.reports if r.result == TestResult.FAILED)
        skipped = sum(1 for r in self.reports if r.result == TestResult.SKIPPED)
        errors = sum(1 for r in self.reports if r.result == TestResult.ERROR)
        
        success_rate = (passed / total * 100) if total > 0 else 0
        
        total_duration = sum(r.duration_ms or 0 for r in self.reports) / 1000
        
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "errors": errors,
            "success_rate": success_rate,
            "total_duration": total_duration
        }


class IntegrationTestingFramework:
    """Main integration testing framework."""

    def __init__(self, config: TestConfig, work_dir: Optional[Path] = None):
        self.config = config
        self.work_dir = work_dir or Path(tempfile.mkdtemp(prefix="integration_test_"))
        self.environment_manager = TestEnvironmentManager(config, self.work_dir)
        self.data_generator = TestDataGenerator(TestData(), self.work_dir)
        self.performance_monitor = PerformanceMonitor()
        self.reporter = TestReporter(self.work_dir / "reports")
        self.test_data: Optional[Dict[str, Any]] = None
        self.logger = logging.getLogger(__name__)

    async def run_test_suite(self, test_suite: TestSuite) -> Dict[str, Any]:
        """Run a complete test suite."""
        self.logger.info(f"Starting test suite: {test_suite.name}")
        
        try:
            # Set up environment
            await self.environment_manager.setup()
            
            # Generate test data
            self.test_data = await self.data_generator.generate_all_data()
            
            # Run tests
            results = await self._run_tests(test_suite)
            
            # Generate reports
            if self.config.generate_reports:
                report_files = self.reporter.generate_reports()
                results["report_files"] = {k: str(v) for k, v in report_files.items()}
            
            return results
            
        finally:
            # Clean up
            await self.environment_manager.teardown()

    async def run_single_test(self, test_name: str, test_func: Callable) -> TestReport:
        """Run a single test."""
        test_id = str(uuid4())
        start_time = datetime.now(UTC)
        
        report = TestReport(
            test_id=test_id,
            test_name=test_name,
            scope=self.config.scope,
            environment=self.config.environment,
            start_time=start_time
        )
        
        try:
            self.performance_monitor.start_timer(test_name)
            
            # Run the test
            await test_func()
            
            # Record success
            end_time = datetime.now(UTC)
            duration = self.performance_monitor.stop_timer(test_name)
            
            report.end_time = end_time
            report.duration_ms = int(duration * 1000)
            report.result = TestResult.PASSED
            report.performance_metrics = self.performance_monitor.get_metrics()
            
        except Exception as e:
            # Record failure
            end_time = datetime.now(UTC)
            duration = self.performance_monitor.stop_timer(test_name) if test_name in self.performance_monitor.start_times else 0
            
            report.end_time = end_time
            report.duration_ms = int(duration * 1000) if duration else None
            report.result = TestResult.FAILED
            report.error_message = str(e)
            report.performance_metrics = self.performance_monitor.get_metrics()
            
            self.logger.error(f"Test {test_name} failed: {e}")
        
        # Add report to reporter
        self.reporter.add_report(report)
        
        return report

    async def _run_tests(self, test_suite: TestSuite) -> Dict[str, Any]:
        """Run all tests in the suite."""
        results = {
            "suite_name": test_suite.name,
            "start_time": datetime.now(UTC).isoformat(),
            "tests": [],
            "summary": {}
        }
        
        # Run tests sequentially for now (can be parallelized later)
        for test_name in test_suite.tests:
            test_func = self._get_test_function(test_name)
            if test_func:
                report = await self.run_single_test(test_name, test_func)
                results["tests"].append(report.__dict__)
            else:
                self.logger.warning(f"Test function not found: {test_name}")
        
        # Generate summary
        results["end_time"] = datetime.now(UTC).isoformat()
        results["summary"] = self.reporter._get_summary_stats()
        
        return results

    def _get_test_function(self, test_name: str) -> Optional[Callable]:
        """Get test function by name."""
        # This would map test names to actual test functions
        # For now, return None - this would be implemented based on the test registry
        return None

    @asynccontextmanager
    async def test_context(self):
        """Context manager for test setup and teardown."""
        try:
            await self.environment_manager.setup()
            self.test_data = await self.data_generator.generate_all_data()
            yield self
        finally:
            await self.environment_manager.teardown()

    def create_test_suite(self, name: str, description: str, tests: List[str]) -> TestSuite:
        """Create a test suite configuration."""
        return TestSuite(
            name=name,
            description=description,
            config=self.config,
            test_data=TestData(),
            tests=tests
        )


# Convenience functions for common test scenarios
async def run_integration_tests(config: TestConfig, test_suites: List[TestSuite]) -> Dict[str, Any]:
    """Run multiple test suites."""
    framework = IntegrationTestingFramework(config)
    results = {}
    
    for suite in test_suites:
        try:
            suite_results = await framework.run_test_suite(suite)
            results[suite.name] = suite_results
        except Exception as e:
            results[suite.name] = {"error": str(e)}
    
    return results


def create_basic_test_suite() -> TestSuite:
    """Create a basic test suite for common scenarios."""
    config = TestConfig(
        environment=TestEnvironment.MOCK,
        scope=TestScope.INTEGRATION,
        timeout_seconds=300,
        generate_reports=True
    )
    
    return TestSuite(
        name="Basic Integration Tests",
        description="Basic integration tests for migration components",
        config=config,
        test_data=TestData(
            jira_projects=1,
            jira_issues_per_project=10,
            jira_users=3,
            op_projects=1,
            op_work_packages=10,
            op_users=3
        ),
        tests=[
            "test_jira_connection",
            "test_openproject_connection",
            "test_data_validation",
            "test_migration_workflow"
        ]
    )


if __name__ == "__main__":
    # Example usage
    async def main():
        # Create test suite
        suite = create_basic_test_suite()
        
        # Run tests
        framework = IntegrationTestingFramework(suite.config)
        results = await framework.run_test_suite(suite)
        
        print("Test Results:")
        print(json.dumps(results, indent=2, default=str))
    
    asyncio.run(main()) 