#!/usr/bin/env python3
"""Markdown converter for Jira wiki markup to OpenProject markdown.

This module provides comprehensive conversion functionality for transforming
Jira's wiki markup syntax to OpenProject's markdown format, preserving
visual fidelity and functionality while adapting to OpenProject's dialect.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class MarkdownConverter:
    """Converts Jira wiki markup to OpenProject markdown format.

    This converter handles:
    1. Basic formatting (bold, italic, underline, strikethrough)
    2. Headings (h1-h6)
    3. Lists (ordered, unordered, nested)
    4. Block quotes and citations
    5. Code blocks and inline code
    6. Tables
    7. Links and references
    8. Horizontal rules
    9. Line breaks and paragraphs
    """

    def __init__(
        self,
        user_mapping: dict[str, int] | None = None,
        work_package_mapping: dict[str, int] | None = None,
    ) -> None:
        """Initialize the markdown converter.

        Args:
            user_mapping: Optional mapping of Jira usernames to OpenProject user IDs
            work_package_mapping: Optional mapping of Jira issue keys to OpenProject work package IDs

        """
        self.user_mapping = user_mapping or {}
        self.work_package_mapping = work_package_mapping or {}

        # Compile regex patterns for performance
        self._compile_patterns()

    def _compile_patterns(self) -> None:
        """Compile regex patterns for efficient text processing."""
        # Basic formatting patterns - be careful not to match markdown formatting
        # Bold text: *text* (but not if already inside markdown ** format)
        self.bold_pattern = re.compile(r"(?<!\*)\*([^*\n\(\)]+)\*(?!\*)")
        # Italic text: _text_ (but not if inside parentheses or already markdown)
        self.italic_pattern = re.compile(r"(?<!\()\b_([^_\n]+)_\b(?!\))")
        # Underline: +text+ -> <u>text</u>
        self.underline_pattern = re.compile(r"\+([^+\n]+)\+")
        # Strikethrough: -text- (but avoid table separators and bullets)
        self.strikethrough_pattern = re.compile(r"(?<![\|\s])-([^-\n\|]+)-(?![\|\s\-])")
        # Monospace: {{text}} -> `text`
        self.monospace_pattern = re.compile(r"\{\{([^}]+)\}\}")

        # Heading patterns (h1-h6)
        self.heading_pattern = re.compile(r"^h([1-6])\.\s*(.+)$", re.MULTILINE)

        # List patterns (be careful not to match markdown headings or bold text)
        # For unordered lists, require * to be followed by space or another *
        self.unordered_list_pattern = re.compile(r"^(\s*)(\*+)\s+(.+)$", re.MULTILINE)
        # Jira ordered lists: # followed by space, not markdown headings which are #+ word
        self.ordered_list_pattern = re.compile(r"^(\s*)(#+)\s+(.+)$", re.MULTILINE)

        # Block quote pattern
        self.blockquote_pattern = re.compile(r"^bq\.\s*(.+)$", re.MULTILINE)

        # Horizontal rule pattern
        self.hr_pattern = re.compile(r"^----+$", re.MULTILINE)

        # Code block patterns
        self.code_block_pattern = re.compile(
            r"\{code(?::([^}]*))?\}(.*?)\{code\}",
            re.DOTALL,
        )
        self.noformat_pattern = re.compile(r"\{noformat\}(.*?)\{noformat\}", re.DOTALL)

        # Link patterns - avoid matching markdown images and user mentions
        self.link_pattern = re.compile(r"(?<!\!)\[([^|\]~][^|\]]*)\|?([^\]]*)\]")

        # Issue reference patterns
        self.issue_ref_pattern = re.compile(r"\b([A-Z][A-Z0-9_]*-\d+)\b")

        # User mention patterns
        self.user_mention_pattern = re.compile(r"\[~([^]]+)\]")

        # Attachment and embedded content patterns
        self.image_pattern = re.compile(r"!([^!|\s]+)(?:\|([^!]*))?!")
        self.attachment_pattern = re.compile(
            r"\[([^\|\]]+)\|([^\]]*\."
            r"(pdf|doc|docx|xls|xlsx|ppt|pptx|txt|zip|rar|7z))\]",
            re.IGNORECASE,
        )

        # Table patterns
        self.table_header_pattern = re.compile(r"^\|\|(.+)\|\|$", re.MULTILINE)
        self.table_row_pattern = re.compile(r"^\|(.+)\|$", re.MULTILINE)

        # Panel and macro patterns
        self.panel_pattern = re.compile(
            r"\{panel(?::([^}]*))?\}(.*?)\{panel\}",
            re.DOTALL,
        )
        self.info_pattern = re.compile(r"\{info(?::([^}]*))?\}(.*?)\{info\}", re.DOTALL)
        self.warning_pattern = re.compile(
            r"\{warning(?::([^}]*))?\}(.*?)\{warning\}",
            re.DOTALL,
        )
        self.note_pattern = re.compile(r"\{note(?::([^}]*))?\}(.*?)\{note\}", re.DOTALL)
        self.tip_pattern = re.compile(r"\{tip(?::([^}]*))?\}(.*?)\{tip\}", re.DOTALL)

        # Advanced macro patterns
        self.expand_pattern = re.compile(
            r"\{expand(?::([^}]*))?\}(.*?)\{expand\}",
            re.DOTALL,
        )
        self.tabs_pattern = re.compile(r"\{tabs\}(.*?)\{tabs\}", re.DOTALL)
        self.tab_pattern = re.compile(r"\{tab:([^}]+)\}(.*?)(?=\{tab:|$)", re.DOTALL)
        self.color_pattern = re.compile(r"\{color:([^}]+)\}(.*?)\{color\}", re.DOTALL)

    def convert(self, jira_markup: str) -> str:
        """Convert Jira wiki markup to OpenProject markdown.

        Args:
            jira_markup: The Jira wiki markup text to convert

        Returns:
            Converted OpenProject markdown text

        """
        if not jira_markup or not isinstance(jira_markup, str):
            return ""

        logger.debug("Converting Jira markup to OpenProject markdown")

        # Start with the original text
        text = jira_markup

        # Apply conversions in order of complexity (most specific first)
        text = self._convert_code_blocks(text)
        text = self._convert_advanced_macros(
            text,
        )  # Advanced macros before basic panels
        text = self._convert_panels_and_macros(text)
        text = self._convert_tables(text)
        text = self._convert_lists(
            text,
        )  # Convert lists before headings to avoid conflicts
        text = self._convert_headings(text)
        text = self._convert_block_quotes(text)
        text = self._convert_issue_references(text)
        text = self._convert_user_mentions(text)  # Convert user mentions before links
        text = self._convert_images(text)
        text = self._convert_links(text)
        text = self._convert_attachments(text)
        text = self._convert_horizontal_rules(text)
        text = self._convert_text_formatting(text)
        text = self._cleanup_whitespace(text)

        logger.debug("Markdown conversion completed")
        return text

    def _convert_headings(self, text: str) -> str:
        """Convert Jira headings (h1. through h6.) to markdown headings."""

        def replace_heading(match: re.Match[str]) -> str:
            level = int(match.group(1))
            content = match.group(2).strip()
            return f"{'#' * level} {content}"

        return self.heading_pattern.sub(replace_heading, text)

    def _convert_text_formatting(self, text: str) -> str:
        """Convert basic text formatting (bold, italic, underline, strikethrough)."""
        # Order matters: do strikethrough before bold to avoid conflicts
        # Strikethrough: -text- -> ~~text~~
        text = self.strikethrough_pattern.sub(r"~~\1~~", text)

        # Bold: *text* -> **text**
        text = self.bold_pattern.sub(r"**\1**", text)

        # Italic: _text_ -> *text*
        text = self.italic_pattern.sub(r"*\1*", text)

        # Underline: +text+ -> <u>text</u> (HTML fallback since markdown doesn't have underline)
        text = self.underline_pattern.sub(r"<u>\1</u>", text)

        # Monospace/inline code: {{text}} -> `text`
        return self.monospace_pattern.sub(r"`\1`", text)

    def _convert_lists(self, text: str) -> str:
        """Convert Jira lists to markdown lists."""

        # Convert unordered lists: * item, ** nested -> - item, - nested (with proper indentation)
        def replace_unordered_list(match: re.Match[str]) -> str:
            leading_space = match.group(1)
            asterisks = match.group(2)
            content = match.group(3)

            # Calculate nesting level: leading spaces + number of asterisks - 1
            level = len(leading_space) // 2 + len(asterisks) - 1
            md_indent = "  " * level
            return f"{md_indent}- {content}"

        text = self.unordered_list_pattern.sub(replace_unordered_list, text)

        # Convert ordered lists: # item, ## nested -> 1. item, 1. nested (with proper indentation)
        def replace_ordered_list(match: re.Match[str]) -> str:
            leading_space = match.group(1)
            hashes = match.group(2)
            content = match.group(3)

            # Calculate nesting level: leading spaces + number of hashes - 1
            level = len(leading_space) // 2 + len(hashes) - 1
            md_indent = "  " * level
            return f"{md_indent}1. {content}"

        return self.ordered_list_pattern.sub(replace_ordered_list, text)

    def _convert_block_quotes(self, text: str) -> str:
        """Convert Jira block quotes to markdown block quotes."""

        def replace_blockquote(match: re.Match[str]) -> str:
            content = match.group(1).strip()
            return f"> {content}"

        return self.blockquote_pattern.sub(replace_blockquote, text)

    def _convert_code_blocks(self, text: str) -> str:
        """Convert Jira code blocks to markdown code blocks."""

        # {code:language} ... {code} -> ```language\n...\n```
        def replace_code_block(match: re.Match[str]) -> str:
            language = match.group(1) or ""
            content = match.group(2).strip()
            return f"```{language}\n{content}\n```"

        text = self.code_block_pattern.sub(replace_code_block, text)

        # {noformat} ... {noformat} -> ```\n...\n```
        def replace_noformat(match: re.Match[str]) -> str:
            content = match.group(1).strip()
            return f"```\n{content}\n```"

        return self.noformat_pattern.sub(replace_noformat, text)

    def _convert_links(self, text: str) -> str:
        """Convert Jira links to markdown links."""

        def replace_link(match: re.Match[str]) -> str:
            first_part = match.group(1).strip()
            second_part = match.group(2).strip() if match.group(2) else ""

            if second_part:  # [title|url] format
                return f"[{first_part}]({second_part})"
            # [url] format (or [title] for internal)
            return f"[{first_part}]({first_part})"

        return self.link_pattern.sub(replace_link, text)

    def _convert_issue_references(self, text: str) -> str:
        """Convert Jira issue references to OpenProject work package references."""

        def replace_issue_ref(match: re.Match[str]) -> str:
            jira_key = match.group(1)

            # Look up the work package ID if mapping is available
            if self.work_package_mapping and jira_key in self.work_package_mapping:
                wp_id = self.work_package_mapping[jira_key]
                return f"#{wp_id}"
            # Fallback: preserve original reference with notation
            return f"~~{jira_key}~~ *(migrated issue)*"

        return self.issue_ref_pattern.sub(replace_issue_ref, text)

    def _convert_user_mentions(self, text: str) -> str:
        """Convert Jira user mentions to OpenProject user mentions."""

        def replace_user_mention(match: re.Match[str]) -> str:
            username = match.group(1).strip()
            if self.user_mapping and username in self.user_mapping:
                user_id = self.user_mapping[username]
                return f"@{user_id}"
            # Return original format if no mapping exists or user not found
            return match.group(0)  # Return the original [~username] format

        return self.user_mention_pattern.sub(replace_user_mention, text)

    def _convert_images(self, text: str) -> str:
        """Convert Jira images to markdown images."""

        def replace_image(match: re.Match[str]) -> str:
            image_url = match.group(1).strip()
            alt_text = match.group(2).strip() if match.group(2) else ""
            return f"![{alt_text}]({image_url})"

        return self.image_pattern.sub(replace_image, text)

    def _convert_attachments(self, text: str) -> str:
        """Convert Jira attachments to markdown links."""

        def replace_attachment(match: re.Match[str]) -> str:
            title = match.group(1).strip()
            filename = match.group(2).strip()
            return f"[{title}]({filename})"

        return self.attachment_pattern.sub(replace_attachment, text)

    def _convert_horizontal_rules(self, text: str) -> str:
        """Convert Jira horizontal rules to markdown horizontal rules."""
        return self.hr_pattern.sub("---", text)

    def _convert_tables(self, text: str) -> str:
        """Convert Jira tables to markdown tables."""
        lines = text.split("\n")
        result_lines = []
        in_table = False
        table_rows = []

        for line in lines:
            if self.table_row_pattern.match(line):
                # This is a table row
                if not in_table:
                    in_table = True
                    table_rows = []

                # Extract cells from the row
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                table_rows.append(cells)

            else:
                # Not a table row
                if in_table:
                    # End of table - convert and add to result
                    converted_table = self._format_markdown_table(table_rows)
                    result_lines.extend(converted_table)
                    in_table = False
                    table_rows = []

                result_lines.append(line)

        # Handle table at end of text
        if in_table and table_rows:
            converted_table = self._format_markdown_table(table_rows)
            result_lines.extend(converted_table)

        return "\n".join(result_lines)

    def _format_markdown_table(self, table_rows: list[list[str]]) -> list[str]:
        """Format table rows as markdown table."""
        if not table_rows:
            return []

        # Determine column count
        max_cols = max(len(row) for row in table_rows)

        # Normalize all rows to have the same number of columns
        normalized_rows = []
        for row in table_rows:
            normalized_row = row + [""] * (max_cols - len(row))
            normalized_rows.append(normalized_row)

        # Create markdown table
        result = []

        # Header row (first row)
        if normalized_rows:
            header = "| " + " | ".join(normalized_rows[0]) + " |"
            result.append(header)

            # Separator row
            separator = "| " + " | ".join(["---"] * max_cols) + " |"
            result.append(separator)

            # Data rows
            for row in normalized_rows[1:]:
                data_row = "| " + " | ".join(row) + " |"
                result.append(data_row)

        return result

    def _convert_panels_and_macros(self, text: str) -> str:
        """Convert Jira panels and macros to markdown equivalents."""

        # Helper function to parse title parameter
        def parse_title(params: str | None) -> str | None:
            if not params:
                return None
            title_match = re.search(r"title=([^|]+)", params)
            return title_match.group(1).strip() if title_match else None

        # Info panels
        def replace_info(match: re.Match[str]) -> str:
            title = parse_title(match.group(1)) or "Info"
            content = match.group(2).strip()
            return f"**ℹ️ {title}**\n\n{content}"

        text = self.info_pattern.sub(replace_info, text)

        # Warning panels
        def replace_warning(match: re.Match[str]) -> str:
            title = parse_title(match.group(1)) or "Warning"
            content = match.group(2).strip()
            return f"**⚠️ {title}**\n\n{content}"

        text = self.warning_pattern.sub(replace_warning, text)

        # Note panels
        def replace_note(match: re.Match[str]) -> str:
            title = parse_title(match.group(1)) or "Note"
            content = match.group(2).strip()
            return f"**📝 {title}**\n\n{content}"

        text = self.note_pattern.sub(replace_note, text)

        # Tip panels
        def replace_tip(match: re.Match[str]) -> str:
            title = parse_title(match.group(1)) or "Tip"
            content = match.group(2).strip()
            return f"**💡 {title}**\n\n{content}"

        text = self.tip_pattern.sub(replace_tip, text)

        # Generic panels
        def replace_panel(match: re.Match[str]) -> str:
            title = parse_title(match.group(1)) or "Panel"
            content = match.group(2).strip()
            return f"**📋 {title}**\n\n{content}"

        return self.panel_pattern.sub(replace_panel, text)

    def _convert_advanced_macros(self, text: str) -> str:
        """Convert advanced Jira macros to markdown equivalents."""

        def parse_title(params: str | None) -> str:
            """Parse title from macro parameters like 'title=Important Info'."""
            if not params:
                return ""

            # Look for title=value pattern
            title_match = re.search(r"title=([^|]+)", params)
            if title_match:
                return title_match.group(1).strip()
            return params

        # Expand/collapsible sections: {expand:title=...}...{expand}
        def replace_expand(match: re.Match[str]) -> str:
            title = parse_title(match.group(1)) or "Show/Hide Details"
            content = match.group(2).strip()
            # HTML details/summary for collapsible content (fallback since Markdown doesn't have native collapsible)
            return f"<details>\n<summary>{title}</summary>\n\n{content}\n\n</details>"

        text = self.expand_pattern.sub(replace_expand, text)

        # Tabs: {tabs}{tab:Tab1}content1{tab:Tab2}content2{tabs}
        def replace_tabs(match: re.Match[str]) -> str:
            tabs_content = match.group(1)

            # Extract individual tabs
            tabs = []
            for tab_match in self.tab_pattern.finditer(tabs_content):
                tab_title = tab_match.group(1).strip()
                tab_content = tab_match.group(2).strip()
                tabs.append((tab_title, tab_content))

            if not tabs:
                return tabs_content  # Fallback if no tabs found

            # Convert to markdown with clear section headers
            result = []
            for i, (title, content) in enumerate(tabs):
                if i == 0:
                    result.append(f"**📑 {title}**\n\n{content}")
                else:
                    result.append(f"**📑 {title}** *(Alternative View)*\n\n{content}")

            return "\n\n---\n\n".join(result)

        text = self.tabs_pattern.sub(replace_tabs, text)

        # Color formatting: {color:red}text{color}
        def replace_color(match: re.Match[str]) -> str:
            color = match.group(1).strip()
            content = match.group(2).strip()

            # Common color mappings to emoji/indicators
            color_indicators = {
                "red": "🔴",
                "green": "🟢",
                "blue": "🔵",
                "yellow": "🟡",
                "orange": "🟠",
                "purple": "🟣",
                "black": "⚫",
                "white": "⚪",
                "gray": "🔘",
                "grey": "🔘",
            }

            indicator = color_indicators.get(color.lower(), f"({color})")
            return f"{indicator} {content}"

        return self.color_pattern.sub(replace_color, text)

    def _cleanup_whitespace(self, text: str) -> str:
        """Clean up excessive whitespace and line breaks."""
        # Remove excessive blank lines (more than 2 consecutive)
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Remove trailing whitespace from lines
        lines = text.split("\n")
        cleaned_lines = [line.rstrip() for line in lines]

        return "\n".join(cleaned_lines)

    def convert_with_context(
        self,
        jira_markup: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Convert Jira markup with additional context information.

        Args:
            jira_markup: The Jira wiki markup text to convert
            context: Additional context for conversion (project info, issue info, etc.)

        Returns:
            Converted OpenProject markdown text with context-aware processing

        """
        if context:
            # Update mappings if provided in context
            if "user_mapping" in context:
                self.user_mapping.update(context["user_mapping"])
            if "work_package_mapping" in context:
                self.work_package_mapping.update(context["work_package_mapping"])

        return self.convert(jira_markup)
