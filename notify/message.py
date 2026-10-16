"""通知消息模型与校验。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

MAX_BODY_BYTES = 512 * 1024

LEVELS = ("info", "warn", "error")

LEVEL_SUBJECT_TAGS = {
    "info": "",
    "warn": "[注意]",
    "error": "[异常]",
}


class MessageError(ValueError):
    """消息不合法（CLI 映射为退出码 2）。"""


@dataclass
class Message:
    """一条待投递的通知。"""

    title: str
    body: str
    level: str = "info"
    dedup_key: Optional[str] = None
    source: str = ""
    created_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()
        if not self.title:
            raise MessageError("title 不能为空")
        if self.level not in LEVELS:
            raise MessageError(
                "level 必须是 %s 之一，收到 %r" % ("/".join(LEVELS), self.level)
            )
        if not isinstance(self.body, str) or not self.body.strip():
            raise MessageError("body 不能为空")
        size = len(self.body.encode("utf-8"))
        if size > MAX_BODY_BYTES:
            raise MessageError(
                "body 过大：%d 字节，上限 %d 字节" % (size, MAX_BODY_BYTES)
            )

    @property
    def effective_dedup_key(self) -> str:
        if self.dedup_key and self.dedup_key.strip():
            return self.dedup_key.strip()
        digest = hashlib.sha1(self.title.encode("utf-8")).hexdigest()
        return "sha1:" + digest

    @property
    def subject_tag(self) -> str:
        return LEVEL_SUBJECT_TAGS[self.level]
