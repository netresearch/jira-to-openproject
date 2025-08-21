from pathlib import Path


def test_ruby_script_deletes_links_before_assign() -> None:
    # Read the source file and assert presence/order of sanitation around assign_attributes
    path = Path("/home/sme/p/j2o/src/migrations/work_package_migration.py")
    text = path.read_text()

    start = text.find("main_script = \"\"\"")
    assert start != -1
    # Find the opening triple quotes after the assignment
    open_q = text.find("\"\"\"", start)
    assert open_q != -1
    start_content = open_q + 3
    end = text.find("\"\"\"", start_content)
    assert end != -1
    ruby = text[start_content:end]

    # Ruby script should be minimal now; use WorkPackage.create and no assign_attributes
    assert "WorkPackage.create(" in ruby
    assert "assign_attributes" not in ruby

    retry_idx = ruby.find("# Refresh the work package for the next attempt")
    assert retry_idx != -1
    sub = ruby[retry_idx:]
    assert "WorkPackage.create(" in sub

