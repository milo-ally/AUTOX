"""CLI helpers for the WeChat channel."""

from __future__ import annotations

import argparse
from typing import Sequence

from microclaw.channels.transports.wechat import (
    delete_wechat_account,
    list_wechat_accounts,
    load_wechat_account,
    login_wechat_with_qr,
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="microclaw wechat",
        description="Manage WeChat channel accounts.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    login = sub.add_parser("login", help="Login by scanning a WeChat QR code.")
    login.add_argument("--account", default="default", help="Local account label before server returns bot id.")
    login.add_argument("--timeout", type=int, default=480, help="Login timeout in seconds (default: 480).")
    login.add_argument("--bot-type", default="3", help="iLink bot_type for QR login (default: 3).")

    sub.add_parser("list", help="List saved WeChat accounts.")

    logout = sub.add_parser("logout", help="Delete a saved WeChat account.")
    logout.add_argument("account", help="Saved account id to delete.")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)

    if args.cmd == "login":
        result = login_wechat_with_qr(
            account_id=args.account,
            timeout_s=args.timeout,
            bot_type=args.bot_type,
        )
        print(result.message)
        if result.connected:
            print(f"account_id: {result.account_id}")
            print(f"base_url: {result.base_url}")
            print("\nNow start the gateway:")
            print(f"microclaw serve --channel wechat --instance {result.account_id}")
            return 0
        return 1

    if args.cmd == "list":
        ids = list_wechat_accounts()
        if not ids:
            print("No saved WeChat accounts. Run: microclaw wechat login")
            return 0
        for account_id in ids:
            account = load_wechat_account(account_id)
            suffix = f" base_url={account.base_url}" if account else ""
            print(f"{account_id}{suffix}")
        return 0

    if args.cmd == "logout":
        deleted = delete_wechat_account(args.account)
        print("Deleted." if deleted else "Account not found.")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
