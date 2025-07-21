"""Tests for documentation structure and content validation."""

import os
from pathlib import Path
import pytest


class TestDocumentationStructure:
    """Test that documentation files exist and have expected content."""
    
    @pytest.fixture
    def project_root(self) -> Path:
        """Get the project root directory."""
        return Path(__file__).parent.parent.parent
    
    def test_main_readme_exists(self, project_root: Path):
        """Test that main README.md exists and contains key sections."""
        readme_path = project_root / "README.md"
        assert readme_path.exists(), "Main README.md file should exist"
        
        content = readme_path.read_text()
        
        # Check for key sections that should be present
        expected_sections = [
            "# Jira to OpenProject Migration Tool",
            "## Features",
            "## Quick Start", 
            "## Architecture",
            "## Configuration",
            "## Documentation"
        ]
        
        for section in expected_sections:
            assert section in content, f"README should contain section: {section}"
    
    def test_consolidated_docs_exist(self, project_root: Path):
        """Test that all consolidated documentation files exist."""
        docs_dir = project_root / "docs"
        
        expected_docs = [
            "DEVELOPER_GUIDE.md",
            "ARCHITECTURE.md", 
            "WORKFLOW_STATUS_GUIDE.md",
            "SECURITY.md",
            "configuration.md"
        ]
        
        for doc_file in expected_docs:
            doc_path = docs_dir / doc_file
            assert doc_path.exists(), f"Documentation file should exist: {doc_file}"
            
            # Verify file has content
            content = doc_path.read_text()
            assert len(content) > 100, f"Documentation file should have substantial content: {doc_file}"
    
    def test_developer_guide_structure(self, project_root: Path):
        """Test that DEVELOPER_GUIDE.md has expected sections."""
        doc_path = project_root / "docs" / "DEVELOPER_GUIDE.md"
        content = doc_path.read_text()
        
        expected_sections = [
            "# Developer Guide",
            "## Quick Testing Commands",
            "## Development Standards",
            "## Exception-Based Error Handling",
            "## Security Requirements"
        ]
        
        for section in expected_sections:
            assert section in content, f"Developer Guide should contain: {section}"
    
    def test_architecture_guide_structure(self, project_root: Path):
        """Test that ARCHITECTURE.md has expected sections."""
        doc_path = project_root / "docs" / "ARCHITECTURE.md"
        content = doc_path.read_text()
        
        expected_sections = [
            "# System Architecture",
            "## Client Architecture", 
            "## Component Responsibilities",
            "## Exception Architecture"
        ]
        
        for section in expected_sections:
            assert section in content, f"Architecture Guide should contain: {section}"
    
    def test_workflow_guide_structure(self, project_root: Path):
        """Test that WORKFLOW_STATUS_GUIDE.md has expected sections."""
        doc_path = project_root / "docs" / "WORKFLOW_STATUS_GUIDE.md"
        content = doc_path.read_text()
        
        expected_sections = [
            "# OpenProject Workflow and Status Configuration",
            "## Automated Migration Process",
            "## Status Mapping Logic",
            "## Manual Workflow Configuration"
        ]
        
        for section in expected_sections:
            assert section in content, f"Workflow Guide should contain: {section}"
    
    def test_no_duplicate_documentation_files(self, project_root: Path):
        """Test that old duplicate documentation files have been removed."""
        removed_files = [
            "AGENTS.md",
            "CLAUDE.md", 
            "docs/client_architecture.md",
            "docs/TESTING_GUIDE.md",
            "docs/compliance_checklist.md",
            "docs/workflow_configuration.md",
            "docs/status_migration.md"
        ]
        
        for removed_file in removed_files:
            file_path = project_root / removed_file
            assert not file_path.exists(), f"Duplicate file should be removed: {removed_file}"
    
    def test_readme_links_to_documentation(self, project_root: Path):
        """Test that README.md properly links to consolidated documentation."""
        readme_path = project_root / "README.md"
        content = readme_path.read_text()
        
        expected_links = [
            "docs/DEVELOPER_GUIDE.md",
            "docs/ARCHITECTURE.md",
            "docs/SECURITY.md", 
            "docs/configuration.md",
            "docs/WORKFLOW_STATUS_GUIDE.md"
        ]
        
        for link in expected_links:
            assert link in content, f"README should link to: {link}" 