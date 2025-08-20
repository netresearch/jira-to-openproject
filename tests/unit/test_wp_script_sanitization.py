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

    assert "wp_attrs.delete('_links')" in ruby
    assert "sanitized_attrs = wp_attrs.reject { |k,_| k == \"_links\" || k == \"watcher_ids\" }" in ruby
    assert "wp.assign_attributes(sanitized_attrs.except('project_id', 'type_id', 'type_name', 'subject'))" in ruby

    retry_idx = ruby.find("# Refresh the work package for the next attempt")
    assert retry_idx != -1
    sub = ruby[retry_idx:]
    assert "wp_attrs.delete('_links')" in sub
    assert "sanitized_attrs = wp_attrs.reject { |k,_| k == \"_links\" || k == \"watcher_ids\" }" in sub
    assert "wp.assign_attributes(sanitized_attrs.except('project_id', 'type_id', 'type_name', 'subject'))" in sub

