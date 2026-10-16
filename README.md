# notify

阿里云 ECS 上的日报邮件通知工具。把脚本生成的总结文本渲染成 HTML 邮件，经阿里云邮件推送（DirectMail）发到自己的 QQ 邮箱。手机端用 QQ 邮箱 App 收推送，无需安装额外 App。

- 零运行时依赖：只用 Python 标准库，ECS 上不用装任何包
- 单一发信通道：DirectMail（QQ 邮箱 SMTP 兜底已按安全取舍移除，见下方注记）
- 去重与限流：cron 重跑不会重复轰炸，退出码可区分"跳过"与"失败"
- 密钥只在环境变量里，配置文件必须 600

> **关于降级通道的取舍**：QQ 邮箱兜底能覆盖 DirectMail 挂掉的情况，但它的授权码可经
> IMAP **读取整个邮箱**，凭据价值很高。`examples/config.ini.example` 里该通道默认
> `enabled = false`；只用主通道时，DirectMail 一旦故障，日报会**静默不到达**
> （投递日志里有 exit 1 记录，但不会有人收到信），需要你留意"今天没收到日报"这个信号。

## 安装

```bash
sudo mkdir -p /opt/notify /etc/notify && sudo chmod 700 /etc/notify
# 把本仓库放到 /opt/notify
sudo timedatectl set-timezone Asia/Shanghai

cp examples/config.ini.example ~/.config/notify/config.ini
chmod 600 ~/.config/notify/config.ini
sudo cp examples/notify.env.example /etc/notify/env
sudo chmod 600 /etc/notify/env
# 编辑上面两个文件，填入真实域名、QQ 号和密钥
```

不想用包装脚本的话，也可以直接装成命令（无依赖，只是把 `notify` 放进 PATH）：

```bash
pip install .
notify doctor
```

## 手工步骤（必须在控制台完成）

1. **邮件推送控制台**：创建发信域名 → 按控制台生成值添加 DNS 记录（所有权 TXT、回信 MX、SPF、DKIM，建议再加 DMARC）→ 创建发信地址 `notice@<域名>` 并生成 SMTP 密码。
2. **QQ 邮箱**：设置 → 账户 → 开启 SMTP 服务 → 生成授权码，填入 `NOTIFY_QQ_AUTHCODE`。
3. **收信侧**：第一封发出后，在 QQ 邮箱把它标为"非垃圾邮件"，并把发件人加进联系人或白名单。

## 使用

```bash
bin/notify doctor                     # 体检：配置、权限、端口、DNS
bin/notify test                       # 每个就绪通道各发一条自检消息
bin/notify send --title "日报" --body-file /tmp/daily.md --dedup-key daily-digest
bin/notify send --title "日报" --body-file /tmp/daily.md --dry-run
bin/notify channels                   # 看通道状态
bin/notify log -n 20                  # 看最近投递记录
```

## 日报内容

`examples/daily-report.py` 生成两节日报，交给 `notify` 发送。

**第一节「硬故障巡检」**——只有这五类会进结论，其余一律进明细：

| 判据 | 阈值 |
| --- | --- |
| 进程 / 端口 / 容器 | 任一非正常 |
| 站点可用性 | 任一不可达（已知项白名单除外） |
| 磁盘 `/` | ≥ 90% |
| MySQL 备份 | 最新备份距今 > 26 小时 |
| 站点证书 | 剩余 ≤ 7 天 |

**第二节「运营明细」**——请求质量（成功率 / 延迟与 TTFB 分位 / 重试 / 账号·模型·来源分布 /
峰值时段）、额度（余额 / 日均消耗 / 可撑天数）、系统（内存·负载·磁盘 / journal 按 unit 分类 /
nginx 错误 / SSH 登录）。

**环比**来自每日快照 `/var/lib/notify/daily-stats.jsonl`（一行一天，保留 400 天，可用
`NOTIFY_STATS` 改位）。同一天重跑会替换该行；首次运行会从保留期内的 `requests-*.jsonl`
回填最近 7 天的请求指标，因此部署当天就有请求质量的昨日对比。额度类指标需要至少两条
余额记录，第二天起才显示日均消耗与可撑天数。

**刻意不报警的东西**：`https://shiliu153.cn` 的证书不匹配（`.cn` 备案中，见
`KNOWN_ISSUES` 白名单）、延迟 P99、错误率、重试率、journal 行数、SSH 失败次数——
它们是明细数字，不是告警。

日报脚本自带单元测试（`python -m unittest discover -s tests -t .`），全部用注入的假数据，
不碰真机器，在 Windows 上也能跑。

## 退出码

| 码 | 含义 | cron 里怎么处理 |
|---|---|---|
| 0 | 至少一个通道成功 | 正常 |
| 1 | 所有通道都失败 | 需要告警 |
| 2 | 配置/参数错误，或没有就绪通道 | 需要修配置 |
| 3 | 被去重或限流跳过 | 正常，忽略 |

## 排障

- **邮件进垃圾箱**：`bin/notify doctor --dkim-selector <控制台选择器>` 确认 SPF/DKIM/DMARC 齐备；在 QQ 邮箱把发件人加入白名单；正文少放外链。
- **535 认证失败**：DirectMail 的 SMTP 密码或 QQ 授权码不对；QQ 必须用授权码而不是登录密码。
- **没有任何就绪通道**：`/etc/notify/env` 里的变量名要和配置里的 `password_env` 完全一致，且 cron 走 `bin/notify`（它会 source 该文件）。
- **重复发送**：给日报脚本固定传 `--dedup-key daily-digest`。

## 待人工验证

自动化测试不真发信。首次部署后请依次执行并确认结果：

1. `bin/notify doctor` —— 端口可达、DNS 记录齐备；
2. `bin/notify test` —— 两个通道各收到一封自检邮件（首封可能进垃圾箱，手动标非垃圾并加白名单）；
3. `bin/notify send --title "日报" --body-file <真实日报文件> --dedup-key daily-digest` —— 确认 HTML 排版正常、中文不乱码；
4. 再跑一次同一条命令 —— 应返回退出码 3 且不重复发信。
