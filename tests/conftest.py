"""pytest 公共配置。

本机环境有两处约束：

1. 系统临时目录（``%TEMP%``）在受限沙箱下不可读写；
2. 用 ``os.mkdir(path, mode=0o700)`` 建出来的目录随后无法列出（Windows 上显式
   传入安全属性时被拒绝），而 pytest 内置的 ``tmp_path`` 正好用这个模式。

因此这里禁用 pytest 的 ``tmpdir`` 插件（它会在仓库根留下 ``.pytest-tmp-*``），
改用自带的 ``work_dir`` fixture 提供临时目录。
"""

from __future__ import annotations

import os
import pathlib
import shutil

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 每轮测试用一个独立目录，跑完删掉；所有用例共用同一个即可。
_WORK_DIR = ROOT / f".pytest-work-{os.getpid()}"


@pytest.fixture(scope="session")
def work_dir() -> pathlib.Path:
    """可列出、可清理的临时目录（替代 ``tmp_path``）。"""
    if _WORK_DIR.exists():
        shutil.rmtree(_WORK_DIR, ignore_errors=True)
    _WORK_DIR.mkdir(parents=True, exist_ok=True)
    yield _WORK_DIR
    shutil.rmtree(_WORK_DIR, ignore_errors=True)
