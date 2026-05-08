#!/usr/bin/env python3
"""Tests for the MarkdownConverter class."""

from src.utils.markdown_converter import MarkdownConverter


class TestMarkdownConverter:
    """Test cases for MarkdownConverter class."""

    def test_init_without_mappings(self) -> None:
        """Test initialization without providing mappings."""
        converter = MarkdownConverter()
        assert converter.user_mapping == {}
        assert converter.work_package_mapping == {}

    def test_init_with_mappings(self) -> None:
        """Test initialization with user and work package mappings."""
        user_mapping = {"jdoe": 123, "asmith": 456}
        wp_mapping = {"PROJ-1": 1, "PROJ-2": 2}

        converter = MarkdownConverter(user_mapping, wp_mapping)
        assert converter.user_mapping == user_mapping
        assert converter.work_package_mapping == wp_mapping

    def test_convert_empty_text(self) -> None:
        """Test conversion of empty or None text."""
        converter = MarkdownConverter()

        assert converter.convert("") == ""
        assert converter.convert(None) == ""
        assert converter.convert("   ") == ""

    def test_convert_basic_formatting(self) -> None:
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

    def test_convert_headings(self) -> None:
        """Test heading conversion."""
        converter = MarkdownConverter()

        assert converter.convert("h1. Heading 1") == "# Heading 1"
        assert converter.convert("h2. Heading 2") == "## Heading 2"
        assert converter.convert("h3. Heading 3") == "### Heading 3"
        assert converter.convert("h4. Heading 4") == "#### Heading 4"
        assert converter.convert("h5. Heading 5") == "##### Heading 5"
        assert converter.convert("h6. Heading 6") == "###### Heading 6"

    def test_convert_lists(self) -> None:
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

    def test_convert_block_quotes(self) -> None:
        """Test block quote conversion."""
        converter = MarkdownConverter()

        jira_quote = "bq. This is a block quote"
        expected_quote = "> This is a block quote"

        assert converter.convert(jira_quote) == expected_quote

    def test_convert_code_blocks(self) -> None:
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

    def test_convert_links(self) -> None:
        """Test link conversion."""
        converter = MarkdownConverter()

        # URL only
        assert converter.convert("[http://example.com]") == "[http://example.com](http://example.com)"

        # Title and URL
        assert converter.convert("[Example Site|http://example.com]") == "[Example Site](http://example.com)"

        # Title only (internal link)
        assert converter.convert("[Internal Page]") == "[Internal Page](Internal Page)"

    def test_convert_issue_references_with_mapping(self) -> None:
        """Test issue reference conversion with work package mapping."""
        wp_mapping = {"PROJ-123": 456, "TEST-789": 999}
        converter = MarkdownConverter(work_package_mapping=wp_mapping)

        text = "See PROJ-123 and TEST-789 for details"
        expected = "See #456 and #999 for details"

        assert converter.convert(text) == expected

    def test_convert_issue_references_without_mapping(self) -> None:
        """Test issue reference conversion without work package mapping."""
        converter = MarkdownConverter()

        text = "See PROJ-123 for details"
        expected = "See ~~PROJ-123~~ *(migrated issue)* for details"

        assert converter.convert(text) == expected

    def test_convert_user_mentions_with_mapping(self) -> None:
        """Test user mention conversion with user mapping."""
        user_mapping = {"john.doe": 123, "jane.smith": 456}
        converter = MarkdownConverter(user_mapping=user_mapping)

        text = "Hey [~john.doe] and [~jane.smith], please review this"
        expected = "Hey @123 and @456, please review this"

        assert converter.convert(text) == expected

    def test_convert_user_mentions_without_mapping(self) -> None:
        """Test user mention conversion without user mapping."""
        converter = MarkdownConverter()
        text = "Hey [~john.doe], please review this"
        result = converter.convert(text)
        # Should preserve original format when no mapping exists
        expected = "Hey [~john.doe], please review this"
        assert result == expected

    def test_convert_horizontal_rules(self) -> None:
        """Test horizontal rule conversion."""
        converter = MarkdownConverter()

        assert converter.convert("----") == "---"
        assert converter.convert("----------") == "---"

    def test_convert_tables(self) -> None:
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

    def test_convert_panels_and_macros(self) -> None:
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

    def test_convert_complex_document(self) -> None:
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

    def test_convert_with_context(self) -> None:
        """Test conversion with additional context."""
        converter = MarkdownConverter()

        context = {
            "user_mapping": {"newuser": 999},
            "work_package_mapping": {"NEW-123": 888},
        }

        text = "Contact [~newuser] about NEW-123"
        expected = "Contact @999 about #888"

        result = converter.convert_with_context(text, context)
        assert result == expected

    def test_cleanup_whitespace(self) -> None:
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

    def test_nested_formatting(self) -> None:
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

    def test_convert_expand_macro(self) -> None:
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

    def test_convert_tabs_macro(self) -> None:
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

    def test_convert_color_macro(self) -> None:
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

    def test_advanced_macros_combined(self) -> None:
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

    def test_edge_cases(self) -> None:
        """Test edge cases and malformed markup."""
        converter = MarkdownConverter()

        # Unclosed formatting (patterns require closing markers)
        assert converter.convert("*unclosed bold") == "*unclosed bold"

        # Empty table cells
        jira_table = "|Header||Empty|"
        result = converter.convert(jira_table)
        assert "Header" in result
        assert "Empty" in result

        # Malformed headings
        assert converter.convert("h7. Invalid heading") == "h7. Invalid heading"

        # Mixed line endings
        text_mixed = "Line 1\r\nLine 2\nLine 3\r"
        result = converter.convert(text_mixed)
        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3" in result

    def test_convert_images(self) -> None:
        """Test image conversion from Jira to markdown format."""
        converter = MarkdownConverter()

        # Test basic image (uses filename as alt text for accessibility)
        text = "Here is an image: !screenshot.png!"
        result = converter.convert(text)
        expected = "Here is an image: ![screenshot.png](screenshot.png)"
        assert result == expected

        # Test image with alt text
        text = "Check this: !diagram.jpg|This is a diagram!"
        result = converter.convert(text)
        expected = "Check this: ![This is a diagram](diagram.jpg)"
        assert result == expected

        # Test multiple images (uses filename as alt text when not specified)
        text = "See !before.png! and !after.png|After image!"
        result = converter.convert(text)
        expected = "See ![before.png](before.png) and ![After image](after.png)"
        assert result == expected

    def test_convert_attachments(self) -> None:
        """Test attachment conversion from Jira to markdown format."""
        converter = MarkdownConverter()

        # Test PDF attachment
        text = "Please review [Technical Spec|spec.pdf]"
        result = converter.convert(text)
        expected = "Please review [Technical Spec](spec.pdf)"
        assert result == expected

        # Test various file types
        text = "Files: [Report|report.docx] and [Data|data.xlsx]"
        result = converter.convert(text)
        expected = "Files: [Report](report.docx) and [Data](data.xlsx)"
        assert result == expected

        # Test zip file
        text = "Download [Source Code|source.zip]"
        result = converter.convert(text)
        expected = "Download [Source Code](source.zip)"
        assert result == expected

    def test_init_with_attachment_mapping(self) -> None:
        """Test initialization with attachment mapping."""
        attachment_mapping = {
            "PROJ-1": {"screenshot.png": 100, "diagram.jpg": 101},
            "PROJ-2": {"report.pdf": 200},
        }

        converter = MarkdownConverter(attachment_mapping=attachment_mapping)
        assert converter.attachment_mapping == attachment_mapping

    def test_convert_images_with_attachment_mapping(self) -> None:
        """Test image conversion using OpenProject attachment URLs."""
        attachment_mapping = {
            "PROJ-1": {"screenshot.png": 100, "diagram.jpg": 101},
        }
        converter = MarkdownConverter(attachment_mapping=attachment_mapping)

        # Test image with attachment mapping - should use OpenProject URL
        text = "Here is an image: !screenshot.png!"
        result = converter.convert(text, jira_key="PROJ-1")
        expected = "Here is an image: ![screenshot.png](/api/v3/attachments/100/content)"
        assert result == expected

        # Test image with alt text and attachment mapping
        text = "Check this: !diagram.jpg|Flow diagram!"
        result = converter.convert(text, jira_key="PROJ-1")
        expected = "Check this: ![Flow diagram](/api/v3/attachments/101/content)"
        assert result == expected

    def test_convert_images_case_insensitive_lookup(self) -> None:
        """Test that image attachment lookup is case-insensitive."""
        attachment_mapping = {
            "PROJ-1": {"Screenshot.PNG": 100},  # Note: mixed case
        }
        converter = MarkdownConverter(attachment_mapping=attachment_mapping)

        # Test with different case - should still find the mapping
        text = "!screenshot.png!"
        result = converter.convert(text, jira_key="PROJ-1")
        expected = "![screenshot.png](/api/v3/attachments/100/content)"
        assert result == expected

    def test_convert_images_without_jira_key(self) -> None:
        """Test image conversion without jira_key falls back to filename."""
        attachment_mapping = {
            "PROJ-1": {"screenshot.png": 100},
        }
        converter = MarkdownConverter(attachment_mapping=attachment_mapping)

        # Without jira_key, should use filename fallback
        text = "!screenshot.png!"
        result = converter.convert(text)  # No jira_key
        expected = "![screenshot.png](screenshot.png)"
        assert result == expected

    def test_convert_images_missing_attachment_mapping(self) -> None:
        """Test image conversion when attachment not in mapping."""
        attachment_mapping = {
            "PROJ-1": {"other.png": 100},  # Different file
        }
        converter = MarkdownConverter(attachment_mapping=attachment_mapping)

        # Attachment not in mapping - should use filename fallback
        text = "!missing.png!"
        result = converter.convert(text, jira_key="PROJ-1")
        expected = "![missing.png](missing.png)"
        assert result == expected

    def test_convert_images_different_jira_key(self) -> None:
        """Test image conversion with different jira_key has separate mappings."""
        attachment_mapping = {
            "PROJ-1": {"screenshot.png": 100},
            "PROJ-2": {"screenshot.png": 200},  # Same filename, different ID
        }
        converter = MarkdownConverter(attachment_mapping=attachment_mapping)

        # PROJ-1 should get ID 100
        result1 = converter.convert("!screenshot.png!", jira_key="PROJ-1")
        assert "/api/v3/attachments/100/content" in result1

        # PROJ-2 should get ID 200
        result2 = converter.convert("!screenshot.png!", jira_key="PROJ-2")
        assert "/api/v3/attachments/200/content" in result2

    def test_convert_attachment_links_with_mapping(self) -> None:
        """Test attachment link conversion using OpenProject URLs."""
        attachment_mapping = {
            "PROJ-1": {"report.pdf": 150, "data.xlsx": 151},
        }
        converter = MarkdownConverter(attachment_mapping=attachment_mapping)

        # Test PDF attachment link with mapping
        text = "Please review [^report.pdf]"
        result = converter.convert(text, jira_key="PROJ-1")
        expected = "Please review [report.pdf](/api/v3/attachments/150/content)"
        assert result == expected

        # Test Excel attachment link with mapping
        text = "Data is in [^data.xlsx]"
        result = converter.convert(text, jira_key="PROJ-1")
        expected = "Data is in [data.xlsx](/api/v3/attachments/151/content)"
        assert result == expected

    def test_convert_multiple_images_in_same_text(self) -> None:
        """Test multiple images in same text are all converted."""
        attachment_mapping = {
            "PROJ-1": {"before.png": 100, "after.png": 101, "diff.png": 102},
        }
        converter = MarkdownConverter(attachment_mapping=attachment_mapping)

        text = "Compare !before.png! with !after.png! showing !diff.png|The difference!"
        result = converter.convert(text, jira_key="PROJ-1")

        assert "/api/v3/attachments/100/content" in result
        assert "/api/v3/attachments/101/content" in result
        assert "/api/v3/attachments/102/content" in result
        assert "![The difference]" in result

    def test_convert_mixed_content_with_attachment_mapping(self) -> None:
        """Test complex content with images, links, and other markup."""
        user_mapping = {"developer": 123}
        wp_mapping = {"PROJ-456": 789}
        attachment_mapping = {
            "PROJ-123": {"screenshot.png": 500, "doc.pdf": 501},
        }
        converter = MarkdownConverter(
            user_mapping=user_mapping,
            work_package_mapping=wp_mapping,
            attachment_mapping=attachment_mapping,
        )

        text = """h2. Issue Report

See !screenshot.png! for the error.

*Steps to reproduce:*
# Open the app
# Click button

Documented in [^doc.pdf]. Also see PROJ-456.

Contact [~developer] for help."""

        result = converter.convert(text, jira_key="PROJ-123")

        # Check heading
        assert "## Issue Report" in result

        # Check image with OpenProject URL
        assert "![screenshot.png](/api/v3/attachments/500/content)" in result

        # Check attachment link with OpenProject URL
        assert "[doc.pdf](/api/v3/attachments/501/content)" in result

        # Check work package reference
        assert "#789" in result

        # Check user mention
        assert "@123" in result


class TestStrikethroughFalsePositives:
    r"""Regression tests for strikethrough false positives (real-world NRS-4391 data).

    Bug: the old pattern (?<![|\s])-([^-\n|]+)-(?![|\s-]) greedily spans
    across spaces and the ~~ in issue-ref fallback text, corrupting compound
    words, dates, CLI flags, and multi-word phrases.
    """

    def test_compound_word_not_struck_two_segments(self) -> None:
        """ansible-core must not become ansible~~core~~."""
        converter = MarkdownConverter()
        assert converter.convert("ansible-core 2.20") == "ansible-core 2.20"

    def test_compound_word_not_struck_multi_segment(self) -> None:
        """ansible-role-concourse-ci must not be partially struck through."""
        converter = MarkdownConverter()
        result = converter.convert("ansible-role-concourse-ci")
        assert "~~" not in result
        assert result == "ansible-role-concourse-ci"

    def test_compound_word_non_trivial_fix(self) -> None:
        """non-trivial-fix must not become non~~trivial~~fix."""
        converter = MarkdownConverter()
        result = converter.convert("non-trivial-fix")
        assert "~~" not in result

    def test_compound_word_pre_existing(self) -> None:
        """pre-existing must not become pre~~existing~~ (negative case)."""
        converter = MarkdownConverter()
        assert converter.convert("pre-existing") == "pre-existing"

    def test_date_not_struck(self) -> None:
        """2023-12-31 must not produce 2023~~12~~31."""
        converter = MarkdownConverter()
        result = converter.convert("2023-12-31")
        assert "~~" not in result
        assert result == "2023-12-31"

    def test_cli_flags_not_struck(self) -> None:
        """ansible-playbook --check --diff -f 30 must not be struck through."""
        converter = MarkdownConverter()
        result = converter.convert("ansible-playbook --check --diff -f 30")
        assert "~~" not in result

    def test_issue_ref_fallback_plus_compound_word_not_struck(self) -> None:
        """Regression: issue-ref fallback output followed by compound word.

        NRS-4388 -> '~~NRS-4388~~ *(migrated issue)*' then text_formatting
        used to match '-4388~~ ...ansible-' as a single greedy span,
        producing '~~NRS~~4388~~ ...' and 'ansible~~core~~'.
        """
        converter = MarkdownConverter()
        # No WP mapping so issue ref becomes fallback ~~KEY~~ *(migrated issue)*
        result = converter.convert("NRS-4388 bumped the toolchain to community Ansible 13 (ansible-core 2.20).")
        # Must preserve the ~~NRS-4388~~ from the issue-ref fallback
        assert "~~NRS-4388~~ *(migrated issue)*" in result
        # Must NOT add extra ~~ inside the key
        assert "~~NRS~~4388~~" not in result
        # ansible-core compound word must not be struck
        assert "ansible~~core~~" not in result
        assert "ansible-core" in result

    def test_legitimate_strikethrough_with_spaces(self) -> None:
        """-strikethrough text- (space-bounded dashes) SHOULD produce ~~strikethrough text~~."""
        converter = MarkdownConverter()
        result = converter.convert("-strikethrough text-")
        assert result == "~~strikethrough text~~"

    def test_legitimate_inline_strikethrough(self) -> None:
        """Text -strike this- done SHOULD produce text ~~strike this~~ done."""
        converter = MarkdownConverter()
        result = converter.convert("text -strike this- done")
        assert result == "text ~~strike this~~ done"

    def test_url_hyphens_not_struck(self) -> None:
        """Hyphens inside a URL path must not be struck through."""
        converter = MarkdownConverter()
        url = "https://git.example.de/provision/ansible-role-concourse-ci/-/merge_requests/28"
        result = converter.convert(f"[Link|{url}]")
        # The link should remain intact; no ~~ injected
        assert "~~" not in result


class TestTableHeaderDoubleColumnBug:
    """Regression tests for Jira || header rows producing doubled columns.

    Bug: ||col1||col2|| is parsed by splitting on '|' giving empty cells
    between each header cell, so a 4-column table becomes 7 columns.
    """

    def test_header_row_correct_column_count(self) -> None:
        """||role||file:line||type||fix pattern|| must produce exactly 4 columns (no empty slots)."""
        converter = MarkdownConverter()
        jira_table = "||role||file:line||type||fix pattern||\n|concourse-ci|defaults/main.yml:5|Jinja|test|"
        result = converter.convert(jira_table)
        lines = [l for l in result.split("\n") if l.strip()]
        # Header row: strip leading/trailing | then split on | to get all cells (including empty)
        header_line = lines[0]
        all_cells = [c.strip() for c in header_line.strip("|").split("|")]
        assert all_cells == ["role", "file:line", "type", "fix pattern"], (
            f"Expected exactly 4 columns with no empty slots, got: {all_cells}"
        )

    def test_header_row_no_empty_cells(self) -> None:
        """No empty cells (|  |) should appear in the converted header."""
        converter = MarkdownConverter()
        jira_table = "||Name||Status||Priority||\n|Task A|Open|High|"
        result = converter.convert(jira_table)
        header_line = result.split("\n")[0]
        # The header line should not contain '|  |' (empty cell markers)
        assert "|  |" not in header_line, f"Empty cells found in header: {header_line!r}"

    def test_data_rows_match_header_column_count(self) -> None:
        """Data rows must have the same column count as the header row."""
        converter = MarkdownConverter()
        jira_table = "||A||B||C||\n|1|2|3|"
        result = converter.convert(jira_table)
        lines = [l.strip() for l in result.split("\n") if l.strip() and not l.startswith("| ---")]
        assert len(lines) >= 2  # at least header + 1 data row
        header_cols = len([c for c in lines[0].split("|") if c.strip()])
        data_cols = len([c for c in lines[1].split("|") if c.strip()])
        assert header_cols == data_cols, f"Header has {header_cols} cols but data row has {data_cols}"

    def test_header_without_trailing_pipes(self) -> None:
        """||col1||col2||col3 (no trailing ||) should also work correctly."""
        converter = MarkdownConverter()
        jira_table = "||role||file:line||type\n|docker_swarm|tasks/ufw.yml|INJECT_FACTS|"
        result = converter.convert(jira_table)
        lines = [l for l in result.split("\n") if l.strip()]
        header_line = lines[0]
        cols = [c.strip() for c in header_line.split("|") if c.strip()]
        assert cols == ["role", "file:line", "type"], f"Expected 3 header columns, got: {cols}"

    def test_real_nrs4391_table_header(self) -> None:
        """Regression test using the exact table from NRS-4391 description."""
        converter = MarkdownConverter()
        jira_table = (
            "||role||file:line||type||fix pattern||\n"
            "|concourse-ci|defaults/main.yml:5|Jinja embedded template|test pattern|"
        )
        result = converter.convert(jira_table)
        lines = [l.strip() for l in result.split("\n") if l.strip()]
        # First line must be the header with correct columns
        header_line = lines[0]
        cols = [c.strip() for c in header_line.split("|") if c.strip()]
        assert len(cols) == 4, f"Expected 4 columns, got {len(cols)}: {cols}"
        assert "role" in cols
        assert "file:line" in cols
        assert "type" in cols
        assert "fix pattern" in cols


class TestAttachmentExtensionWhitelist:
    """Regression tests for attachment extension whitelist being too narrow.

    Bug: only pdf|doc|docx|xls|xlsx|ppt|pptx|txt|zip|rar|7z extensions matched,
    so [^maintenance.log] and similar references were silently left unconverted.
    All real NRS-4391 attachments are .log files — none were converted.
    """

    def test_log_attachment_ref_pattern(self) -> None:
        """[^file.log] must be converted to a markdown link."""
        converter = MarkdownConverter()
        result = converter.convert("[^maintenance-NRS-4391-20260506-111231.log]")
        # Should become a markdown link, not remain as raw [^...] Jira syntax
        assert result.startswith("[")
        assert "[^" not in result, f"Raw Jira attachment syntax not converted: {result!r}"
        assert "maintenance-NRS-4391-20260506-111231.log" in result

    def test_log_attachment_with_mapping(self) -> None:
        """[^file.log] with attachment mapping must resolve to OP URL."""
        attachment_mapping = {
            "NRS-4391": {"maintenance-NRS-4391-20260506-111231.log": 116987},
        }
        converter = MarkdownConverter(attachment_mapping=attachment_mapping)
        result = converter.convert(
            "[^maintenance-NRS-4391-20260506-111231.log]",
            jira_key="NRS-4391",
        )
        assert "[maintenance-NRS-4391-20260506-111231.log](/api/v3/attachments/116987/content)" in result

    def test_csv_attachment_ref(self) -> None:
        """[^export.csv] must be converted (CSV not in old whitelist)."""
        converter = MarkdownConverter()
        result = converter.convert("[^export.csv]")
        assert "[^" not in result

    def test_msg_attachment_ref(self) -> None:
        """[^email.msg] must be converted."""
        converter = MarkdownConverter()
        result = converter.convert("[^email.msg]")
        assert "[^" not in result

    def test_eml_attachment_ref(self) -> None:
        """[^message.eml] must be converted."""
        converter = MarkdownConverter()
        result = converter.convert("[^message.eml]")
        assert "[^" not in result

    def test_named_csv_attachment(self) -> None:
        """[Export Data|data.csv] must be converted."""
        converter = MarkdownConverter()
        result = converter.convert("[Export Data|data.csv]")
        assert result == "[Export Data](data.csv)"

    def test_named_log_attachment(self) -> None:
        """[Check Log|run.log] must be converted."""
        converter = MarkdownConverter()
        result = converter.convert("[Check Log|run.log]")
        assert result == "[Check Log](run.log)"

    def test_url_link_not_matched_as_attachment(self) -> None:
        """[Google|https://google.com] must NOT be treated as an attachment."""
        converter = MarkdownConverter()
        result = converter.convert("[Google|https://google.com]")
        # Should produce a markdown link preserving the full URL, not a bare filename
        assert "[Google](https://google.com)" in result
        assert "[^" not in result

    def test_http_url_link_not_matched_as_attachment(self) -> None:
        """[Example|http://example.com] must NOT be treated as an attachment."""
        converter = MarkdownConverter()
        result = converter.convert("[Example|http://example.com]")
        assert "[Example](http://example.com)" in result


class TestBoldWithParentheses:
    r"""Regression tests for bold text containing parentheses.

    Bug: bold_pattern excluded ( and ) from content via [^*\n()]+,
    so *important (note)* was not bolded. Real Jira content routinely
    uses parenthetical notes inside bold spans.
    """

    def test_bold_with_parentheses(self) -> None:
        """*important (note)* must convert to **important (note)**."""
        converter = MarkdownConverter()
        assert converter.convert("*important (note)*") == "**important (note)**"

    def test_bold_with_parens_complex(self) -> None:
        """*defaults/main.yml:5 (see task)* must convert correctly."""
        converter = MarkdownConverter()
        result = converter.convert("*defaults/main.yml:5 (see task)*")
        assert result == "**defaults/main.yml:5 (see task)**"

    def test_bold_without_parens_still_works(self) -> None:
        """Plain *bold text* must still convert to **bold text**."""
        converter = MarkdownConverter()
        assert converter.convert("*bold text*") == "**bold text**"

    def test_markdown_double_star_not_affected(self) -> None:
        """**already bold** must not get extra stars."""
        converter = MarkdownConverter()
        assert converter.convert("**already bold**") == "**already bold**"

    def test_bold_with_parens_in_sentence(self) -> None:
        """Sentence containing *bold (phrase)* converts only that span."""
        converter = MarkdownConverter()
        result = converter.convert("The *important (note)* here.")
        assert "**important (note)**" in result


class TestBoldWithSymbols:
    r"""Regression tests: bold spans led by punctuation/symbols (#7).

    Bold pattern previously required a word character (\w) as the first char
    of the bold span, blocking valid Jira bold like *!important!* or *"quoted"*.
    The fix widens the leading-char constraint to exclude only whitespace, ( and *.
    """

    def test_bold_with_hash_lead(self) -> None:
        """*#important* must convert to **#important**."""
        converter = MarkdownConverter()
        assert converter.convert("*#important*") == "**#important**"

    def test_bold_with_quoted_lead(self) -> None:
        r"""*\"quoted\"* must convert to **"quoted"**."""
        converter = MarkdownConverter()
        assert converter.convert('*"quoted"*') == '**"quoted"**'

    def test_bold_parenthetical_still_excluded(self) -> None:
        """*(parenthetical)* must NOT be bolded (leading ( stays excluded)."""
        converter = MarkdownConverter()
        result = converter.convert("*(parenthetical)*")
        assert "**" not in result

    def test_bold_space_lead_excluded(self) -> None:
        """* leading space* must NOT be bolded (leading space stays excluded)."""
        converter = MarkdownConverter()
        result = converter.convert("* leading space*")
        # Should be treated as an unordered list item, not bold
        assert result != "** leading space**"

    def test_bold_word_lead_still_works(self) -> None:
        """*word* must still convert to **word**."""
        converter = MarkdownConverter()
        assert converter.convert("*word*") == "**word**"


class TestAttachmentSpacesAndCase:
    """Regression tests: spaces in filenames and case-insensitive URL exclusion (#6/#8/#9).

    attachment_pattern and attachment_ref_pattern previously excluded spaces in
    filenames and lacked re.IGNORECASE, breaking real-world filenames like
    "some doc.pdf" and failing to exclude "HTTPS://example.com/file.pdf".
    """

    def test_attachment_with_space_in_filename(self) -> None:
        """[link|some doc.pdf] must be converted as an attachment, not a link."""
        converter = MarkdownConverter()
        result = converter.convert("[link|some doc.pdf]")
        assert result == "[link](some doc.pdf)"

    def test_attachment_with_space_and_mapping(self) -> None:
        """[link|some doc.pdf] with attachment mapping must resolve to OP URL."""
        attachment_mapping = {"PROJ-1": {"some doc.pdf": 42}}
        converter = MarkdownConverter(attachment_mapping=attachment_mapping)
        result = converter.convert("[link|some doc.pdf]", jira_key="PROJ-1")
        assert result == "[link](/api/v3/attachments/42/content)"

    def test_attachment_ref_with_space_in_filename(self) -> None:
        """[^another file.csv] must be converted as an attachment reference."""
        converter = MarkdownConverter()
        result = converter.convert("[^another file.csv]")
        assert "[^" not in result
        assert "another file.csv" in result

    def test_attachment_ref_with_space_and_mapping(self) -> None:
        """[^another file.csv] with attachment mapping must resolve to OP URL."""
        attachment_mapping = {"PROJ-2": {"another file.csv": 99}}
        converter = MarkdownConverter(attachment_mapping=attachment_mapping)
        result = converter.convert("[^another file.csv]", jira_key="PROJ-2")
        assert result == "[another file.csv](/api/v3/attachments/99/content)"

    def test_uppercase_https_url_not_treated_as_attachment(self) -> None:
        """[link|HTTPS://example.com/file.pdf] must NOT be converted as an attachment."""
        converter = MarkdownConverter()
        result = converter.convert("[link|HTTPS://example.com/file.pdf]")
        # Must preserve the URL, not treat it as a local filename
        assert "HTTPS://example.com/file.pdf" in result or "https://example.com/file.pdf" in result.lower()
        # Must not strip the URL down to just a filename fragment
        assert result != "[link](file.pdf)"

    def test_uppercase_ftp_url_not_treated_as_attachment(self) -> None:
        """[link|FTP://files.example.com/archive.zip] must NOT be treated as an attachment."""
        converter = MarkdownConverter()
        result = converter.convert("[link|FTP://files.example.com/archive.zip]")
        assert result != "[link](archive.zip)"


class TestTableEmptyHeaderCells:
    """Regression tests: empty cells in Jira table headers (#10).

    The header cell parser previously discarded empty cells via
    `if c.strip() != ""`, causing misalignment when a header row
    intentionally contained empty columns like ||col1|| ||col3||.
    """

    def test_table_header_with_empty_cell(self) -> None:
        """||col1|| ||col3|| must produce a header with 3 cells, not 2."""
        converter = MarkdownConverter()
        result = converter.convert("||col1|| ||col3||")
        lines = [ln for ln in result.split("\n") if ln.strip()]
        # The first non-empty line is the markdown header row
        header_line = lines[0]
        # Split on | and count non-separator cells
        parts = [p for p in header_line.split("|") if p != ""]
        # Should have 3 cells: "col1", " " (or ""), "col3"
        assert len(parts) == 3, f"Expected 3 cells, got {len(parts)}: {parts!r}"

    def test_table_header_all_populated_unchanged(self) -> None:
        """||col1||col2||col3|| with all cells populated must still produce 3 cells."""
        converter = MarkdownConverter()
        result = converter.convert("||col1||col2||col3||")
        lines = [ln for ln in result.split("\n") if ln.strip()]
        header_line = lines[0]
        parts = [p.strip() for p in header_line.split("|") if p.strip()]
        assert len(parts) == 3


class TestStrikethroughCLIFlagFalsePositives:
    r"""Regression tests for strikethrough false positives on CLI flag patterns.

    Bug: the PR #242 pattern (?<![\w\-])-([^-\n|]+)-(?!\w) fixed compound words
    but still matched CLI flag sequences like '--diff -f 30 --skip-tags' because
    the closing lookahead only excluded \w (word chars), not another dash.
    This allowed '-f 30 -' to match with the standalone '-' before 'f' as the
    opening dash and the '-' of '--skip' as the closing dash.

    Live evidence (NRS-4391):
      INPUT:  ansible-playbook site.yml --diff -f 30 --skip-tags acme-setup
      OUTPUT: ansible-playbook site.yml --diff ~~f 30 ~~-skip-tags acme-setup

    Fix: extend the closing lookahead to also exclude '-', and add whitespace
    constraints around the inner span so that ' -text -' (space before closing
    dash) is not treated as strikethrough.
    """

    # --- Negative regressions: PR #242's existing compound-word fixes must hold ---

    def test_compound_word_concourse_ci_not_struck(self) -> None:
        """concourse-ci must not produce ~~."""
        converter = MarkdownConverter()
        result = converter.convert("concourse-ci")
        assert "~~" not in result
        assert result == "concourse-ci"

    def test_compound_word_non_trivial_fix_not_struck(self) -> None:
        """non-trivial-fix must not produce ~~."""
        converter = MarkdownConverter()
        result = converter.convert("non-trivial-fix")
        assert "~~" not in result
        assert result == "non-trivial-fix"

    def test_date_not_struck(self) -> None:
        """2023-12-31 must not produce ~~."""
        converter = MarkdownConverter()
        result = converter.convert("2023-12-31")
        assert "~~" not in result
        assert result == "2023-12-31"

    def test_multi_segment_compound_word_not_struck(self) -> None:
        """multi-line-string must not produce ~~."""
        converter = MarkdownConverter()
        result = converter.convert("multi-line-string")
        assert "~~" not in result
        assert result == "multi-line-string"

    # --- NEW negatives: CLI flag patterns (the live-verified bug) ---

    def test_double_dash_flags_not_struck(self) -> None:
        """--diff -f 30 --skip-tags must not produce any ~~."""
        converter = MarkdownConverter()
        result = converter.convert("--diff -f 30 --skip-tags")
        assert "~~" not in result

    def test_single_char_flags_sequence_not_struck(self) -> None:
        """Cmd -x -y -z must not produce any ~~."""
        converter = MarkdownConverter()
        result = converter.convert("cmd -x -y -z")
        assert "~~" not in result

    def test_flag_with_value_not_struck(self) -> None:
        """--flag -value must not produce any ~~."""
        converter = MarkdownConverter()
        result = converter.convert("--flag -value")
        assert "~~" not in result

    def test_nrs4391_full_ansible_command_not_struck(self) -> None:
        """Exact NRS-4391 live input must not introduce any ~~ and must be preserved verbatim."""
        converter = MarkdownConverter()
        jira_input = "ansible-playbook site.yml --diff -f 30 --skip-tags acme-setup"
        result = converter.convert(jira_input)
        assert "~~" not in result, f"Unexpected strikethrough in: {result!r}"
        assert result == jira_input, f"Input was mutated: {result!r}"

    # --- Positive regressions: legitimate Jira strikethrough must still work ---

    def test_legitimate_standalone_strikethrough(self) -> None:
        """-strikethrough text- must produce ~~strikethrough text~~."""
        converter = MarkdownConverter()
        result = converter.convert("-strikethrough text-")
        assert result == "~~strikethrough text~~"

    def test_legitimate_inline_strikethrough(self) -> None:
        """Here is -gone text- now must produce here is ~~gone text~~ now."""
        converter = MarkdownConverter()
        result = converter.convert("here is -gone text- now")
        assert result == "here is ~~gone text~~ now"

    def test_legitimate_parenthetical_strikethrough(self) -> None:
        """Parenthetical span: (this -is also- removed) must produce (this ~~is also~~ removed)."""
        converter = MarkdownConverter()
        result = converter.convert("(this -is also- removed)")
        assert result == "(this ~~is also~~ removed)"
