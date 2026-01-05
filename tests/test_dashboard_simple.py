#!/usr/bin/env python3
"""Simple test for the dashboard functionality."""

import sys
from pathlib import Path

import pytest

# Add the src directory to the path
sys.path.insert(0, str(Path(__file__).parent / "src"))


def test_dashboard_imports() -> bool:
    """Test that dashboard components can be imported."""
    print("Testing dashboard imports...")

    try:
        from dashboard.app import app  # noqa: F401

        print("✓ Successfully imported FastAPI app")
    except ImportError as e:
        print(f"✗ Failed to import FastAPI app: {e}")
        return False

    try:
        from dashboard.app import ConnectionManager  # noqa: F401

        print("✓ Successfully imported ConnectionManager")
    except ImportError as e:
        print(f"✗ Failed to import ConnectionManager: {e}")
        return False

    try:
        from dashboard.app import MigrationMetrics, MigrationProgress  # noqa: F401

        print("✓ Successfully imported data models")
    except ImportError as e:
        print(f"✗ Failed to import data models: {e}")
        return False

    return True


def test_dashboard_routes() -> bool | None:
    """Test that dashboard routes are properly configured."""
    print("\nTesting dashboard routes...")

    try:
        from dashboard.app import app

        # Check that the app has the expected routes
        routes = [route.path for route in app.routes]
        expected_routes = [
            "/",
            "/ws/progress",
            "/ws/dashboard",
            "/api/progress",
            "/api/metrics",
            "/api/metrics/csv",
            "/api/migration/status",
            "/api/migration/start",
            "/api/migration/stop",
        ]

        for route in expected_routes:
            if route in routes:
                print(f"✓ Route {route} found")
            else:
                print(f"✗ Route {route} not found")
                return False

        return True

    except Exception as e:
        print(f"✗ Error testing routes: {e}")
        return False


def test_dashboard_templates() -> bool:
    """Test that dashboard templates exist."""
    print("\nTesting dashboard templates...")

    template_path = Path("src/dashboard/templates/dashboard.html")
    if template_path.exists():
        print("✓ Dashboard HTML template exists")
    else:
        print("✗ Dashboard HTML template not found")
        return False

    css_path = Path("src/dashboard/static/css/dashboard.css")
    if css_path.exists():
        print("✓ Dashboard CSS file exists")
    else:
        print("✗ Dashboard CSS file not found")
        return False

    js_path = Path("src/dashboard/static/js/dashboard.js")
    if js_path.exists():
        print("✓ Dashboard JavaScript file exists")
    else:
        print("✗ Dashboard JavaScript file not found")
        return False

    return True


@pytest.mark.asyncio
async def test_dashboard_api() -> bool | None:
    """Test dashboard API endpoints."""
    print("\nTesting dashboard API endpoints...")

    try:
        from fastapi.testclient import TestClient

        from dashboard.app import app

        client = TestClient(app)

        # Test dashboard page
        response = client.get("/")
        if response.status_code == 200:
            print("✓ Dashboard page loads successfully")
        else:
            print(f"✗ Dashboard page failed to load: {response.status_code}")
            return False

        # Test migration status API
        response = client.get("/api/migration/status")
        if response.status_code == 200:
            print("✓ Migration status API works")
        else:
            print(f"✗ Migration status API failed: {response.status_code}")
            return False

        # Test metrics API
        response = client.get("/api/metrics")
        if response.status_code == 200:
            print("✓ Metrics API works")
        else:
            print(f"✗ Metrics API failed: {response.status_code}")
            return False

        return True

    except ImportError as e:
        print(f"✗ Could not test API (missing dependency): {e}")
        return False
    except Exception as e:
        print(f"✗ Error testing API: {e}")
        return False


def main():
    """Run all dashboard tests."""
    print("=== Dashboard Functionality Test ===\n")

    tests = [
        test_dashboard_imports,
        test_dashboard_routes,
        test_dashboard_templates,
    ]

    passed = 0
    total = len(tests)

    for test in tests:
        if test():
            passed += 1
        print()

    # Try API test if possible
    try:
        import asyncio

        if asyncio.run(test_dashboard_api()):
            passed += 1
        total += 1
    except Exception as e:
        print(f"API test skipped: {e}")

    print(f"=== Test Results: {passed}/{total} tests passed ===")

    if passed == total:
        print("🎉 All tests passed! Dashboard is ready to use.")
        print("\nTo start the dashboard:")
        print("1. Install dependencies: pip install fastapi uvicorn")
        print("2. Run: python src/dashboard/app.py")
        print("3. Open: http://localhost:8000")
    else:
        print("❌ Some tests failed. Please check the implementation.")

    return passed == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
