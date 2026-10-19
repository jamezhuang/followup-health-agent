"""Local-only account provisioning: python -m app.account_admin admin001 --role admin."""

import argparse
import getpass
import secrets
import string

from .config import Settings
from .schemas import AuthRequest
from .storage import Store


def create_account(username, password, role="user"):
    credentials = AuthRequest(username=username, password=password)
    store = Store(Settings().database_url)
    try:
        account = store.register(credentials.email, credentials.password, role=role)
        if not account:
            raise ValueError("账号已存在，不会覆盖密码或提升权限。")
        return account
    finally:
        store.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="创建本地管理员或普通用户；不提供公开提权接口。")
    parser.add_argument("username", help="6–20 位数字或字母")
    parser.add_argument("--role", choices=["admin", "user"], default="user")
    parser.add_argument("--generate-password", action="store_true", help="生成并显示随机初始密码")
    args = parser.parse_args()
    password = ("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
                if args.generate_password else getpass.getpass("密码（6–20 位数字或字母）："))
    if not args.generate_password and password != getpass.getpass("再次输入密码："):
        parser.error("两次输入的密码不一致。")
    try:
        account = create_account(args.username, password, args.role)
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    print(f"账号：{account['username']}；角色：{account['role']}")
    if args.generate_password:
        print(f"初始密码：{password}")


if __name__ == "__main__":
    main()
