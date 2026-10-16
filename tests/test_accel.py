"""``latency_bench.accel`` 的单元测试（不依赖真实硬件）。"""

from __future__ import annotations

import pytest

from latency_bench import accel


# --------------------------------------------------------------------------------------
# 回读检测
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("d3d11", False),
        ("d3d11va", False),
        ("d3d12", False),
        ("videotoolbox", False),
        ("videotoolbox_vld", False),
        ("cuda", False),
        ("vaapi", False),
        ("yuv420p", True),
        ("yuv420p10le", True),
        ("nv12", True),
        ("rgb0", True),
        ("gray10le", True),
        ("p010le", True),
        (None, True),
        ("", True),
    ],
)
def test_is_software_pix_fmt(name, expected):
    assert accel.is_software_pix_fmt(name) is expected


# --------------------------------------------------------------------------------------
# 平台映射与参数校验
# --------------------------------------------------------------------------------------


def test_platform_default_mapping():
    assert accel.platform_default_hwaccel("win32") == "d3d11va"
    assert accel.platform_default_hwaccel("darwin") == "videotoolbox"
    with pytest.raises(accel.HardwareUnavailable):
        accel.platform_default_hwaccel("linux")


def test_allowed_hwaccels_excludes_qsv():
    allowed = accel.allowed_hwaccels(platform="win32")
    assert allowed == ("d3d11va",)
    assert "qsv" not in allowed
    assert "d3d12va" not in allowed
    assert "dxva2" not in allowed


def test_allowed_hwaccels_with_alt():
    allowed = accel.allowed_hwaccels(allow_alt=True, platform="win32")
    assert allowed[0] == "d3d11va"
    assert "cuda" in allowed
    assert "qsv" not in allowed


def test_validate_hwaccel_rejects_qsv(monkeypatch):
    monkeypatch.setattr(accel, "device_type_available", lambda name: True)
    with pytest.raises(accel.AccelError) as excinfo:
        accel.validate_hwaccel("qsv", platform="win32")
    message = str(excinfo.value)
    assert "qsv" in message
    assert "d3d11va" in message


def test_validate_hwaccel_rejects_videotoolbox_on_windows(monkeypatch):
    monkeypatch.setattr(accel, "device_type_available", lambda name: True)
    with pytest.raises(accel.AccelError):
        accel.validate_hwaccel("videotoolbox", platform="win32")


def test_validate_hwaccel_cuda_needs_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(accel, "device_type_available", lambda name: True)
    with pytest.raises(accel.AccelError) as excinfo:
        accel.validate_hwaccel("cuda", platform="win32")
    assert "--allow-alt-hwaccel" in str(excinfo.value)
    assert accel.validate_hwaccel("cuda", allow_alt=True, platform="win32") == "cuda"


def test_validate_hwaccel_default_and_missing_device(monkeypatch):
    monkeypatch.setattr(accel, "device_type_available", lambda name: True)
    assert accel.validate_hwaccel(None, platform="win32") == "d3d11va"
    assert accel.validate_hwaccel("auto", platform="win32") == "d3d11va"

    monkeypatch.setattr(accel, "device_type_available", lambda name: False)
    with pytest.raises(accel.HardwareUnavailable):
        accel.validate_hwaccel("d3d11va", platform="win32")


# --------------------------------------------------------------------------------------
# 解码器候选
# --------------------------------------------------------------------------------------


def test_decoder_candidates_prefer_native():
    assert accel.decoder_candidates("av1", "d3d11va")[0] == "av1"
    assert accel.decoder_candidates("hevc", "d3d11va")[0] == "hevc"
    assert accel.decoder_candidates("av1", "cuda")[0] == "av1_cuvid"
    # 末项固定是 None（让容器自己选），便于把「容器默认解码器」的失败原因也记下来
    assert accel.decoder_candidates("hevc", "d3d11va")[-1] is None
    # 未知编解码器：只能交给容器
    assert accel.decoder_candidates("theora", "d3d11va") == (None,)
    # 已知编解码器但设备类型没配：也交给容器
    assert accel.decoder_candidates("vp9", "d3d12va") == (None,)


def test_plan_for_codec_auto_and_explicit():
    plan = accel.plan_for_codec("hevc", "d3d11va")
    assert plan.decoder == "hevc"
    assert plan.label == "hevc+d3d11va"

    plan = accel.plan_for_codec("hevc", "d3d11va", requested_decoder="hevc_qsv")
    assert plan.decoder == "hevc_qsv"


def test_plan_for_codec_rejects_software_decoder():
    with pytest.raises(accel.AccelError) as excinfo:
        accel.plan_for_codec("av1", "d3d11va", requested_decoder="libdav1d")
    assert "libdav1d" in str(excinfo.value)
    # 打开软解开关后允许（仅用于排查问题）
    plan = accel.plan_for_codec(
        "av1", "d3d11va", requested_decoder="libdav1d", force_hardware=False
    )
    assert plan.decoder == "libdav1d"


def test_make_hwaccel_defaults():
    hw = accel.make_hwaccel("d3d11va")
    assert hw.allow_software_fallback is False
    assert hw.is_hw_owned is True


def test_describe_environment_shape():
    env = accel.describe_environment()
    for key in ("platform", "python", "pyav", "hwdevices_available", "excluded_hwaccels"):
        assert key in env
    assert "qsv" in env["excluded_hwaccels"]


def test_format_attempts_renders_decoder_names():
    text = accel.format_attempts(
        [
            accel.PlanAttempt("d3d11va", "av1", "EINVAL"),
            accel.PlanAttempt("d3d11va", None, "no config"),
        ]
    )
    assert "av1 + d3d11va: EINVAL" in text
    assert "（容器默认）" in text
