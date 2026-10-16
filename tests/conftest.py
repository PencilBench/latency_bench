"""pytest 公共配置。

本机受限沙箱下有两个环境约束需要绕开：

1. 系统临时目录不可读写，因此把 pytest 的 ``basetemp`` 固定到仓库根；
2. 用 ``os.mkdir(path, mode=0o700)`` 建出来的目录随后无法列出（Windows 上显式
   传入安全属性时被拒绝），而 pytest 的 ``tmp_path`` 正好用这个模式。
   这里在收集阶段前把 ``Path.mkdir`` 的 ``mode`` 参数去掉，让它走继承权限。
"""

from __future__ import annotations

import os
import pathlib
import shutil

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

_ORIGINAL_MKDIR = pathlib.Path.mkdir


def _mkdir_inherit_permissions(self: pathlib.Path, mode: int = 0o777, **kwargs) -> None:
    _ORIGINAL_MKDIR(self, **kwargs)


def pytest_configure(config: pytest.Config) -> None:
    basetemp = config.option.basetemp
    if basetemp is not None:
        # 显式传入的 basetemp 由调用方负责，只确保它可列目录。
        pathlib.Path(basetemp).mkdir(parents=True, exist_ok=True)
        return
    base = ROOT / f".pytest-tmp-{os.getpid()}"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(base)


def pytest_sessionstart(session: pytest.Session) -> None:
    pathlib.Path.mkdir = _mkdir_inherit_permissions  # type: ignore[method-assign]


@pytest.fixture()
def work_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """可列出、可清理的临时目录（语义等价于 ``tmp_path``）。"""
    path = ROOT / f".pytest-work-{os.getpid()}-{abs(hash(str(tmp_path))) % 10**8}"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)
