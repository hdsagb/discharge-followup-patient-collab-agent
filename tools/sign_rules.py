"""规则集签名工具：对 redflag.json 计算 HMAC-SHA256 并写入 redflag.json.sig。

用法：
  python tools/sign_rules.py                 # 用 FDE_RULES_KEY / data/.rules_secret 签名
  python tools/sign_rules.py --key-file K    # 指定密钥文件
  python tools/sign_rules.py --check         # 只校验，不写入
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from core import redflag  # noqa: E402


def sign(check: bool = False, key_file: str | None = None) -> str:
    raw = redflag.RULES_PATH.read_bytes()
    key = Path(key_file).read_bytes().strip() if key_file else redflag.rules_secret()
    if not key:
        raise SystemExit("密钥为空，拒绝签发")
    digest = hmac.new(key, raw, hashlib.sha256).hexdigest()
    if check:
        actual = redflag.SIG_PATH.read_text(encoding="utf-8").strip()
        if not hmac.compare_digest(digest, actual):
            raise SystemExit("签名校验失败：规则文件与 redflag.json.sig 不一致")
        return digest
    redflag.SIG_PATH.write_text(digest + "\n", encoding="utf-8")
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description="redflag.json 规则签发/校验")
    parser.add_argument("--check", action="store_true", help="仅校验签名")
    parser.add_argument("--key-file", help="HMAC 密钥文件路径")
    args = parser.parse_args()
    digest = sign(check=args.check, key_file=args.key_file)
    print(("校验通过: " if args.check else "已签发: ") + digest)


if __name__ == "__main__":
    main()
