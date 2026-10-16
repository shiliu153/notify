"""配置加载：INI 解析 + 权限校验 + 密钥注入。"""
from __future__ import annotations

import configparser
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional

DEFAULT_CONFIG_PATH = "~/.config/notify/config.ini"
DEFAULT_LOG_PATH = "~/.local/state/notify/notify.jsonl"
DEFAULT_STATE_PATH = "~/.local/state/notify/state.db"

CHANNEL_PREFIX = "channels.email."


class ConfigError(Exception):
    """配置非法（CLI 映射为退出码 2）。"""


@dataclass
class EmailChannelConfig:
    name: str
    enabled: bool
    host: str
    port: int
    username: str
    password: Optional[str]
    security: str


@dataclass
class Limits:
    per_minute: int = 5
    per_day: int = 50
    dedup_window_minutes: int = 720


@dataclass
class Config:
    path: Path
    recipients: List[str]
    from_name: str
    subject_prefix: str
    channels: List[EmailChannelConfig]
    limits: Limits
    log_path: Path
    state_path: Path

    @property
    def ready_channels(self) -> List[EmailChannelConfig]:
        return [c for c in self.channels if c.enabled and c.password]


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(value.strip()))


def _check_permissions(path: Path) -> None:
    if os.name == "nt":
        return
    mode = stat.S_IMODE(os.stat(str(path)).st_mode)
    if mode & 0o077:
        raise ConfigError(
            "配置文件权限过宽（%o）：必须为 600，否则密钥可能被他人读取。"
            "执行 chmod 600 %s" % (mode, path)
        )


def _parse_bool(parser: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    if not parser.has_option(section, key):
        return default
    try:
        return parser.getboolean(section, key)
    except ValueError as exc:
        raise ConfigError("[%s] %s 不是合法布尔值：%s" % (section, key, exc))


def _parse_int(parser: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    if not parser.has_option(section, key):
        return default
    try:
        return parser.getint(section, key)
    except ValueError as exc:
        raise ConfigError("[%s] %s 不是合法整数：%s" % (section, key, exc))


def _resolve_password(
    parser: configparser.ConfigParser,
    section: str,
    env: Mapping[str, str],
) -> Optional[str]:
    if parser.has_option(section, "password_env"):
        name = parser.get(section, "password_env").strip()
        if not name:
            raise ConfigError("[%s] password_env 不能为空" % section)
        value = env.get(name)
        return value if value else None
    if parser.has_option(section, "password_file"):
        raw = parser.get(section, "password_file").strip()
        if not raw:
            raise ConfigError("[%s] password_file 不能为空" % section)
        secret_path = _expand(raw)
        if not secret_path.exists():
            return None
        return secret_path.read_text(encoding="utf-8").strip() or None
    return None


def _load_channel(
    parser: configparser.ConfigParser,
    section: str,
    env: Mapping[str, str],
) -> EmailChannelConfig:
    name = section[len(CHANNEL_PREFIX):].strip()
    if not name:
        raise ConfigError("通道段落名不合法：%s" % section)

    for required in ("host", "username"):
        if not parser.has_option(section, required) or not parser.get(section, required).strip():
            raise ConfigError("[%s] 缺少必填项 %s" % (section, required))

    port = _parse_int(parser, section, "port", 465)
    if not (1 <= port <= 65535):
        raise ConfigError("[%s] port 越界：%d" % (section, port))

    security = "ssl" if port == 465 else "starttls"
    if parser.has_option(section, "security"):
        security = parser.get(section, "security").strip().lower()
        if security not in ("ssl", "starttls"):
            raise ConfigError("[%s] security 只能是 ssl 或 starttls" % section)

    return EmailChannelConfig(
        name=name,
        enabled=_parse_bool(parser, section, "enabled", True),
        host=parser.get(section, "host").strip(),
        port=port,
        username=parser.get(section, "username").strip(),
        password=_resolve_password(parser, section, env),
        security=security,
    )


def load_config(
    path: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Config:
    """读取并校验配置。任何问题都抛 ConfigError（调用方映射为退出码 2）。"""
    env = os.environ if env is None else env
    raw_path = path or os.environ.get("NOTIFY_CONFIG") or DEFAULT_CONFIG_PATH
    config_path = _expand(raw_path)

    if not config_path.exists():
        raise ConfigError("配置文件不存在：%s" % config_path)
    if config_path.is_dir():
        raise ConfigError("配置路径是目录：%s" % config_path)

    _check_permissions(config_path)

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(str(config_path), "r", encoding="utf-8") as fh:
            parser.read_file(fh)
    except configparser.Error as exc:
        raise ConfigError("配置解析失败：%s" % exc)
    except OSError as exc:
        raise ConfigError("配置读取失败：%s" % exc)

    if not parser.has_section("message"):
        raise ConfigError("缺少 [message] 段落")

    raw_to = parser.get("message", "to", fallback="")
    recipients = [item.strip() for item in raw_to.split(",") if item.strip()]
    if not recipients:
        raise ConfigError("[message] to 不能为空")
    for addr in recipients:
        if "@" not in addr or addr.startswith("@") or addr.endswith("@"):
            raise ConfigError("[message] to 不是合法邮箱：%r" % addr)

    from_name = parser.get("message", "from_name", fallback="").strip() or "notify"
    subject_prefix = parser.get("message", "subject_prefix", fallback="").strip()

    sections = [s for s in parser.sections() if s.startswith(CHANNEL_PREFIX)]
    if not sections:
        raise ConfigError("没有任何 [%s<名称>] 通道段落" % CHANNEL_PREFIX)
    channels = [_load_channel(parser, s, env) for s in sections]

    limits = Limits(
        per_minute=_parse_int(parser, "limits", "per_minute", 5)
        if parser.has_section("limits")
        else 5,
        per_day=_parse_int(parser, "limits", "per_day", 50)
        if parser.has_section("limits")
        else 50,
        dedup_window_minutes=_parse_int(parser, "limits", "dedup_window_minutes", 720)
        if parser.has_section("limits")
        else 720,
    )

    log_path = _expand(parser.get("log", "path", fallback=DEFAULT_LOG_PATH))
    state_path = _expand(parser.get("state", "path", fallback=DEFAULT_STATE_PATH))

    return Config(
        path=config_path,
        recipients=recipients,
        from_name=from_name,
        subject_prefix=subject_prefix,
        channels=channels,
        limits=limits,
        log_path=log_path,
        state_path=state_path,
    )
