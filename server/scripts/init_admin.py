"""初始化管理员账号（首次部署用）。

用法: python -m server.scripts.init_admin <username> <password>
"""
from __future__ import annotations
import sys

from server.db import init_db, session_scope, repository as repo
from server.web.auth import hash_password


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("用法: python -m server.scripts.init_admin <username> <password>")
        return 2
    username, password = argv[1], argv[2]
    init_db()
    with session_scope() as s:
        if repo.get_user_by_username(s, username):
            print(f"用户 {username} 已存在，跳过。")
            return 0
        u = repo.create_user(s, username, hash_password(password), role="admin",
                             must_change_password=True)
        print(f"已创建管理员 {username} (id={u.id})。首次登录后请改密码。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
