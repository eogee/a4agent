"""win_toast_native 单测：XML 组装纯函数与 AUMID 注册。

COM 弹窗路径（RoGetActivationFactory/Show）依赖真机系统服务，
不进单测，由桌面端验收覆盖。
"""
import pytest

from backend.app import win_toast_native as n


def test_build_toast_xml_escapes_and_defaults():
    xml = n.build_toast_xml("标题<T&>", "正文")
    assert xml.startswith("<toast")
    assert "ToastGeneric" in xml
    # 默认有声（系统默认提示音）
    assert "ms-winsoundevent:Notification.Default" in xml
    # 正文不放 appLogoOverride 大圆标（用户决议）：来源区小 logo 由 AUMID 注册表渲染
    assert "appLogoOverride" not in xml and "<image" not in xml
    # 点击走协议激活（Win11 24H2 不向未打包进程派发 Activated 事件）
    assert 'activationType="protocol"' in xml
    assert f'launch="{n.WAKE_PROTOCOL}://wake"' in xml
    # 标题里的 XML 特殊字符必须转义
    assert "&lt;T&amp;&gt;" in xml


def test_build_toast_xml_silent():
    xml = n.build_toast_xml("t", "m", sound=False)
    assert 'silent="true"' in xml
    assert "ms-winsoundevent" not in xml


@pytest.mark.skipif(not n.AVAILABLE, reason="仅 Windows")
def test_ensure_aumid_registers():
    n.ensure_aumid()
    import winreg

    with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\AppUserModelId\{n.AUMID}") as key:
        # 来源名双写：默认值（老系统）+ DisplayName 值（Win11 24H2 实测只认这个）
        name, _ = winreg.QueryValueEx(key, "")
        display, _ = winreg.QueryValueEx(key, "DisplayName")
        icon, _ = winreg.QueryValueEx(key, "IconUri")
    assert name == n.DISPLAY_NAME and display == n.DISPLAY_NAME
    assert icon.endswith("logo_small.png")


@pytest.mark.skipif(not n.AVAILABLE, reason="仅 Windows")
def test_ensure_aumid_cleans_legacy_keys():
    """污染过的旧身份键由 ensure_aumid 兜底清理（v2 换名治愈缓存问题）。"""
    import winreg

    legacy = rf"Software\Classes\AppUserModelId\{n._LEGACY_AUMIDS[0]}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, legacy) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "stale")
    n.ensure_aumid()
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, legacy)
        raise AssertionError("旧身份键应已被清理")
    except OSError:
        pass  # 目标态：键不存在
