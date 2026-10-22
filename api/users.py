"""Управление пользователями веб-приложения (запускать на сервере из папки проекта).

    python3 -m api.users add ivanov --admin          # пароль спросит дважды
    python3 -m api.users add petrov --generate       # сгенерировать пароль и показать один раз
    python3 -m api.users passwd ivanov               # сменить пароль (все сессии пользователя закрываются)
    python3 -m api.users disable petrov | enable petrov
    python3 -m api.users del petrov
    python3 -m api.users list
    python3 -m api.users logout [--user ivanov]      # закрыть сессии (всех или одного пользователя)

Или через обёртку: ./scripts/users.sh add ivanov --admin
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import string
import sys
import time

from forestmerge import settings

from . import auth


def _ask_password() -> str:
    p1 = getpass.getpass("Пароль: ")
    p2 = getpass.getpass("Ещё раз: ")
    if p1 != p2:
        sys.exit("Пароли не совпадают")
    return p1


def _generate() -> str:
    alphabet = string.ascii_letters + string.digits + "-_.!@#%"
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(16))
        if any(c.isdigit() for c in p) and any(c.isupper() for c in p) and any(c.islower() for c in p):
            return p


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="api.users", description="пользователи ForestMerge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("login")
    a.add_argument("--admin", action="store_true")
    a.add_argument("--generate", action="store_true", help="сгенерировать пароль")
    p = sub.add_parser("passwd")
    p.add_argument("login")
    p.add_argument("--generate", action="store_true")
    for name in ("del", "disable", "enable"):
        sub.add_parser(name).add_argument("login")
    sub.add_parser("list")
    lo = sub.add_parser("logout")
    lo.add_argument("--user")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "add":
            pw = _generate() if args.generate else _ask_password()
            auth.create_user(args.login, pw, "admin" if args.admin else "user")
            print(f"Создан пользователь {args.login} ({'admin' if args.admin else 'user'}), база {settings.DB_PATH}")
            if args.generate:
                print(f"Пароль (показывается один раз): {pw}")
        elif args.cmd == "passwd":
            pw = _generate() if args.generate else _ask_password()
            auth.set_password(args.login, pw)
            print(f"Пароль {args.login} изменён, его сессии закрыты")
            if args.generate:
                print(f"Новый пароль (показывается один раз): {pw}")
        elif args.cmd == "del":
            auth.delete_user(args.login)
            print(f"Удалён {args.login}")
        elif args.cmd in ("disable", "enable"):
            auth.set_disabled(args.login, args.cmd == "disable")
            print(f"{args.login}: {'заблокирован' if args.cmd == 'disable' else 'разблокирован'}")
        elif args.cmd == "list":
            users = auth.list_users()
            if not users:
                print("Пользователей нет. Создайте: python3 -m api.users add <логин> --admin")
            for u in users:
                print(f"{u['login']:<20} {u['role']:<6} {'ЗАБЛОКИРОВАН ' if u['disabled'] else ''}"
                      f"сессий: {u['sessions']}  создан {time.strftime('%Y-%m-%d', time.localtime(u['created']))}")
        elif args.cmd == "logout":
            n = auth.revoke_sessions(args.user)
            print(f"Закрыто сессий: {n}")
    except ValueError as e:
        sys.exit(f"Ошибка: {e}")


if __name__ == "__main__":
    main()
