"""Tests for terminal markdown formatting, ANSI coloring, and consistent indentation."""

import unittest
from orchestrator.markdown import (
    render_markdown,
    format_inline,
    render_code_box,
    render_table
)


class TestMarkdownRenderer(unittest.TestCase):
    """Test terminal markdown parser and styler."""

    def test_format_inline_bold_semantics(self):
        """Test semantic color highlights for status keywords in bold."""
        self.assertIn("\033[1;32m", format_inline("**idle**"))
        self.assertIn("\033[1;32m", format_inline("**Complete**"))
        self.assertIn("\033[1;31m", format_inline("**Blocker**"))
        self.assertIn("\033[1;33m", format_inline("**running**"))
        self.assertIn("\033[1;37m", format_inline("**Normal Bold**"))

    def test_format_inline_code(self):
        """Test code syntax highlight vs file path highlight."""
        # Command or identifier -> yellow
        res_cmd = format_inline("`/run <goal>`")
        self.assertIn("\033[1;33m", res_cmd)

        # File or directory path -> cyan
        res_path = format_inline("`/home/user/project`")
        self.assertIn("\033[36m", res_path)

    def test_render_code_box(self):
        """Test rounded container box for code snippets."""
        box = render_code_box(["git status", "git diff"], lang="bash", width=60)
        box_text = "\n".join(box)
        self.assertIn("╭─", box_text)
        self.assertIn("bash", box_text)
        self.assertIn("│", box_text)
        self.assertIn("git status", box_text)
        self.assertIn("╰", box_text)

    def test_render_table(self):
        """Test markdown pipe table conversion to box drawings."""
        table_raw = [
            "| Task ID | Role | Status |",
            "|---|---|---|",
            "| 1 | planner | complete |",
            "| 2 | coder | running |"
        ]
        rendered = render_table(table_raw)
        table_text = "\n".join(rendered)
        self.assertIn("┌─", table_text)
        self.assertIn("├─", table_text)
        self.assertIn("└─", table_text)
        self.assertIn("planner", table_text)
        self.assertIn("complete", table_text)

    def test_render_markdown_bullet_indentation(self):
        """Test consistent indentation on wrapped bullet points."""
        text = "* **Long Task Description**: This is a very long text string that will surely wrap around to multiple lines and needs to have clean subsequent indentation aligned with the start of the text rather than column zero."
        rendered = render_markdown(text, width=50)
        lines = rendered.split("\n")
        self.assertGreater(len(lines), 1)
        # First line has bullet
        self.assertIn("•", lines[0])
        # Subsequent lines must start with at least 4 spaces of indentation
        for line in lines[1:]:
            self.assertTrue(line.startswith("    "), f"Line failed indentation: '{line}'")

    def test_render_markdown_headings_and_boxes(self):
        """Test headings and standalone code blocks."""
        sample = (
            "### Summary\n"
            "All systems nominal.\n\n"
            "`/run <goal>`\n"
        )
        rendered = render_markdown(sample)
        self.assertIn("▸ Summary", rendered)
        self.assertIn("╭─", rendered)
        self.assertIn("/run <goal>", rendered)


if __name__ == "__main__":
    unittest.main()
