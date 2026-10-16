"""Message 模型的校验与派生属性。"""
import unittest

from notify.message import MAX_BODY_BYTES, Message, MessageError


class TestMessageValidation(unittest.TestCase):
    def test_rejects_empty_title(self):
        with self.assertRaises(MessageError):
            Message(title="   ", body="正文")

    def test_rejects_empty_body(self):
        with self.assertRaises(MessageError):
            Message(title="标题", body="\n  \n")

    def test_rejects_unknown_level(self):
        with self.assertRaises(MessageError):
            Message(title="标题", body="正文", level="fatal")

    def test_rejects_oversized_body(self):
        with self.assertRaises(MessageError):
            Message(title="标题", body="x" * (MAX_BODY_BYTES + 1))

    def test_accepts_body_at_limit(self):
        msg = Message(title="标题", body="x" * MAX_BODY_BYTES)
        self.assertEqual(len(msg.body.encode("utf-8")), MAX_BODY_BYTES)

    def test_trims_title(self):
        self.assertEqual(Message(title="  日报  ", body="正文").title, "日报")


class TestMessageDerived(unittest.TestCase):
    def test_default_dedup_key_derives_from_title(self):
        a = Message(title="日报", body="内容 A")
        b = Message(title="日报", body="内容 B")
        self.assertTrue(a.effective_dedup_key.startswith("sha1:"))
        self.assertEqual(a.effective_dedup_key, b.effective_dedup_key)

    def test_explicit_dedup_key_wins(self):
        msg = Message(title="日报", body="正文", dedup_key="daily-digest")
        self.assertEqual(msg.effective_dedup_key, "daily-digest")

    def test_different_titles_get_different_keys(self):
        a = Message(title="日报 A", body="正文")
        b = Message(title="日报 B", body="正文")
        self.assertNotEqual(a.effective_dedup_key, b.effective_dedup_key)

    def test_subject_tag_by_level(self):
        self.assertEqual(Message(title="t", body="b").subject_tag, "")
        self.assertEqual(Message(title="t", body="b", level="warn").subject_tag, "[注意]")
        self.assertEqual(Message(title="t", body="b", level="error").subject_tag, "[异常]")


if __name__ == "__main__":
    unittest.main()
