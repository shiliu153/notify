# notify

阿里云 ECS 上的日报邮件通知工具。把脚本生成的总结文本渲染成 HTML 邮件，经阿里云邮件推送（DirectMail）发到自己的 QQ 邮箱；主通道失败时自动降级到 QQ 邮箱 SMTP。手机端用 QQ 邮箱 App 收推送，无需安装额外 App。

- 零运行时依赖：只用 Python 标准库，ECS 上不用装任何包
- 主备双通道：DirectMail 失败自动走 QQ 邮箱
- 去重与限流：cron 重跑不会重复轰炸，退出码可区分"跳过"与"失败"
- 密钥只在环境变量里，配置文件必须 600

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
