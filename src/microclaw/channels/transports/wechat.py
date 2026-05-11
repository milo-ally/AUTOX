"""WeChat iLink channel.

This is a Python channel-layer implementation inspired by the OpenClaw WeChat iLink plugin:

  * long-poll `ilink/bot/getupdates`
  * persist `get_updates_buf` so restarts resume from the last cursor
  * extract text from `WechatMessage.item_list`
  * carry `context_token` from inbound to outbound replies
  * send text replies through `ilink/bot/sendmessage`

It intentionally stays in the interaction layer. The channel only handles
transport concerns; `ChannelEngine` still owns the agent runtime turn.

MVP scope: text-only direct messages. Media download/upload, typing tickets,
slash-command auth, pairing, and group policies can be layered in later.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, Field

from microclaw.channels.base import Channel, InboundMessage, TurnSink
from microclaw.channels.sinks import BufferedTurnSink, ThrottledBufferedSink


MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
MESSAGE_ITEM_TEXT = 1
DEFAULT_BASE_URL = "http://127.0.0.1:48080/"
QR_LOGIN_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_ILINK_BOT_TYPE = "3"
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
SESSION_EXPIRED_ERRCODE = -14


class TextItem(BaseModel):
    text: str | None = None


class MessageItem(BaseModel):
    type: int | None = None
    text_item: TextItem | None = None
    voice_item: dict[str, Any] | None = None


class WechatMessage(BaseModel):
    seq: int | None = None
    message_id: int | None = None
    from_user_id: str | None = None
    to_user_id: str | None = None
    client_id: str | None = None
    create_time_ms: int | None = None
    update_time_ms: int | None = None
    delete_time_ms: int | None = None
    session_id: str | None = None
    group_id: str | None = None
    message_type: int | None = None
    message_state: int | None = None
    item_list: list[MessageItem] = Field(default_factory=list)
    context_token: str | None = None


class GetUpdatesResp(BaseModel):
    ret: int | None = None
    errcode: int | None = None
    errmsg: str | None = None
    msgs: list[WechatMessage] = Field(default_factory=list)
    get_updates_buf: str | None = None
    longpolling_timeout_ms: int | None = None


class WeChatAccountData(BaseModel):
    account_id: str
    token: str
    base_url: str = DEFAULT_BASE_URL
    user_id: str | None = None
    saved_at: str | None = None


class WeChatLoginResult(BaseModel):
    connected: bool
    account_id: str | None = None
    token: str | None = None
    base_url: str | None = None
    user_id: str | None = None
    message: str


@dataclass
class WechatAccountConfig:
    account_id: str = "default"
    base_url: str = DEFAULT_BASE_URL
    token: str | None = None
    poll_timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS
    state_dir: str = os.path.expanduser("~/.microclaw/channels/wechat/default")
    throttle_interval: float = 6.0


class WechatChannel(Channel):
    """Text-only WeChat channel using iLink bot getupdates/sendmessage."""

    name = "wechat"
    supports_streaming = False

    def __init__(
        self,
        *,
        account_id: str = "default",
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        poll_timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS,
        state_root: str | None = None,
        throttled: bool = True,
    ):
        root = state_root or default_wechat_state_root()
        loaded = load_wechat_account(account_id, root=root)
        if loaded is None and account_id == "default":
            loaded = load_default_wechat_account(root=root)
            if loaded is not None:
                account_id = loaded.account_id

        state_dir = os.path.join(root, account_id)
        os.makedirs(state_dir, exist_ok=True)
        self.config = WechatAccountConfig(
            account_id=account_id,
            base_url=loaded.base_url if loaded else base_url,
            token=token or (loaded.token if loaded else None),
            poll_timeout_ms=poll_timeout_ms,
            state_dir=state_dir,
        )
        self._inbound: queue.Queue[InboundMessage] = queue.Queue()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._throttled = throttled
        self._context_tokens: dict[str, str] = {}
        self._sync_path = os.path.join(state_dir, "get_updates_buf.txt")
        self._client = httpx.Client(timeout=None, trust_env=False)

    # -- Channel ------------------------------------------------------------

    def serve(self) -> Iterator[InboundMessage]:
        if not self.config.token:
            raise RuntimeError(
                "WeChat account is not logged in. Run: microclaw wechat login"
            )
        self._start_monitor()
        while not self._stopped.is_set():
            try:
                yield self._inbound.get(timeout=0.2)
            except queue.Empty:
                continue

    def open_sink(self, message: InboundMessage) -> TurnSink:
        to_user = str(message.reply_to or message.user_id)
        context_token = message.meta.get("context_token") if isinstance(message.meta, dict) else None
        if not isinstance(context_token, str):
            context_token = self._context_tokens.get(to_user)

        def deliver(text: str) -> None:
            if text.strip():
                self._send_text(to_user, text, context_token=context_token)

        if self._throttled:
            return ThrottledBufferedSink(
                deliver,
                flush_interval=self.config.throttle_interval,
                min_chars_between_flushes=200,
                include_tool_status=False,
            )
        return BufferedTurnSink(deliver, include_tool_status=False)

    def stop(self) -> None:
        self._stopped.set()
        try:
            self._client.close()
        except Exception:
            pass

    # -- monitor ------------------------------------------------------------

    def _start_monitor(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def _monitor_loop(self) -> None:
        get_updates_buf = self._load_sync_buf()
        timeout_ms = self.config.poll_timeout_ms
        failures = 0
        while not self._stopped.is_set():
            try:
                resp = self._get_updates(get_updates_buf, timeout_ms)
                if resp.longpolling_timeout_ms and resp.longpolling_timeout_ms > 0:
                    timeout_ms = resp.longpolling_timeout_ms

                if self._is_error(resp):
                    if resp.errcode == SESSION_EXPIRED_ERRCODE or resp.ret == SESSION_EXPIRED_ERRCODE:
                        time.sleep(60)
                        continue
                    failures += 1
                    time.sleep(min(30, 2 * failures))
                    continue

                failures = 0
                if resp.get_updates_buf:
                    get_updates_buf = resp.get_updates_buf
                    self._save_sync_buf(get_updates_buf)

                for msg in resp.msgs:
                    inbound = self._message_to_inbound(msg)
                    if inbound is not None:
                        self._inbound.put(inbound)
            except httpx.TimeoutException:
                continue
            except Exception:
                failures += 1
                time.sleep(min(30, 2 * failures))

    def _get_updates(self, get_updates_buf: str, timeout_ms: int) -> GetUpdatesResp:
        payload = {
            "get_updates_buf": get_updates_buf,
            "base_info": self._base_info(),
        }
        data = self._post_json("ilink/bot/getupdates", payload, timeout_ms=timeout_ms / 1000)
        return GetUpdatesResp.model_validate(data)

    def _message_to_inbound(self, msg: WechatMessage) -> InboundMessage | None:
        text = self._extract_text(msg)
        if not text.strip():
            return None
        sender = msg.from_user_id or "unknown"
        if msg.context_token:
            self._context_tokens[sender] = msg.context_token
        return InboundMessage(
            id=f"wx_{msg.message_id or msg.client_id or uuid.uuid4().hex[:12]}",
            text=text,
            user_id=sender,
            account_id=self.config.account_id,
            session_key=msg.session_id or sender,
            reply_to=sender,
            received_at=(msg.create_time_ms / 1000) if msg.create_time_ms else time.time(),
            meta={
                "context_token": msg.context_token,
                "raw_message": msg.model_dump(exclude_none=True),
            },
        )

    @staticmethod
    def _extract_text(msg: WechatMessage) -> str:
        parts: list[str] = []
        for item in msg.item_list:
            if item.type == MESSAGE_ITEM_TEXT and item.text_item and item.text_item.text:
                parts.append(item.text_item.text)
            elif item.voice_item and isinstance(item.voice_item.get("text"), str):
                parts.append(str(item.voice_item["text"]))
        return "\n".join(parts)

    @staticmethod
    def _is_error(resp: GetUpdatesResp) -> bool:
        return (resp.ret is not None and resp.ret != 0) or (resp.errcode is not None and resp.errcode != 0)

    # -- outbound -----------------------------------------------------------

    def _send_text(self, to_user: str, text: str, *, context_token: str | None = None) -> None:
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user,
                "client_id": f"microclaw-wechat-{uuid.uuid4().hex[:16]}",
                "message_type": MESSAGE_TYPE_BOT,
                "message_state": MESSAGE_STATE_FINISH,
                "item_list": [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": text}}],
                "context_token": context_token,
            },
            "base_info": self._base_info(),
        }
        self._post_json("ilink/bot/sendmessage", payload, timeout_ms=15)

    # -- HTTP ---------------------------------------------------------------

    def _post_json(self, endpoint: str, payload: dict[str, Any], *, timeout_ms: float) -> dict[str, Any]:
        url = urljoin(self._ensure_slash(self.config.base_url), endpoint)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        headers = self._headers(body)
        res = self._client.post(url, content=body.encode("utf-8"), headers=headers, timeout=timeout_ms)
        res.raise_for_status()
        if not res.text.strip():
            return {}
        return res.json()

    def _headers(self, body: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Content-Length": str(len(body.encode("utf-8"))),
            "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
            "iLink-App-ClientVersion": "1",
        }
        if self.config.token and self.config.token.strip():
            headers["Authorization"] = f"Bearer {self.config.token.strip()}"
        return headers

    @staticmethod
    def _base_info() -> dict[str, str]:
        return {"channel_version": "microclaw-wechat"}

    @staticmethod
    def _ensure_slash(url: str) -> str:
        return url if url.endswith("/") else f"{url}/"

    # -- sync buf -----------------------------------------------------------

    def _load_sync_buf(self) -> str:
        try:
            with open(self._sync_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def _save_sync_buf(self, value: str) -> None:
        tmp = self._sync_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(value)
        os.replace(tmp, self._sync_path)


# -- account store ----------------------------------------------------------


def default_wechat_state_root() -> str:
    return os.path.expanduser("~/.microclaw/channels/wechat")


def normalize_wechat_account_id(raw: str) -> str:
    return raw.strip().replace("@", "-").replace(".", "-").replace("/", "-") or "default"


def _accounts_dir(root: str | None = None) -> str:
    path = os.path.join(root or default_wechat_state_root(), "accounts")
    os.makedirs(path, exist_ok=True)
    return path


def _account_path(account_id: str, *, root: str | None = None) -> str:
    return os.path.join(_accounts_dir(root), f"{account_id}.json")


def _index_path(root: str | None = None) -> str:
    path = root or default_wechat_state_root()
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, "accounts.json")


def list_wechat_accounts(*, root: str | None = None) -> list[str]:
    try:
        with open(_index_path(root), "r", encoding="utf-8") as f:
            data = json.load(f)
        return [str(x) for x in data if str(x).strip()] if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_wechat_account(data: WeChatAccountData, *, root: str | None = None) -> None:
    account_id = normalize_wechat_account_id(data.account_id)
    payload = data.model_copy(update={
        "account_id": account_id,
        "saved_at": data.saved_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    path = _account_path(account_id, root=root)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload.model_dump(exclude_none=True), f, indent=2, ensure_ascii=False)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    ids = list_wechat_accounts(root=root)
    if account_id not in ids:
        ids.append(account_id)
        with open(_index_path(root), "w", encoding="utf-8") as f:
            json.dump(ids, f, indent=2, ensure_ascii=False)


def load_wechat_account(account_id: str, *, root: str | None = None) -> WeChatAccountData | None:
    account_id = normalize_wechat_account_id(account_id)
    try:
        with open(_account_path(account_id, root=root), "r", encoding="utf-8") as f:
            return WeChatAccountData.model_validate(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None


def load_default_wechat_account(*, root: str | None = None) -> WeChatAccountData | None:
    ids = list_wechat_accounts(root=root)
    if not ids:
        return None
    return load_wechat_account(ids[0], root=root)


def delete_wechat_account(account_id: str, *, root: str | None = None) -> bool:
    account_id = normalize_wechat_account_id(account_id)
    deleted = False
    try:
        os.unlink(_account_path(account_id, root=root))
        deleted = True
    except FileNotFoundError:
        pass
    ids = [x for x in list_wechat_accounts(root=root) if x != account_id]
    with open(_index_path(root), "w", encoding="utf-8") as f:
        json.dump(ids, f, indent=2, ensure_ascii=False)
    return deleted


# -- QR login ---------------------------------------------------------------


def _api_get_json(base_url: str, endpoint: str, *, timeout: float = 35.0) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        res = client.get(urljoin(WechatChannel._ensure_slash(base_url), endpoint))
        res.raise_for_status()
        return res.json()


def _print_qr(qr_text: str) -> None:
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_text)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        pass


def login_wechat_with_qr(
    *,
    account_id: str = "default",
    timeout_s: int = 480,
    bot_type: str = DEFAULT_ILINK_BOT_TYPE,
    root: str | None = None,
) -> WeChatLoginResult:
    qr_resp = _api_get_json(
        QR_LOGIN_BASE_URL,
        f"ilink/bot/get_bot_qrcode?bot_type={bot_type}",
        timeout=15,
    )
    qrcode_value = str(qr_resp.get("qrcode") or "")
    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
    if not qrcode_value or not qrcode_url:
        return WeChatLoginResult(connected=False, message="未能获取微信登录二维码。")

    print("\n使用微信扫描以下二维码：\n")
    _print_qr(qrcode_url)
    print("\n如果终端二维码不可用，请用浏览器打开以下链接扫码：")
    print(qrcode_url)
    print("\n等待扫码确认...\n")

    deadline = time.time() + timeout_s
    current_base = QR_LOGIN_BASE_URL
    scanned = False
    while time.time() < deadline:
        try:
            status = _api_get_json(
                current_base,
                f"ilink/bot/get_qrcode_status?qrcode={qrcode_value}",
                timeout=35,
            )
        except httpx.TimeoutException:
            status = {"status": "wait"}
        except Exception as e:
            time.sleep(2)
            continue

        state = status.get("status")
        if state == "wait":
            print(".", end="", flush=True)
        elif state == "scaned":
            if not scanned:
                print("\n已扫码，请在微信里确认登录。")
                scanned = True
        elif state == "scaned_but_redirect":
            redirect_host = status.get("redirect_host")
            if redirect_host:
                current_base = f"https://{redirect_host}"
        elif state == "expired":
            return WeChatLoginResult(connected=False, message="二维码已过期，请重新运行登录命令。")
        elif state == "confirmed":
            raw_account_id = str(status.get("ilink_bot_id") or account_id)
            normalized_id = normalize_wechat_account_id(raw_account_id)
            token = str(status.get("bot_token") or "")
            base_url = str(status.get("baseurl") or DEFAULT_BASE_URL)
            user_id = status.get("ilink_user_id")
            if not token:
                return WeChatLoginResult(connected=False, message="登录确认成功，但服务器未返回 bot token。")
            data = WeChatAccountData(
                account_id=normalized_id,
                token=token,
                base_url=base_url,
                user_id=str(user_id) if user_id else None,
            )
            save_wechat_account(data, root=root)
            return WeChatLoginResult(
                connected=True,
                account_id=normalized_id,
                token=token,
                base_url=base_url,
                user_id=str(user_id) if user_id else None,
                message="✅ 微信登录成功。",
            )
        time.sleep(1)

    return WeChatLoginResult(connected=False, message="登录超时，请重试。")


__all__ = [
    "WechatChannel",
    "WeChatAccountData",
    "WeChatLoginResult",
    "login_wechat_with_qr",
    "list_wechat_accounts",
    "load_wechat_account",
    "delete_wechat_account",
]
