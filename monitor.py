#!/usr/bin/env python3
"""苹果官网翻新 Mac 监测器。

监测 https://www.apple.com.cn/shop/refurbished/mac 上的翻新机型，
当指定机型（默认 Mac Studio / Mac mini）有新产品上架时，立即提醒。
通知通道（config.json 的 channels，默认 ["feishu","smtp"]）：飞书优先，失败再试 QQ SMTP 邮件，任一成功即算已通知。

数据来源：翻新总页内嵌的 window.REFURB_GRID_BOOTSTRAP JSON。
唯一键：partNumber（如 FH1F4CH/A）。从未推送过的 partNumber 才算新上架；
推送过的货号记入永久台账 notified，下架后再上架也不再推送。
汇总页（config.json 的 summary）：每轮把全部在架翻新 Mac 渲染成一页 HTML，rsync 到云服务器；
推送只发“有 N 款新品 + 汇总页链接”。

用法：
  python3 monitor.py            正常巡检一次（供 launchd 定时调用）
  python3 monitor.py --once     同上
  python3 monitor.py --status   打印当前已记录状态，不抓取
  python3 monitor.py --selftest 注入假数据验证识别+正文生成，不发真邮件
  python3 monitor.py --test-email  发一封测试邮件验证 SMTP 配置可用
  python3 monitor.py --test-feishu 发一条测试消息验证飞书通道可用（会真实发送）
  python3 monitor.py --reset-baseline  重建基线（把当前在架机型记为已知，不提醒；保留已推送台账）
"""
import hashlib
import html as htmllib
import json
import os
import re
import signal
import smtplib
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "logs" / "monitor.log"
SECRET_FILE = Path.home() / ".config" / "apple-refurb-monitor" / "smtp_pass"

REFURB_URL = "https://www.apple.com.cn/shop/refurbished/mac"
SITE = "https://www.apple.com.cn"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# 文件日志按大小轮转：monitor.log 超过上限 → monitor.log.1 … .N（最旧的一份被覆盖）
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 5
# 轮转前就已超大的历史日志单独归档，不进入编号链，因此永远不会被自动删除
LOG_LEGACY_SUFFIX = ".pre-rotation"

# 通知通道：默认飞书优先、SMTP 兜底；飞书库在 2T 上，TCC/卸载时可能卡住或不可用，所以整段放进带超时的子进程
DEFAULT_CHANNELS = ["feishu", "smtp"]
FEISHU_LIB_DEFAULT = "~/.local/lib/_feishu-lib.sh"  # 你自己的飞书发送库路径，或在 config.feishu.lib 指定
FEISHU_SEND_TIMEOUT = 20      # 传给 feishu_send_text 的 curl 超时（秒）
FEISHU_HARD_TIMEOUT_GRACE = 15  # 整个子进程在此之上再给的硬上限，超时则整组 kill
# 子进程里 source 飞书库并调用 feishu_send_text；退出码 3=库文件不存在 4=库不可用
FEISHU_SCRIPT = r'''
lib="$1"; text="$2"; to="$3"
[ -f "$lib" ] || { echo "lib_missing"; exit 3; }
source "$lib" >/dev/null 2>&1 || { echo "lib_source_failed"; exit 4; }
type feishu_send_text >/dev/null 2>&1 || { echo "lib_no_function"; exit 4; }
feishu_send_text "$text" "$to"
'''

# 发信失败退避：连续失败 n 次后，下次尝试间隔 = min(BASE * 2^(n-1), MAX)
SEND_BACKOFF_BASE_MIN = 10
SEND_BACKOFF_MAX_MIN = 360

# 迁移旧 state 时，从日志的“已通知 N 款 …: ['G1JX1CH/A', …]”行找回推送过的货号
NOTIFIED_LOG_RE = re.compile(r"'([A-Z0-9]+/[A-Z]+)'")

# 汇总页：全部在架翻新 Mac 渲染成一页静态 HTML，rsync 到云服务器已对外开放的静态站子目录。
# config.json 没有 summary 段（或 enabled=false）时不生成、不上传。
SUMMARY_DIR = BASE_DIR / "site"
SUMMARY_FILE = SUMMARY_DIR / "index.html"
SUMMARY_HEARTBEAT_MIN = 30    # 内容没变时最多每 30 分钟重传一次，刷新页面上的“最近核对”时间
SUMMARY_UPLOAD_TIMEOUT = 60
NEW_BADGE_HOURS = 24          # 首次发现 24 小时内的货号标“新”
CATALOG_KEEP_DAYS = 60        # catalog 只记首次发现时间，下架超过 60 天的条目清掉（notified 永不清）
MODEL_NAMES = {
    "macmini": "Mac mini", "macstudio": "Mac Studio", "macpro": "Mac Pro",
    "macbookpro": "MacBook Pro", "macbookair": "MacBook Air", "macbookneo": "MacBook Neo",
    "imac": "iMac", "display": "显示器",
}
MODEL_ORDER = ["macstudio", "macmini", "macpro", "macbookpro", "macbookair", "macbookneo", "imac", "display"]


# ---------- 基础工具 ----------

def _rotate_log():
    try:
        size = LOG_FILE.stat().st_size
    except OSError:
        return
    if size < LOG_MAX_BYTES:
        return
    legacy = LOG_FILE.with_name(LOG_FILE.name + LOG_LEGACY_SUFFIX)
    if size >= 2 * LOG_MAX_BYTES and not legacy.exists():
        LOG_FILE.replace(legacy)
        return
    for i in range(LOG_BACKUPS - 1, 0, -1):
        src = LOG_FILE.with_name(f"{LOG_FILE.name}.{i}")
        if src.exists():
            src.replace(LOG_FILE.with_name(f"{LOG_FILE.name}.{i + 1}"))
    LOG_FILE.replace(LOG_FILE.with_name(f"{LOG_FILE.name}.1"))


def log(msg):
    """只写一路：文件日志（带轮转）。交互终端里额外回显到屏幕；
    launchd 下 stdout 不是 tty，不再把同一行重复写进 launchd.out.log。"""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if sys.stdout.isatty():
        print(line)
    try:
        _rotate_log()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"{line}\n(写日志失败: {e})", file=sys.stderr)


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log(f"读取 config.json 失败: {e}")
        sys.exit(1)


def read_secret():
    """读取 SMTP 授权码：优先环境变量，其次 ~/.config/apple-refurb-monitor/smtp_pass 文件首行。"""
    val = os.environ.get("APPLE_MONITOR_SMTP_PASS")
    if val and val.strip() and val.strip() != "YOUR_QQ_AUTH_CODE_HERE":
        return val.strip()
    if SECRET_FILE.exists():
        for raw in SECRET_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and line != "YOUR_QQ_AUTH_CODE_HERE":
                return line
    return None


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"first_run_done": False, "seen": {}, "last_check": None, "last_status": None}
    except (OSError, ValueError) as e:
        raise RuntimeError("读取 state.json 失败；保留原文件并停止，拒绝自动重建基线") from e
    if (not isinstance(state, dict)
            or not isinstance(state.get("first_run_done"), bool)
            or not isinstance(state.get("seen"), dict)):
        raise RuntimeError("state.json 结构无效；保留原文件并停止，拒绝自动重建基线")
    return state


def save_state(state):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------- 抓取与解析 ----------

def fetch_html(url, retries=3, backoff=3):
    """用系统 curl 抓取（绕开 python.org build 缺 CA 证书的问题，curl 走系统 keychain）。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = subprocess.run(
                ["/usr/bin/curl", "-sL", "--compressed", "--max-time", "30",
                 "-A", UA, "-H", "Accept-Language: zh-CN,zh;q=0.9", url],
                capture_output=True, timeout=45)
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode("utf-8", errors="replace")
            last_err = f"curl rc={r.returncode}: {r.stderr.decode(errors='replace')[:200]}"
        except (subprocess.SubprocessError, OSError) as e:
            last_err = e
        log(f"抓取失败({attempt}/{retries}): {last_err}")
        if attempt < retries:
            time.sleep(backoff * attempt)
    raise RuntimeError(f"抓取 {url} 失败，已重试 {retries} 次：{last_err}")


def parse_tiles(html):
    """从 REFURB_GRID_BOOTSTRAP 提取产品 tiles 列表。"""
    m = re.search(r"window\.REFURB_GRID_BOOTSTRAP\s*=\s*(\{.*?\});", html, re.S)
    if not m:
        raise RuntimeError("页面中找不到 REFURB_GRID_BOOTSTRAP，苹果可能改版")
    data = json.loads(m.group(1))
    tiles = data.get("tiles")
    if not isinstance(tiles, list):
        raise RuntimeError("REFURB_GRID_BOOTSTRAP.tiles 结构异常")
    return tiles


def match_watch(tile, watch_models, title_patterns):
    """判断该产品是否属于监测机型。机型字段优先，标题子串兜底。"""
    dims = (tile.get("filters") or {}).get("dimensions") or {}
    model = (dims.get("refurbClearModel") or "").lower()
    if model and model in watch_models:
        return True
    title = (tile.get("title") or "").replace("\xa0", " ")
    # 标题兜底：含 "Mac Studio"/"Mac mini"。注意 "Studio Display"（显示器）不含 "Mac Studio"，自动排除。
    return any(p.lower() in title.lower() for p in title_patterns)


def _tile_product(t):
    price = (t.get("price") or {}).get("currentPrice") or {}
    url = t.get("productDetailsUrl") or ""
    if url.startswith("/"):
        url = SITE + url
    return {
        "title": (t.get("title") or "").replace("\xa0", " ").strip(),
        "price": price.get("amount") or "",
        "raw_price": price.get("raw_amount") or "",
        "url": url,
        "model": ((t.get("filters") or {}).get("dimensions") or {}).get("refurbClearModel", ""),
    }


def extract_products(tiles, watch_models, title_patterns):
    """只取监测机型。"""
    return {t["partNumber"]: _tile_product(t) for t in tiles
            if t.get("partNumber") and match_watch(t, watch_models, title_patterns)}


def extract_all(tiles):
    """全部在架翻新 Mac（汇总页用）。"""
    return {t["partNumber"]: _tile_product(t) for t in tiles if t.get("partNumber")}


# ---------- 邮件 ----------

def _ssl_context():
    """构造带 CA 的 SSL context（python.org build 默认找不到 CA，显式指定）。"""
    for cafile in (None, "/etc/ssl/cert.pem"):
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001
            pass
        try:
            if cafile:
                return ssl.create_default_context(cafile=cafile)
        except Exception:  # noqa: BLE001
            continue
    return ssl.create_default_context()


def resolve_real_ip(host):
    """用 DoH(走443,不受 Clash TUN fake-ip DNS 劫持)解析域名真实 IP。"""
    try:
        r = subprocess.run(
            ["/usr/bin/curl", "-s", "--max-time", "12",
             f"https://dns.alidns.com/resolve?name={host}&type=A"],
            capture_output=True, timeout=15)
        data = json.loads(r.stdout or b"{}")
        ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
        return ips[0] if ips else None
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError, KeyError):
        return None


def get_src_ip():
    """取默认路由物理网卡的本地 IP，用作发信 socket 源地址以绕过 Clash TUN(utun)劫持。"""
    try:
        out = subprocess.check_output(["/sbin/route", "-n", "get", "default"], timeout=5).decode()
        m = re.search(r"interface:\s*(\S+)", out)
        if not m:
            return None
        ip = subprocess.check_output(["/usr/sbin/ipconfig", "getifaddr", m.group(1)], timeout=5).decode().strip()
        return ip or None
    except (subprocess.SubprocessError, OSError):
        return None


class _DirectSMTP_SSL(smtplib.SMTP_SSL):
    """连真实 IP + 绑物理网卡源地址(绕 TUN) + 用真实域名做 SNI/证书校验(不降级安全)。"""

    def __init__(self, real_ip, src_ip, sni, **kw):
        self._real_ip = real_ip
        self._src_ip = src_ip
        self._sni = sni
        super().__init__(host=sni, **kw)

    def _get_socket(self, host, port, timeout):
        sock = socket.create_connection((self._real_ip, port), timeout, source_address=(self._src_ip, 0))
        return self.context.wrap_socket(sock, server_hostname=self._sni)


def build_email_html(new_products, summary_link=None):
    rows = []
    for pn, p in new_products.items():
        gone = ""
        if p.get("on_shelf") is False:
            gone = f" · 发现于 {p.get('first_seen', '')}，本次检测时已下架"
        rows.append(f"""
        <tr>
          <td style="padding:12px 10px;border-bottom:1px solid #eee;font-size:15px;color:#1d1d1f;">
            <a href="{p['url']}" style="color:#06c;text-decoration:none;font-weight:600;">{p['title']}</a>
            <div style="color:#86868b;font-size:12px;margin-top:4px;">货号 {pn}{gone}</div>
          </td>
          <td style="padding:12px 10px;border-bottom:1px solid #eee;font-size:16px;color:#1d1d1f;font-weight:700;white-space:nowrap;text-align:right;">{p['price']}</td>
        </tr>""")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = ""
    if summary_link:
        link = htmllib.escape(summary_link)
        summary = (f'<p style="margin:16px 0 0;font-size:14px;color:#1d1d1f;">全部在架翻新 Mac 汇总（新品置顶）：'
                   f'<a href="{link}" style="color:#06c;">{link}</a></p>')
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f5f5f7;font-family:-apple-system,'PingFang SC',sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">
  <h1 style="font-size:22px;color:#1d1d1f;margin:0 0 4px;">🍎 苹果翻新上架提醒</h1>
  <p style="color:#86868b;font-size:13px;margin:0 0 20px;">检测时间 {ts} · 共 {len(new_products)} 款新上架</p>
  <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;">
    {''.join(rows)}
  </table>
  {summary}
  <p style="margin:20px 0 0;">
    <a href="{REFURB_URL}" style="display:inline-block;background:#06c;color:#fff;padding:10px 20px;border-radius:980px;text-decoration:none;font-size:14px;">前往苹果官网翻新店 →</a>
  </p>
  <p style="color:#86868b;font-size:12px;margin-top:24px;">翻新货数量稀少、售完即下架，建议尽快下单。本邮件由本地监测脚本自动发送。</p>
</div></body></html>"""


def send_email(config, subject, html_body):
    smtp = config["smtp"]
    password = read_secret()
    if not password:
        raise RuntimeError(
            f"未找到 SMTP 授权码。请在 {SECRET_FILE} 写入你的 QQ 邮箱 SMTP 授权码（单独一行）")

    sender = smtp["sender"]
    recipient = config["recipient"]
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(smtp.get("sender_name", "苹果翻新监测"), "utf-8")), sender))
    msg["To"] = recipient

    ctx = _ssl_context()
    host, port = smtp["host"], int(smtp["port"])
    raw = msg.as_string()

    # 首选：绕 Clash TUN 直连(真实IP + 绑物理网卡源地址 + SNI 校验域名)
    real_ip = resolve_real_ip(host)
    src_ip = get_src_ip()
    if port == 465 and real_ip and src_ip:
        try:
            with _DirectSMTP_SSL(real_ip, src_ip, host, port=port, timeout=30, context=ctx) as s:
                s.login(sender, password)
                s.sendmail(sender, [recipient], raw)
            log(f"邮件已发送 → {recipient}（{subject}）[direct {real_ip} via {src_ip}]")
            return
        except (smtplib.SMTPException, ssl.SSLError, OSError) as e:
            log(f"绕TUN直连发信失败，回退标准连接: {e}")

    # 回退：标准连接(无代理/直连网络环境)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
            s.login(sender, password)
            s.sendmail(sender, [recipient], raw)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(sender, password)
            s.sendmail(sender, [recipient], raw)
    log(f"邮件已发送 → {recipient}（{subject}）[standard]")


# ---------- 飞书 ----------

def build_feishu_text(products, summary_link=None):
    """飞书纯文本摘要：与邮件同一批货号。
    有汇总页链接时每款只占一行，详情看链接；没有链接（未配置或本轮上传失败）时附每款官网链接。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if summary_link:
        head = f"🍎 苹果翻新汇总页有更新 · 新上架 {len(products)} 款（检测时间 {ts}）"
    else:
        head = f"🍎 苹果翻新上架提醒 · 共 {len(products)} 款（检测时间 {ts}）"
    lines = [head, ""]
    for i, (pn, p) in enumerate(products.items(), 1):
        gone = ""
        if p.get("on_shelf") is False:
            gone = f"（发现于 {p.get('first_seen', '')}，本次检测时已下架）"
        if summary_link:
            lines.append(f"{i}. {short_title(p['title'])}  {p['price']}（货号 {pn}）{gone}")
            continue
        lines.append(f"{i}. {p['title']}  {p['price']}")
        lines.append(f"   货号 {pn}{gone}")
        if p.get("url"):
            lines.append(f"   {p['url']}")
    if summary_link:
        lines += ["", "汇总页已更新（全部在架翻新 Mac，新品置顶）：", summary_link,
                  "同一货号只提醒这一次，之后上下架不再重复推送。"]
    else:
        lines += ["", f"官网翻新店：{REFURB_URL}", "翻新货售完即下架，建议尽快下单。"]
    return "\n".join(lines)


def _feishu_python():
    """给飞书库用的 python：优先当前解释器（/usr/bin/python3 是 Xcode shim，许可未接受时会 exit 69）。"""
    for c in (sys.executable, "/usr/local/bin/python3", "/opt/homebrew/bin/python3", "/usr/bin/python3"):
        if c and os.access(c, os.X_OK):
            return c
    return "/usr/bin/python3"


def _exec_group(argv, env, timeout, timeout_msg):
    """在独立进程组里跑子进程；超时则杀整个进程组并抛 RuntimeError(timeout_msg)。返回 (returncode, stdout, stderr)。"""
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.communicate(timeout=5)
        except subprocess.SubprocessError:
            pass
        raise RuntimeError(timeout_msg)
    return p.returncode, out, err


def _feishu_exec(argv, env, timeout):
    """跑飞书子进程；超时则杀整个进程组并抛 RuntimeError。返回 (returncode, stdout, stderr)。"""
    return _exec_group(argv, env, timeout, f"飞书子进程超过 {timeout}s 未返回（2T/TCC 卡住？），已终止")


def send_feishu(config, text):
    """经本机飞书发送库的 feishu_send_text 发一条文本。成功返回；失败抛 RuntimeError。
    收件人/bot 默认取库内默认值；config.feishu 可选覆盖
    app_id / receive_id / receive_type，不含任何密钥（密钥由库经 secrets-getter 取）。"""
    fcfg = config.get("feishu") or {}
    lib = fcfg.get("lib") or FEISHU_LIB_DEFAULT
    to = int(fcfg.get("timeout") or FEISHU_SEND_TIMEOUT)
    env = dict(os.environ)
    env["FEISHU_PYTHON"] = _feishu_python()
    for key, var in (("app_id", "FEISHU_APP_ID"), ("receive_id", "FEISHU_RECEIVE_ID"),
                     ("receive_type", "FEISHU_RECEIVE_TYPE")):
        if fcfg.get(key):
            env[var] = str(fcfg[key])
    if fcfg.get("app_id"):
        env["FEISHU_SECRET_ID"] = f"app-{fcfg['app_id']}"
    argv = ["/bin/bash", "-c", FEISHU_SCRIPT, "feishu", lib, text, str(to)]
    try:
        rc, out, err = _feishu_exec(argv, env, to + FEISHU_HARD_TIMEOUT_GRACE)
    except OSError as e:
        raise RuntimeError(f"飞书子进程启动失败: {e}") from e
    out = (out or "").strip()
    if rc == 3:
        raise RuntimeError(f"飞书库不存在: {lib}（2T 未挂载或无权限？）")
    if rc == 4:
        raise RuntimeError(f"飞书库不可用: {out[:80]}")
    if rc != 0 or "code=0" not in out:
        raise RuntimeError(f"飞书发送失败 rc={rc}: {out[:160]}")
    log(f"飞书已发送（{out[:40]}）")


def notify_channels(config):
    """通道顺序；未知名字忽略，空列表回退默认。"""
    raw = config.get("channels")
    if not isinstance(raw, list):
        return list(DEFAULT_CHANNELS)
    chans = [c for c in raw if c in ("feishu", "smtp")]
    return chans or list(DEFAULT_CHANNELS)


def notify(config, subject, html_body, text_body):
    """按通道顺序尝试，任一成功即返回其名字；全部失败抛 RuntimeError（含各通道原因）。"""
    errors = []
    for ch in notify_channels(config):
        try:
            if ch == "feishu":
                send_feishu(config, text_body)
            else:
                send_email(config, subject, html_body)
            return ch
        except (RuntimeError, smtplib.SMTPException, ssl.SSLError, OSError, KeyError) as e:
            log(f"通道 {ch} 失败: {e!r}" if isinstance(e, KeyError) else f"通道 {ch} 失败: {e}")
            errors.append(f"{ch}: {e!r}" if isinstance(e, KeyError) else f"{ch}: {e}")
    raise RuntimeError("所有通知通道均失败 — " + " | ".join(errors))


# ---------- 主流程 ----------

def _parse_time(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def send_backoff(failures):
    """连续失败 failures 次后的等待时长（指数退避，有上限）。"""
    minutes = SEND_BACKOFF_BASE_MIN * (2 ** max(failures - 1, 0))
    return timedelta(minutes=min(minutes, SEND_BACKOFF_MAX_MIN))


# ---------- 已推送台账 ----------

def short_title(title):
    """去掉标题里的“翻新 ”前缀（飞书单行与汇总页用）。"""
    t = (title or "").strip()
    return t[3:].strip() if t.startswith("翻新 ") else t


def _notified_entry(p, when, via):
    return {"title": p.get("title", ""), "price": p.get("price", ""),
            "first_seen": p.get("first_seen") or when, "first_notified": when, "via": via}


def _log_files():
    """历史日志，从旧到新：.pre-rotation、.N … .1、当前日志。"""
    files = [LOG_FILE.with_name(LOG_FILE.name + LOG_LEGACY_SUFFIX)]
    files += [LOG_FILE.with_name(f"{LOG_FILE.name}.{i}") for i in range(LOG_BACKUPS, 0, -1)]
    files.append(LOG_FILE)
    return [f for f in files if f.exists()]


def notified_from_logs():
    """从日志的“已通知 N 款（…）: ['PN', …]”行找回推送过的货号 → {pn: 首次推送时间}。"""
    found = {}
    for f in _log_files():
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "已通知" not in line or ": [" not in line:
                        continue
                    when = line[1:20].replace(" ", "T") if line.startswith("[") else ""
                    for pn in NOTIFIED_LOG_RE.findall(line.split(": [", 1)[1]):
                        found.setdefault(pn, when)
        except OSError as e:
            log(f"读取历史日志 {f.name} 失败，跳过: {e}")
    return found


def ensure_notified(state, stamp):
    """取永久台账 notified（推送过的货号 + 基线已知货号，永不删除）。
    旧 state 没有这个字段时迁移一次：沿用 seen，再从历史日志补回推送过但已下架的货号。"""
    notified = state.get("notified")
    if isinstance(notified, dict):
        return notified
    notified = {}
    for pn, p in (state.get("seen") or {}).items():
        if isinstance(p, dict):
            notified[pn] = _notified_entry(p, p.get("first_seen") or stamp, "migrated")
    from_log = 0
    for pn, when in notified_from_logs().items():
        if pn not in notified:
            notified[pn] = {"title": "", "price": "", "first_seen": when or stamp,
                            "first_notified": when or stamp, "via": "log"}
            from_log += 1
    log(f"建立已推送台账：沿用 seen {len(notified) - from_log} 款，从历史日志补回 {from_log} 款")
    return notified


# ---------- 汇总页 ----------

def summary_config(config):
    s = config.get("summary")
    if not isinstance(s, dict) or s.get("enabled") is False or not s.get("url") or not s.get("remote"):
        return None
    return s


def update_catalog(state, all_items, now):
    """catalog 记录全部机型每个货号首次被发现的时间，汇总页据此标“新”。
    第一次建立时 first_seen 记空串：那一批是存量，不标“新”。下架超过 CATALOG_KEEP_DAYS 天的条目清掉。"""
    catalog = state.get("catalog")
    first_build = not isinstance(catalog, dict)
    if first_build:
        catalog = {}
    stamp = now.isoformat(timespec="seconds")
    for pn in all_items:
        entry = catalog.get(pn)
        if not isinstance(entry, dict):
            entry = catalog[pn] = {"first_seen": "" if first_build else stamp}
        entry["last_seen"] = stamp
    cutoff = now - timedelta(days=CATALOG_KEEP_DAYS)
    for pn in list(catalog):
        last = _parse_time((catalog[pn] or {}).get("last_seen") if isinstance(catalog[pn], dict) else None)
        if pn not in all_items and (last is None or last < cutoff):
            del catalog[pn]
    state["catalog"] = catalog
    return catalog


def _safe_url(url):
    """只放行苹果官网链接，其余一律换成翻新总页。去掉 ? 和 # 之后的部分：
    官网每次抓取给的 fnode 参数都不同，留着它页面哈希每轮都变；去掉后商品页照常打开。"""
    if not (isinstance(url, str) and url.startswith(SITE + "/")):
        return REFURB_URL
    return re.split(r"[?#]", url, maxsplit=1)[0]


def summary_rows(all_items, watched, pending, catalog, now):
    """汇总页的数据行；内容哈希只由这些行决定（不含时间），用来判断要不要重新上传。"""
    cutoff = now - timedelta(hours=NEW_BADGE_HOURS)
    rows = []
    for pn, p in all_items.items():
        first = _parse_time((catalog.get(pn) or {}).get("first_seen"))
        rows.append({
            "pn": pn,
            "title": short_title(p.get("title")),
            "price": p.get("price") or "",
            "raw_price": p.get("raw_price") or "",
            "url": _safe_url(p.get("url")),
            "model": (p.get("model") or "").lower(),
            "is_new": pn in pending or bool(first and first >= cutoff),
            "watched": pn in watched,
        })
    return rows


def _price_key(row):
    for raw in (row.get("raw_price"), re.sub(r"[^\d.]", "", row.get("price") or "")):
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return float("inf")


def _show_time(iso):
    return (iso or "")[:16].replace("T", " ")


# 设计 token：primitive（色阶、间距、字号）→ semantic（用途）；组件样式只引用 semantic token。
# 暗色模式只覆盖 semantic token。唯一断点 640px：窄屏下价格换到标题下方（媒体查询里不能用 var，所以写在这里）。
SUMMARY_CSS = """
:root {
  --gray-0: #ffffff; --gray-50: #f4f5f7; --gray-100: #e6e8ec; --gray-400: #9aa1ad;
  --gray-500: #5d6470; --gray-800: #2a2e37; --gray-850: #1c1f26; --gray-900: #15181d; --gray-950: #0d0f13;
  --blue-300: #8ab4ff; --blue-600: #0a58ca;
  --green-100: #dcf3e4; --green-200: #a6e2bd; --green-700: #12703b; --green-900: #123b25;
  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px; --space-6: 24px; --space-8: 32px;
  --radius-1: 4px; --radius-2: 10px;
  --border-1: 1px; --focus-width: 2px; --focus-offset: 2px;
  --text-xs: 12px; --text-sm: 13px; --text-md: 15px; --text-lg: 17px; --text-xl: 22px;
  --weight-semibold: 600; --weight-bold: 700;
  --leading-tight: 1.3; --leading-body: 1.55;
  --width-content: 860px;
  --font-sans: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;

  --color-page: var(--gray-50);
  --color-surface: var(--gray-0);
  --color-text: var(--gray-900);
  --color-muted: var(--gray-500);
  --color-line: var(--gray-100);
  --color-link: var(--blue-600);
  --color-focus: var(--blue-600);
  --color-new-bg: var(--green-100);
  --color-new-text: var(--green-700);
  --color-new-line: var(--green-700);
}
@media (prefers-color-scheme: dark) {
  :root {
    --color-page: var(--gray-950);
    --color-surface: var(--gray-850);
    --color-text: var(--gray-50);
    --color-muted: var(--gray-400);
    --color-line: var(--gray-800);
    --color-link: var(--blue-300);
    --color-focus: var(--blue-300);
    --color-new-bg: var(--green-900);
    --color-new-text: var(--green-200);
    --color-new-line: var(--green-200);
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--color-page); color: var(--color-text); font-family: var(--font-sans);
  font-size: var(--text-md); line-height: var(--leading-body); }
.wrap { max-width: var(--width-content); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
h1 { font-size: var(--text-xl); line-height: var(--leading-tight); margin: 0 0 var(--space-2); }
.lead { margin: 0; color: var(--color-muted); font-size: var(--text-sm); }
.block { background: var(--color-surface); border: var(--border-1) solid var(--color-line);
  border-radius: var(--radius-2); margin-top: var(--space-4); overflow: hidden; }
.block-new { border-color: var(--color-new-line); }
.block h2 { font-size: var(--text-lg); line-height: var(--leading-tight); margin: 0;
  padding: var(--space-3) var(--space-4); border-bottom: var(--border-1) solid var(--color-line); }
details.block > summary { display: flex; justify-content: space-between; gap: var(--space-3);
  padding: var(--space-3) var(--space-4); cursor: pointer; list-style: none; font-weight: var(--weight-semibold); }
details.block > summary::-webkit-details-marker { display: none; }
details.block > summary .name::before { content: "▸"; display: inline-block; margin-right: var(--space-2); color: var(--color-muted); }
details.block[open] > summary .name::before { content: "▾"; }
details.block[open] > summary { border-bottom: var(--border-1) solid var(--color-line); }
summary .meta { color: var(--color-muted); font-weight: normal; font-size: var(--text-sm); white-space: nowrap; }
.list { list-style: none; margin: 0; padding: 0; }
.item { display: flex; justify-content: space-between; align-items: baseline; gap: var(--space-4);
  padding: var(--space-3) var(--space-4); border-top: var(--border-1) solid var(--color-line); }
.item:first-child { border-top: 0; }
.item-main { min-width: 0; }
.item-title { color: var(--color-link); text-decoration: none; font-weight: var(--weight-semibold); overflow-wrap: anywhere; }
.item-title:hover { text-decoration: underline; }
.item-meta { color: var(--color-muted); font-size: var(--text-xs); margin-top: var(--space-1); }
.item-price { font-weight: var(--weight-bold); white-space: nowrap; }
.badge { display: inline-block; margin-right: var(--space-2); padding: 0 var(--space-2); border-radius: var(--radius-1);
  background: var(--color-new-bg); color: var(--color-new-text); font-size: var(--text-xs); font-weight: var(--weight-bold); }
.empty { margin: 0; padding: var(--space-3) var(--space-4); color: var(--color-muted); }
.foot { margin-top: var(--space-6); color: var(--color-muted); font-size: var(--text-xs); }
.foot p { margin: var(--space-1) 0; }
.foot a { color: var(--color-link); }
a:focus-visible, summary:focus-visible { outline: var(--focus-width) solid var(--color-focus); outline-offset: var(--focus-offset); }
@media (max-width: 640px) {
  .wrap { padding: var(--space-4) var(--space-3) var(--space-6); }
  .item { flex-direction: column; gap: var(--space-1); }
}
"""


def _item_html(r):
    e = htmllib.escape
    badge = '<span class="badge">新</span>' if r["is_new"] else ""
    return (f'<li class="item"><div class="item-main">{badge}'
            f'<a class="item-title" href="{e(r["url"])}" target="_blank" rel="noopener noreferrer">{e(r["title"])}</a>'
            f'<div class="item-meta">货号 {e(r["pn"])}</div></div>'
            f'<div class="item-price">{e(r["price"])}</div></li>')


def render_summary(rows, watch_models, updated_at, checked_at):
    """全部在架翻新 Mac 的单页 HTML：新上架置顶，关注机型展开，其余机型折叠；每款按价格从低到高。"""
    e = htmllib.escape
    groups = {}
    for r in rows:
        groups.setdefault(r["model"] or "other", []).append(r)
    for items in groups.values():
        items.sort(key=_price_key)
    watched_models = [m for m in MODEL_ORDER if m in watch_models] + sorted(m for m in watch_models if m not in MODEL_ORDER)
    others = [m for m in MODEL_ORDER if m in groups and m not in watch_models]
    others += sorted(m for m in groups if m not in MODEL_ORDER and m not in watch_models)
    watched_names = " / ".join(MODEL_NAMES.get(m, m) for m in watched_models)
    new_rows = sorted((r for r in rows if r["is_new"] and r["watched"]), key=_price_key)

    parts = []
    if new_rows:
        parts.append(f'<section class="block block-new" aria-labelledby="sec-new"><h2 id="sec-new">新上架 · {len(new_rows)} 款</h2>'
                     f'<ul class="list">{"".join(_item_html(r) for r in new_rows)}</ul></section>')
    for i, m in enumerate(watched_models):
        items = groups.get(m, [])
        body = "".join(_item_html(r) for r in items) if items else ""
        inner = f'<ul class="list">{body}</ul>' if items else '<p class="empty">当前没有在架货号。</p>'
        parts.append(f'<section class="block" aria-labelledby="sec-w{i}"><h2 id="sec-w{i}">{e(MODEL_NAMES.get(m, m))} · {len(items)} 款</h2>'
                     f'{inner}</section>')
    for m in others:
        items = groups[m]
        parts.append(f'<details class="block"><summary><span class="name">{e(MODEL_NAMES.get(m, m))}</span>'
                     f'<span class="meta">{len(items)} 款 · 最低 {e(items[0]["price"])}</span></summary>'
                     f'<ul class="list">{"".join(_item_html(r) for r in items)}</ul></details>')
    watched_count = sum(1 for r in rows if r["watched"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="light dark">
<title>苹果官网翻新 Mac 在架汇总</title>
<style>{SUMMARY_CSS}</style>
</head>
<body>
<main class="wrap">
<h1>苹果官网翻新 Mac 在架汇总</h1>
<p class="lead">共 {len(rows)} 款在架，其中 {e(watched_names)} {watched_count} 款。内容更新于 {e(_show_time(updated_at))} · 最近核对 {e(_show_time(checked_at))}</p>
{"".join(parts)}
<footer class="foot">
<p>数据来自 <a href="{REFURB_URL}" target="_blank" rel="noopener noreferrer">苹果官网翻新店</a>，每 5 分钟核对一次；翻新货售完即下架，以官网为准。</p>
<p>“新”= 24 小时内首次发现。{e(watched_names)} 的同一货号只推送一次，之后下架再上架不再推送。</p>
</footer>
</main>
</body>
</html>
"""


def upload_summary(remote):
    argv = ["/usr/bin/rsync", "-az", "--timeout=30",
            "-e", "/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=15",
            f"{SUMMARY_DIR}/", remote]
    rc, out, err = _exec_group(argv, None, SUMMARY_UPLOAD_TIMEOUT,
                               f"汇总页上传超过 {SUMMARY_UPLOAD_TIMEOUT}s 未返回，已终止")
    if rc != 0:
        raise RuntimeError(f"汇总页上传失败 rc={rc}: {((err or '') + (out or '')).strip()[:160]}")


def publish_summary(config, state, watched, all_items, pending, catalog, now):
    """生成汇总页；内容变了或距上次上传满 SUMMARY_HEARTBEAT_MIN 分钟就上传。
    返回可推送的链接（远端已是本轮内容时），否则 None。任何异常只记日志，不挡推送。"""
    scfg = summary_config(config)
    if not scfg:
        return None
    stamp = now.isoformat(timespec="seconds")
    try:
        rows = summary_rows(all_items, watched, pending, catalog, now)
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        changed = state.get("page_hash") != digest
        if changed:
            state["page_hash"] = digest
            state["page_updated_at"] = stamp
        watch_models = set(config.get("watch_models", ["macstudio", "macmini"]))
        page = render_summary(rows, watch_models, state.get("page_updated_at") or stamp, stamp)
        SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SUMMARY_FILE.with_suffix(".html.tmp")
        tmp.write_text(page, encoding="utf-8")
        tmp.replace(SUMMARY_FILE)
        last_up = _parse_time(state.get("page_uploaded_at"))
        if (state.get("page_uploaded_hash") != digest or last_up is None
                or now - last_up >= timedelta(minutes=SUMMARY_HEARTBEAT_MIN)):
            upload_summary(scfg["remote"])
            state["page_uploaded_hash"] = digest
            state["page_uploaded_at"] = stamp
            if changed or state.get("page_status") != "ok":
                log(f"汇总页已上传（{len(rows)} 款在架）")
            state["page_status"] = "ok"
    except Exception as e:  # noqa: BLE001 — 汇总页只是附加功能，任何失败都不能挡住推送
        state["page_status"] = f"error: {e}"
        log(f"汇总页生成或上传失败（不影响推送）: {e}")
        return None
    if state.get("page_uploaded_hash") != digest:
        return None
    return f"{scfg['url']}?v={now:%Y%m%d%H%M}"


def run_once(config, now=None):
    """巡检一次。

    state 字段（缺失时取默认值，兼容旧 state.json）：
      notified        永久已推送台账：推送过（或首次运行时已在架的基线）的货号，永不删除；
                      只有不在这里的货号才算新上架，所以同一货号下架再上架也不会再推送
      seen            notified 里当前仍在架的监测机型货号（--status 查看用）
      pending         已发现但尚未成功通知的货号；只有发信成功才会移出，绝不静默丢弃
      catalog         全部机型货号的首次/最近发现时间（汇总页“新”标记用）
      page_*          汇总页内容哈希、内容更新时间、上传时间与状态
      send_failures   连续发信失败次数
      next_send_after 退避期内（早于该时间）不尝试发信
      send_channels   上次运行时的通知通道配置；与当前不同（含旧 state 无此字段）时清零退避，
                      让新通道立刻有机会送出积压，而不是被旧通道（SMTP）攒下的退避挡住
    """
    now = now or datetime.now()
    stamp = now.isoformat(timespec="seconds")
    state = load_state()
    prev_seen = state.get("seen") if isinstance(state.get("seen"), dict) else {}
    pending = state.get("pending")
    if not isinstance(pending, dict):
        pending = {}
    failures = state.get("send_failures")
    if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
        failures = 0
    next_send_after = _parse_time(state.get("next_send_after"))
    channels = notify_channels(config)
    if state.get("send_channels") != channels:
        if failures or next_send_after:
            log(f"通知通道变更为 {channels}，清零发信退避（原连续失败 {failures} 次）")
        failures = 0
        next_send_after = None
    watch_models = set(config.get("watch_models", ["macstudio", "macmini"]))
    title_patterns = config.get("title_patterns", ["Mac Studio", "Mac mini"])

    try:
        html = fetch_html(REFURB_URL)
        tiles = parse_tiles(html)
        current = extract_products(tiles, watch_models, title_patterns)
    except (RuntimeError, json.JSONDecodeError) as e:
        log(f"巡检异常: {e}")
        state["last_check"] = stamp
        state["last_status"] = f"error: {e}"
        save_state(state)
        return

    notified = ensure_notified(state, stamp)
    all_items = extract_all(tiles)
    catalog = update_catalog(state, all_items, now)

    status = "ok"
    if not state.get("first_run_done"):
        # 首次运行：当前在架的都记为已知（不提醒），已推送台账里原有的记录保持不变
        for pn, p in current.items():
            notified.setdefault(pn, _notified_entry({**p, "first_seen": stamp}, stamp, "baseline"))
        state["first_run_done"] = True
        log(f"首次运行，建立基线：当前在架监测机型 {len(current)} 款（不发提醒）")
        publish_summary(config, state, current, all_items, pending, catalog, now)
    else:
        already = [pn for pn in pending if pn in notified]
        for pn in already:
            del pending[pn]
        if already:
            log(f"待通知里 {len(already)} 款已推送过，移出不再推送: {already}")
        fresh = [pn for pn in current if pn not in notified and pn not in pending]
        for pn, p in current.items():
            if pn in notified:
                continue
            first_seen = (pending.get(pn) or {}).get("first_seen") or stamp
            pending[pn] = {**p, "first_seen": first_seen}
        for pn, p in pending.items():
            p["on_shelf"] = pn in current
        if fresh:
            log(f"发现 {len(fresh)} 款新上架: {fresh}")
        back = [pn for pn in current if pn in notified and pn not in prev_seen]
        if back:
            log(f"{len(back)} 款已推送过的货号重新在架，按规则不再推送: {back}")

        # 先更新汇总页（含本轮新货号），上传成功才在推送里带链接；失败则退回逐款详情，不挡推送
        link = publish_summary(config, state, current, all_items, pending, catalog, now)

        if not pending:
            log(f"巡检完成，无新上架（当前在架监测机型 {len(current)} 款）")
        elif next_send_after and now < next_send_after:
            log(f"发信退避中（已连续失败 {failures} 次），本轮不发信；"
                f"待通知 {len(pending)} 款，{next_send_after.isoformat(timespec='seconds')} 后再试")
            status = f"send_backoff: {failures} failures, next {next_send_after.isoformat(timespec='seconds')}"
        else:
            # 所有待通知货号（含退避期间积压的）合并成一条摘要
            names = "、".join(short_title(p["title"])[:24] for p in pending.values())
            subject = f"🍎 苹果翻新上架：{len(pending)} 款新 Mac（{names[:40]}）"
            try:
                used = notify(config, subject, build_email_html(pending, link), build_feishu_text(pending, link))
                for pn, p in pending.items():
                    notified[pn] = _notified_entry(p, stamp, used)
                log(f"已通知 {len(pending)} 款（合并为 1 条，通道 {used}）: {list(pending)}")
                pending = {}
                failures = 0
                next_send_after = None
            except (RuntimeError, smtplib.SMTPException, OSError) as e:
                failures += 1
                next_send_after = now + send_backoff(failures)
                log(f"待通知 {len(pending)} 款但所有通道发送失败（连续第 {failures} 次，"
                    f"{next_send_after.isoformat(timespec='seconds')} 前不再尝试）: {e}")
                status = f"new_found_email_failed: {e}"

    # seen 只是 notified 里当前在架的那部分（便于查看）；判断新旧只看 notified，下架不会让货号“变新”
    seen = {}
    for pn in current:
        if pn in notified:
            prev = prev_seen.get(pn) if isinstance(prev_seen.get(pn), dict) else {}
            seen[pn] = {**current[pn], "first_seen": prev.get("first_seen") or notified[pn].get("first_seen") or stamp}
    state["seen"] = seen
    state["notified"] = notified
    state["pending"] = pending
    state["send_failures"] = failures
    state["send_channels"] = channels
    state["next_send_after"] = next_send_after.isoformat(timespec="seconds") if next_send_after else None
    state["last_check"] = stamp
    state["last_status"] = status
    save_state(state)


def cmd_status():
    state = load_state()
    print(json.dumps(state, ensure_ascii=False, indent=2))


def cmd_selftest(config):
    """注入一个假 Mac Studio，验证识别逻辑 + 邮件正文生成（不发送）。"""
    fake_tile = {
        "title": "翻新 Mac Studio Apple M4 Max 芯片 (配备 14 核中央处理器和 32 核图形处理器)",
        "partNumber": "FAKE123CH/A",
        "productDetailsUrl": "/shop/product/fake123ch/a?fnode=test",
        "price": {"currentPrice": {"amount": "RMB 14,999", "raw_amount": "14999.00"}},
        "filters": {"dimensions": {"refurbClearModel": "macstudio"}},
    }
    fake_display = {  # Studio Display 应被排除
        "title": "翻新 Apple Studio Display (配备标准玻璃面板)",
        "partNumber": "FYJG3CH/A",
        "price": {"currentPrice": {"amount": "RMB 9,219"}},
        "filters": {"dimensions": {"refurbClearModel": "studiodisplay"}},
    }
    watch_models = set(config.get("watch_models", ["macstudio", "macmini"]))
    title_patterns = config.get("title_patterns", ["Mac Studio", "Mac mini"])
    prods = extract_products([fake_tile, fake_display], watch_models, title_patterns)
    assert "FAKE123CH/A" in prods, "未能识别 Mac Studio！"
    assert "FYJG3CH/A" not in prods, "错误地把 Studio Display 当成了 Mac Studio！"
    print("[selftest] ✓ Mac Studio 识别正确，Studio Display 已正确排除")
    print("[selftest] 邮件主题预览：🍎 苹果翻新上架：1 款新 Mac")
    html = build_email_html(prods)
    preview = BASE_DIR / "logs" / "selftest_email_preview.html"
    preview.write_text(html, encoding="utf-8")
    print(f"[selftest] ✓ 邮件正文已生成，预览文件：{preview}")
    # 真实抓取与解析连通性测试
    try:
        html_live = fetch_html(REFURB_URL)
        tiles = parse_tiles(html_live)
        live = extract_products(tiles, watch_models, title_patterns)
        print(f"[selftest] ✓ 实时抓取成功：总 tiles {len(tiles)}，当前在架监测机型 {len(live)} 款")
        for pn, p in live.items():
            print(f"            - {p['title']} {p['price']} [{pn}]")
        now = datetime.now()
        rows = summary_rows(extract_all(tiles), live, {}, {}, now)
        page = render_summary(rows, watch_models, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"))
        page_preview = BASE_DIR / "logs" / "selftest_summary_preview.html"
        page_preview.write_text(page, encoding="utf-8")
        print(f"[selftest] ✓ 汇总页已生成（{len(rows)} 款在架，不上传），预览文件：{page_preview}")
    except Exception as e:  # noqa: BLE001
        print(f"[selftest] ✗ 实时抓取失败：{e}")
        return 1
    print("[selftest] 全部通过 ✓")
    return 0


def cmd_test_email(config):
    sample = {
        "TEST00CH/A": {
            "title": "翻新 Mac Studio Apple M4 Max（这是一封测试邮件）",
            "price": "RMB 14,999",
            "url": REFURB_URL,
        }
    }
    send_email(config, "🍎 苹果翻新监测 · 测试邮件", build_email_html(sample))
    print("测试邮件已发送，请检查收件箱（含垃圾邮件）。")


def cmd_test_feishu(config):
    sample = {
        "TEST00CH/A": {
            "title": "翻新 Mac Studio Apple M4 Max（这是一条测试消息）",
            "price": "RMB 14,999",
            "url": REFURB_URL,
        }
    }
    send_feishu(config, build_feishu_text(sample, (summary_config(config) or {}).get("url")))
    print("测试消息已发送，请检查飞书。")


def cmd_reset_baseline():
    """清掉基线与待通知，下次巡检把当前在架的记为已知；已推送台账与 catalog 保留，推过的货号仍不会再推。"""
    stamp = datetime.now().isoformat(timespec="seconds")
    keep = {}
    try:
        old = load_state()
    except RuntimeError as e:
        log(f"{e}；改为只按历史日志重建已推送台账")
        old = {}
    keep["notified"] = ensure_notified(old, stamp)
    if isinstance(old.get("catalog"), dict):
        keep["catalog"] = old["catalog"]
    save_state({"first_run_done": False, "seen": {}, "last_check": None, "last_status": None, **keep})
    log(f"已清除基线并保留已推送台账（{len(keep['notified'])} 款），下次巡检将重建（不提醒当前在架机型）")


def main():
    config = load_config()
    arg = sys.argv[1] if len(sys.argv) > 1 else "--once"
    if arg == "--status":
        cmd_status()
    elif arg == "--selftest":
        sys.exit(cmd_selftest(config))
    elif arg == "--test-email":
        cmd_test_email(config)
    elif arg == "--test-feishu":
        try:
            cmd_test_feishu(config)
        except RuntimeError as e:
            print(f"飞书发送失败: {e}", file=sys.stderr)
            sys.exit(1)
    elif arg == "--reset-baseline":
        cmd_reset_baseline()
    else:
        run_once(config)


if __name__ == "__main__":
    main()
