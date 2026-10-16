"""正文渲染：HTML 转义、三种轻量结构、邮件模板。"""
import unittest

from notify.message import Message
from notify.render import (
    LEVEL_COLORS,
    escape,
    render_blocks,
    render_email_html,
    render_email_text,
)


class TestEscape(unittest.TestCase):
    def test_escapes_html_metacharacters(self):
        self.assertEqual(
            escape('<script>alert("x")&</script>'),
            "&lt;script&gt;alert(\"x\")&amp;&lt;/script&gt;",
        )

    def test_keeps_quotes_unquoted_entity(self):
        self.assertEqual(escape('a"b'), 'a"b')


class TestBlocks(unittest.TestCase):
    def test_paragraph_lines_join_with_br(self):
        self.assertEqual(render_blocks("第一行\n第二行"), "<p>第一行<br>第二行</p>")

    def test_blank_line_separates_paragraphs(self):
        self.assertEqual(
            render_blocks("第一段\n\n第二段"),
            "<p>第一段</p>\n<p>第二段</p>",
        )

    def test_dash_list_becomes_ul(self):
        self.assertEqual(
            render_blocks("- 磁盘 40%\n- 内存 60%"),
            "<ul><li>磁盘 40%</li><li>内存 60%</li></ul>",
        )

    def test_star_list_becomes_ul(self):
        self.assertEqual(render_blocks("* 一项"), "<ul><li>一项</li></ul>")

    def test_list_items_are_escaped(self):
        self.assertEqual(
            render_blocks("- <b>粗体</b>"),
            "<ul><li>&lt;b&gt;粗体&lt;/b&gt;</li></ul>",
        )

    def test_pipe_table_becomes_table(self):
        body = "| 项目 | 值 |\n| --- | --- |\n| 备份 | 成功 |"
        html = render_blocks(body)
        self.assertIn("<table", html)
        self.assertIn("<th", html)
        self.assertIn("项目", html)
        self.assertIn("<td", html)
        self.assertIn("成功", html)

    def test_pipe_without_separator_is_a_paragraph(self):
        html = render_blocks("a | b\nc | d")
        self.assertNotIn("<table", html)
        self.assertIn("<p>", html)

    def test_script_in_table_cell_is_escaped(self):
        body = "| a | b |\n| --- | --- |\n| <script> | x |"
        self.assertNotIn("<script>", render_blocks(body))

    def test_crlf_is_normalised(self):
        self.assertEqual(render_blocks("a\r\nb"), "<p>a<br>b</p>")

    def test_empty_body_gives_empty_string(self):
        self.assertEqual(render_blocks("   \n\n  "), "")


class TestEmailTemplates(unittest.TestCase):
    def setUp(self):
        self.msg = Message(title="服务器日报", body="- 备份完成", level="info")

    def test_text_version_contains_title_and_footer(self):
        text = render_email_text(self.msg, "ecs-01", "2026-10-16 20:00:00")
        self.assertIn("服务器日报", text)
        self.assertIn("ecs-01", text)
        self.assertIn("2026-10-16 20:00:00", text)
        self.assertIn("由 notify 自动发送", text)
        self.assertIn("- 备份完成", text)

    def test_html_version_uses_level_color(self):
        html = render_email_html(self.msg, "ecs-01", "2026-10-16 20:00:00")
        self.assertIn(LEVEL_COLORS["info"], html)

    def test_html_error_level_uses_red(self):
        msg = Message(title="异常", body="炸了", level="error")
        html = render_email_html(msg, "ecs-01", "2026-10-16 20:00:00")
        self.assertIn(LEVEL_COLORS["error"], html)

    def test_html_escapes_title(self):
        msg = Message(title="<img src=x>", body="正文")
        html = render_email_html(msg, "ecs-01", "2026-10-16 20:00:00")
        self.assertNotIn("<img src=x>", html)
        self.assertIn("&lt;img src=x&gt;", html)

    def test_html_has_no_external_resources(self):
        html = render_email_html(self.msg, "ecs-01", "2026-10-16 20:00:00")
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)


if __name__ == "__main__":
    unittest.main()
