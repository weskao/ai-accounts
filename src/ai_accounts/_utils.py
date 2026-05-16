"""Shared helpers for wes-toolbox CLI tools."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Sequence

YELLOW = "\033[1;33m"
GREEN = "\033[1;32m"
RED = "\033[1;31m"
DIM = "\033[2m"
RESET = "\033[0m"


def _color_supported() -> bool:
    return sys.stderr.isatty()


def log_yellow(msg: str) -> None:
    if _color_supported():
        print(f"{YELLOW}{msg}{RESET}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


def log_green(msg: str) -> None:
    if _color_supported():
        print(f"{GREEN}{msg}{RESET}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


def log_red(msg: str) -> None:
    if _color_supported():
        print(f"{RED}{msg}{RESET}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def ensure_brew_package(pkg: str, cmd: str | None = None) -> bool:
    """Ensure ``cmd`` (defaults to pkg) exists; auto-install via Homebrew if not.

    Mirrors the zsh helper of the same name. Returns True on success.
    """
    bin_name = cmd or pkg
    if have(bin_name):
        return True
    log_yellow(f"⚠️  未偵測到 {bin_name},嘗試透過 Homebrew 安裝 {pkg}...")
    if not have("brew"):
        log_red("❌ 找不到 Homebrew,請先安裝:https://brew.sh")
        return False
    res = subprocess.run(["brew", "install", pkg])
    if res.returncode != 0:
        log_red(f"❌ brew install {pkg} 失敗")
        return False
    return have(bin_name)


def copy_to_clipboard(text: str) -> bool:
    """Pipe text into pbcopy. Returns True on success."""
    if not have("pbcopy"):
        return False
    try:
        proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        return proc.returncode == 0
    except subprocess.CalledProcessError:
        return False


def output_and_copy(text: str) -> None:
    """Print to stdout, copy to clipboard, and announce on stderr."""
    print(text)
    if copy_to_clipboard(text):
        print(f"\n✅ Copied to clipboard", file=sys.stderr)


def run(cmd: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run that uses text=True by default."""
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)
