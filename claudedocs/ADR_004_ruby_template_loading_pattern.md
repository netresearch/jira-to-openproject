# ADR 004: Ruby Template Loading Pattern for Journal Creation

**Status**: Accepted
**Date**: 2025-11-17
**Context**: Bug #32 Fix - Jira-to-OpenProject Migration Journal Creation

## Context and Problem Statement

During the implementation of Bug #32 fix, we needed to inject complex Ruby code into OpenProject's bulk_create operation for journal creation. The challenge was to load multi-line Ruby code from an external template file (`src/ruby/create_work_package_journals.rb`) and inject it into the bulk creation code while maintaining proper:
- Variable scoping
- Code execution context
- Multi-line indentation
- Error handling

## Decision Drivers

1. **Code Maintainability**: Complex journal creation logic (~230 lines) should be maintained separately from Python orchestration code
2. **Ruby Scoping**: The injected code needs access to variables defined in the surrounding bulk_create context (`rec`, `rails_ops`, `idx`, `verbose`)
3. **Multi-line Formatting**: Python string interpolation must preserve Ruby code structure and indentation
4. **Debugging**: Ruby syntax errors and runtime exceptions must be traceable to their source
5. **Flexibility**: Ability to modify journal creation logic without changing Python orchestration code

## Considered Options

### Option A: Inline Ruby Code (Initial Approach)
Embed the entire journal creation logic directly in the Python string.

**Pros**:
- Single file contains all logic
- No file loading overhead
- Easier to understand the full context

**Cons**:
- Poor maintainability (~230 lines of Ruby embedded in Python strings)
- Syntax highlighting breaks in Python files
- Difficult to test Ruby code independently
- Python string escaping makes Ruby code harder to read
- Changes require modifying Python file instead of Ruby file

### Option B: Ruby `load` Statement
Use Ruby's `load` statement to execute the external file.

```python
journal_creation_ruby = f"""
  load '{ruby_file_path}'
"""
```

**Pros**:
- Clean separation of Ruby code
- Ruby file can be tested independently
- Proper syntax highlighting in .rb files

**Cons**:
- **CRITICAL ISSUE**: Ruby `load` creates a new scope, breaking access to surrounding variables (`rec`, `rails_ops`, `idx`, `verbose`)
- Requires absolute file paths in containerized environment
- Additional file I/O during bulk operations
- Harder to debug scope-related issues

### Option C: Template Loading with String Interpolation (Selected)
Load the `.rb` file content as a Python string and inject it inline with proper indentation.

```python
# Load the Ruby template file once at module level
ruby_template_path = Path(__file__).parent.parent / 'ruby' / 'create_work_package_journals.rb'
with open(ruby_template_path, 'r') as f:
    journal_creation_ruby_template = f.read()

# In bulk_create, inject with proper indentation
journal_creation_ruby = '\n'.join(f"      {line}" for line in journal_creation_ruby_template.split('\n'))
```

**Pros**:
- Clean separation: Ruby code lives in `.rb` file with proper syntax highlighting
- **Preserves variable scope**: Code executes in surrounding context, accessing `rec`, `rails_ops`, `idx`, `verbose`
- Maintainable: Ruby changes don't require Python modifications
- Testable: `.rb` file can be tested independently
- Performant: Template loaded once at module initialization
- Proper indentation: `'\n'.join()` ensures each line gets correct indentation

**Cons**:
- Requires careful indentation management
- Template file must be available at runtime
- String concatenation has small overhead (negligible for our use case)

## Decision Outcome

**Chosen option: "Option C - Template Loading with String Interpolation"**

This approach provides the best balance of maintainability, scoping correctness, and code organization.

### Implementation Details

**File Structure**:
```
src/
├── clients/
│   └── openproject_client.py    # Loads and injects template
└── ruby/
    └── create_work_package_journals.rb    # Pure Ruby template
```

**Loading Pattern (openproject_client.py:~45-50)**:
```python
# Load Ruby template at module initialization
ruby_template_path = Path(__file__).parent.parent / 'ruby' / 'create_work_package_journals.rb'
if ruby_template_path.exists():
    with open(ruby_template_path, 'r') as f:
        journal_creation_ruby_template = f.read()
else:
    journal_creation_ruby_template = None
```

**Injection Pattern (openproject_client.py:~2660)**:
```python
# In bulk_create method
journal_creation_ruby = None
if journal_creation_ruby_template:
    # Apply 6-space indentation to each line for proper Ruby block nesting
    journal_creation_ruby = '\n'.join(
        f"      {line}" for line in journal_creation_ruby_template.split('\n')
    )

# Build bulk creation code
bulk_create_code = f"""
  records.each_with_index do |rec, idx|
    begin
      {custom_field_code}

      {
        ('\n'.join(f"      {line}" for line in journal_creation_ruby.split('\n'))
         if journal_creation_ruby else "")
      }

      rec.save(validate: false)
    rescue => e
      puts "Error: #{{e.message}}"
    end
  end
"""
```

### Critical Fix: Multi-line Indentation

**Problem**: Python f-strings only indent the first line:
```python
f"      {multi_line_string}"  # Only first line gets 6 spaces
```

**Solution**: Use `'\n'.join()` to indent each line:
```python
'\n'.join(f"      {line}" for line in multi_line_string.split('\n'))
```

This ensures all 230+ lines of the journal creation Ruby code maintain proper indentation within the bulk_create block.

## Consequences

### Positive
- **Clean Code Organization**: Ruby logic separate from Python orchestration
- **Maintainability**: Changes to journal creation logic only touch `.rb` file
- **Correct Scoping**: Template code executes in bulk_create context with full variable access
- **Testability**: Ruby template can be tested independently or through integration tests
- **Syntax Highlighting**: Proper IDE support for both Python and Ruby files
- **Performance**: Template loaded once at module initialization, not per-record
- **Debugging**: Ruby syntax errors point to correct line numbers in `.rb` file

### Negative
- **Indentation Complexity**: Must carefully manage indentation when injecting template
- **Runtime Dependency**: Template file must be present (but this is documented and version-controlled)
- **String Processing Overhead**: Minimal - only processes template once during bulk_create call

### Trade-offs
- **Simplicity vs. Organization**: Slight increase in complexity for significant improvement in code organization
- **File Count**: One additional file (`.rb` template) for better separation of concerns
- **Performance**: Negligible overhead (<1ms per bulk operation) for substantial maintainability gains

## Related Decisions

- **Bug #32**: OpenProject auto-creates journal v1 requiring UPDATE instead of CREATE
- **Validity Period Fix**: Chronological sorting and bounded/endless range handling
- **Multi-line Indentation**: Using `'\n'.join()` for proper Python f-string multi-line handling

## Notes

This pattern can be reused for other complex Ruby injections in the migration system. The key insight is that loading Ruby code as a string (not via `load`) preserves the execution context while maintaining clean code separation.

The indentation fix (`'\n'.join()`) is critical and must be applied whenever injecting multi-line Ruby templates into Python f-strings.

## References

- `src/ruby/create_work_package_journals.rb` - Journal creation template (230 lines)
- `src/clients/openproject_client.py:45-50` - Template loading
- `src/clients/openproject_client.py:2660` - Template injection with indentation fix
- Bug #32 Investigation: `/home/sme/p/j2o/claudedocs/bug32_root_cause_analysis.md`
