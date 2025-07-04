#!/usr/bin/env python3
"""Tests for the MarkdownConverter class."""

from src.utils.markdown_converter import MarkdownConverter


class TestMarkdownConverter:
    """Test cases for MarkdownConverter class."""

    def test_init_without_mappings(self):
        """Test initialization without providing mappings."""
        converter = MarkdownConverter()
        assert converter.user_mapping == {}
        assert converter.work_package_mapping == {}

    def test_init_with_mappings(self):
        """Test initialization with user and work package mappings."""
        user_mapping = {"jdoe": 123, "asmith": 456}
        wp_mapping = {"PROJ-1": 1, "PROJ-2": 2}

        converter = MarkdownConverter(user_mapping, wp_mapping)
        assert converter.user_mapping == user_mapping
        assert converter.work_package_mapping == wp_mapping

    def test_convert_empty_text(self):
        """Test conversion of empty or None text."""
        converter = MarkdownConverter()

        assert converter.convert("") == ""
        assert converter.convert(None) == ""
        assert converter.convert("   ") == ""

    def test_convert_basic_formatting(self):
        """Test basic text formatting conversion."""
        converter = MarkdownConverter()

        # Bold
        assert converter.convert("*bold text*") == "**bold text**"

        # Italic
        assert converter.convert("_italic text_") == "*italic text*"

        # Underline
        assert converter.convert("+underlined text+") == "<u>underlined text</u>"

        # Strikethrough
        assert converter.convert("-strikethrough text-") == "~~strikethrough text~~"

        # Monospace/inline code
        assert converter.convert("{{code text}}") == "`code text`"

    def test_convert_headings(self):
        """Test heading conversion."""
        converter = MarkdownConverter()

        assert converter.convert("h1. Heading 1") == "# Heading 1"
        assert converter.convert("h2. Heading 2") == "## Heading 2"
        assert converter.convert("h3. Heading 3") == "### Heading 3"
        assert converter.convert("h4. Heading 4") == "#### Heading 4"
        assert converter.convert("h5. Heading 5") == "##### Heading 5"
        assert converter.convert("h6. Heading 6") == "###### Heading 6"

    def test_convert_lists(self):
        """Test list conversion."""
        converter = MarkdownConverter()

        # Unordered lists
        jira_unordered = """* First item
* Second item
** Nested item
* Third item"""

        expected_unordered = """- First item
- Second item
  - Nested item
- Third item"""

        assert converter.convert(jira_unordered) == expected_unordered

        # Ordered lists
        jira_ordered = """# First item
# Second item
## Nested item
# Third item"""

        expected_ordered = """1. First item
1. Second item
  1. Nested item
1. Third item"""

        assert converter.convert(jira_ordered) == expected_ordered

    def test_convert_block_quotes(self):
        """Test block quote conversion."""
        converter = MarkdownConverter()

        jira_quote = "bq. This is a block quote"
        expected_quote = "> This is a block quote"

        assert converter.convert(jira_quote) == expected_quote

    def test_convert_code_blocks(self):
        """Test code block conversion."""
        converter = MarkdownConverter()

        # Code block with language
        jira_code = """{code:python}
def hello():
    print("Hello World")
{code}"""

        expected_code = """```python
def hello():
    print("Hello World")
```"""

        assert converter.convert(jira_code) == expected_code

        # Code block without language
        jira_code_no_lang = """{code}
console.log("Hello");
{code}"""

        expected_code_no_lang = """```
console.log("Hello");
```"""

        assert converter.convert(jira_code_no_lang) == expected_code_no_lang

        # Noformat block
        jira_noformat = """{noformat}
Plain text content
{noformat}"""

        expected_noformat = """```
Plain text content
```"""

        assert converter.convert(jira_noformat) == expected_noformat

    def test_convert_links(self):
        """Test link conversion."""
        converter = MarkdownConverter()

        # URL only
        assert converter.convert("[http://example.com]") == "[http://example.com](http://example.com)"

        # Title and URL
        assert converter.convert("[Example Site|http://example.com]") == "[Example Site](http://example.com)"

        # Title only (internal link)
        assert converter.convert("[Internal Page]") == "[Internal Page](Internal Page)"

    def test_convert_issue_references_with_mapping(self):
        """Test issue reference conversion with work package mapping."""
        wp_mapping = {"PROJ-123": 456, "TEST-789": 999}
        converter = MarkdownConverter(work_package_mapping=wp_mapping)

        text = "See PROJ-123 and TEST-789 for details"
        expected = "See #456 and #999 for details"

        assert converter.convert(text) == expected

    def test_convert_issue_references_without_mapping(self):
        """Test issue reference conversion without work package mapping."""
        converter = MarkdownConverter()

        text = "See PROJ-123 for details"
        expected = "See ~~PROJ-123~~ *(migrated issue)* for details"

        assert converter.convert(text) == expected

    def test_convert_user_mentions_with_mapping(self):
        """Test user mention conversion with user mapping."""
        user_mapping = {"john.doe": 123, "jane.smith": 456}
        converter = MarkdownConverter(user_mapping=user_mapping)

        text = "Hey [~john.doe] and [~jane.smith], please review this"
        expected = "Hey @123 and @456, please review this"

        assert converter.convert(text) == expected

    def test_convert_user_mentions_without_mapping(self):
        """Test user mention conversion without user mapping."""
        converter = MarkdownConverter()

        text = "Hey [~john.doe], please review this"
        expected = "Hey @john.doe, please review this"

        assert converter.convert(text) == expected

    def test_convert_horizontal_rules(self):
        """Test horizontal rule conversion."""
        converter = MarkdownConverter()

        assert converter.convert("----") == "---"
        assert converter.convert("----------") == "---"

    def test_convert_tables(self):
        """Test table conversion."""
        converter = MarkdownConverter()

        jira_table = """|Header 1|Header 2|Header 3|
|Cell 1|Cell 2|Cell 3|
|Cell 4|Cell 5|Cell 6|"""

        expected_table = """| Header 1 | Header 2 | Header 3 |
| --- | --- | --- |
| Cell 1 | Cell 2 | Cell 3 |
| Cell 4 | Cell 5 | Cell 6 |"""

        assert converter.convert(jira_table) == expected_table

    def test_convert_panels_and_macros(self):
        """Test panel and macro conversion."""
        converter = MarkdownConverter()

        # Info panel
        jira_info = """{info:title=Important Info}
This is important information.
{info}"""

        expected_info = """**ℹ️ Important Info**

This is important information."""

        assert converter.convert(jira_info) == expected_info

        # Warning panel
        jira_warning = """{warning}
This is a warning.
{warning}"""

        expected_warning = """**⚠️ Warning**

This is a warning."""

        assert converter.convert(jira_warning) == expected_warning

        # Note panel
        jira_note = """{note:title=Please Note}
This is a note.
{note}"""

        expected_note = """**📝 Please Note**

This is a note."""

        assert converter.convert(jira_note) == expected_note

        # Tip panel
        jira_tip = """{tip}
This is a helpful tip.
{tip}"""

        expected_tip = """**💡 Tip**

This is a helpful tip."""

        assert converter.convert(jira_tip) == expected_tip

    def test_convert_complex_document(self):
        """Test conversion of a complex document with multiple elements."""
        user_mapping = {"developer": 123}
        wp_mapping = {"PROJ-456": 789}
        converter = MarkdownConverter(user_mapping, wp_mapping)

        jira_complex = """h1. Project Overview

This project involves implementing *new features* and fixing issues like PROJ-456.

h2. Requirements

* _Functional requirements_:
** User authentication
** Data validation
* *Non-functional requirements*:
** Performance optimization
** Security enhancements

{info:title=Important}
Please review with [~developer] before proceeding.
{info}

h3. Code Example

{code:python}
def authenticate(username, password):
    return validate_credentials(username, password)
{code}

|Feature|Status|Assignee|
|Auth|Complete|[~developer]|
|Validation|In Progress|TBD|

bq. Remember to update documentation after implementation.

----

For questions, see PROJ-456 or contact [~developer]."""

        expected_complex = """# Project Overview

This project involves implementing **new features** and fixing issues like #789.

## Requirements
- *Functional requirements*:
  - User authentication
  - Data validation
- **Non-functional requirements**:
  - Performance optimization
  - Security enhancements

**ℹ️ Important**

Please review with @123 before proceeding.

### Code Example

```python
def authenticate(username, password):
    return validate_credentials(username, password)
```

| Feature | Status | Assignee |
| --- | --- | --- |
| Auth | Complete | @123 |
| Validation | In Progress | TBD |

> Remember to update documentation after implementation.

---

For questions, see #789 or contact @123."""

        assert converter.convert(jira_complex) == expected_complex

    def test_convert_with_context(self):
        """Test conversion with additional context."""
        converter = MarkdownConverter()

        context = {
            'user_mapping': {'newuser': 999},
            'work_package_mapping': {'NEW-123': 888}
        }

        text = "Contact [~newuser] about NEW-123"
        expected = "Contact @999 about #888"

        result = converter.convert_with_context(text, context)
        assert result == expected

    def test_cleanup_whitespace(self):
        """Test whitespace cleanup functionality."""
        converter = MarkdownConverter()

        text_with_excess = """Line 1


Line 2




Line 3
Line 4	"""

        expected_cleaned = """Line 1

Line 2

Line 3
Line 4"""

        assert converter.convert(text_with_excess) == expected_cleaned

    def test_nested_formatting(self):
        """Test handling of nested formatting elements."""
        converter = MarkdownConverter()

        # Bold and italic combined
        text = "*_bold italic_*"
        expected = "***bold italic***"
        assert converter.convert(text) == expected

        # Code within bold
        text = "*bold {{code}} text*"
        expected = "**bold `code` text**"
        assert converter.convert(text) == expected

    def test_convert_expand_macro(self):
        """Test conversion of Jira expand/collapsible sections."""
        converter = MarkdownConverter()

        # Basic expand
        jira_expand = "{expand:title=Click to expand}Hidden content here{expand}"
        expected = "<details>\n<summary>Click to expand</summary>\n\nHidden content here\n\n</details>"
        result = converter.convert(jira_expand)
        assert result == expected

        # Expand without title
        jira_expand_no_title = "{expand}Hidden content here{expand}"
        result = converter.convert(jira_expand_no_title)
        assert "<summary>Show/Hide Details</summary>" in result
        assert "Hidden content here" in result

    def test_convert_tabs_macro(self):
        """Test conversion of Jira tabs."""
        converter = MarkdownConverter()

        # Basic tabs
        jira_tabs = "{tabs}{tab:First Tab}Content 1{tab:Second Tab}Content 2{tabs}"
        result = converter.convert(jira_tabs)

        assert "**📑 First Tab**" in result
        assert "Content 1" in result
        assert "**📑 Second Tab** *(Alternative View)*" in result
        assert "Content 2" in result
        assert "---" in result  # Tab separator

        # Single tab
        jira_single_tab = "{tabs}{tab:Only Tab}Single content{tabs}"
        result = converter.convert(jira_single_tab)
        assert "**📑 Only Tab**" in result
        assert "Single content" in result

    def test_convert_color_macro(self):
        """Test conversion of Jira color formatting."""
        converter = MarkdownConverter()

        # Standard colors
        test_cases = [
            ("{color:red}Red text{color}", "🔴 Red text"),
            ("{color:green}Green text{color}", "🟢 Green text"),
            ("{color:blue}Blue text{color}", "🔵 Blue text"),
            ("{color:yellow}Yellow text{color}", "🟡 Yellow text"),
            ("{color:orange}Orange text{color}", "🟠 Orange text"),
            ("{color:purple}Purple text{color}", "🟣 Purple text"),
            ("{color:black}Black text{color}", "⚫ Black text"),
            ("{color:white}White text{color}", "⚪ White text"),
            ("{color:gray}Gray text{color}", "🔘 Gray text"),
            ("{color:grey}Grey text{color}", "🔘 Grey text"),
        ]

        for jira_input, expected in test_cases:
            result = converter.convert(jira_input)
            assert result == expected

        # Custom color (fallback)
        jira_custom = "{color:#FF5733}Custom color{color}"
        result = converter.convert(jira_custom)
        assert result == "(#FF5733) Custom color"

    def test_advanced_macros_combined(self):
        """Test multiple advanced macros together."""
        converter = MarkdownConverter()

        jira_combined = """
{expand:title=Advanced Example}
{color:red}Important:{color} This is critical information.

{tabs}
{tab:Option A}
Content for option A with *bold* text.
{tab:Option B}
Content for option B with _italic_ text.
{tabs}
{expand}"""

        result = converter.convert(jira_combined)

        # Check expand
        assert "<details>" in result
        assert "<summary>Advanced Example</summary>" in result
        assert "</details>" in result

        # Check color
        assert "🔴 Important:" in result

        # Check tabs
        assert "**📑 Option A**" in result
        assert "**📑 Option B** *(Alternative View)*" in result

        # Check text formatting within tabs
        assert "**bold**" in result
        assert "*italic*" in result

    def test_edge_cases(self):
        """Test edge cases and malformed markup."""
        converter = MarkdownConverter()

        # Unclosed formatting (patterns require closing markers)
        assert converter.convert("*unclosed bold") == "*unclosed bold"

        # Empty table cells
        jira_table = "|Header||Empty|"
        result = converter.convert(jira_table)
        assert "Header" in result and "Empty" in result

        # Malformed headings
        assert converter.convert("h7. Invalid heading") == "h7. Invalid heading"

        # Mixed line endings
        text_mixed = "Line 1\r\nLine 2\nLine 3\r"
        result = converter.convert(text_mixed)
        assert "Line 1" in result and "Line 2" in result and "Line 3" in result
