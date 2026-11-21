"""发送邮箱验证码。

- console：只打印到日志，开发用。
- memory：存在内存里，测试用。
- smtp：生产用。SES、Postmark、Resend 等事务邮件服务都提供 SMTP。
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import get_settings

log = logging.getLogger("bm.email")

# memory 模式下发出的邮件：[(收件人, 验证码)]
outbox: list[tuple[str, str]] = []


def _build(to: str, code: str, purpose: str) -> EmailMessage:
    s = get_settings()
    action = "绑定邮箱" if purpose == "bind" else "登录账号"
    msg = EmailMessage()
    msg["From"] = s.mail_from
    msg["To"] = to
    msg["Subject"] = f"BM 验证码：{code}"
    msg.set_content(
        f"你正在{action}，验证码是 {code}，{s.code_ttl_minutes} 分钟内有效。\n"
        f"Your BM verification code is {code}. It expires in {s.code_ttl_minutes} minutes.\n\n"
        "如果不是你本人操作，请忽略这封邮件。"
    )
    return msg


def _send_smtp(msg: EmailMessage) -> None:
    s = get_settings()
    if s.smtp_use_ssl:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ssl.create_default_context(), timeout=15) as smtp:
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)


async def send_code(to: str, code: str, purpose: str) -> None:
    backend = get_settings().email_backend
    if backend == "memory":
        outbox.append((to, code))
    elif backend == "console":
        log.warning("[console 邮件] 发给 %s 的验证码：%s（生产环境请改用 smtp）", to, code)
    else:
        await asyncio.to_thread(_send_smtp, _build(to, code, purpose))
