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
    # 默认有声（系统默认提示音）+ 正文圆形 logo
    assert "ms-winsoundevent:Notification.Default" in xml
    assert "appLogoOverride" in xml
    # 点击走协议激活（Win11 24H2 不向未打包进程派发 Activated 事件）
    assert 'activationType="protocol"' in xml
    assert f'launch="{n.WAKE_PROTOCOL}://wake"' in xml
    # 标题里的 XML 特殊字符必须转义
    assert "&lt;T&amp;&gt;" in xml


def test_build_toast_xml_no_logo_no_sound():
    xml = n.build_toast_xml("t", "m", logo=False, sound=False)
    assert "appLogoOverride" not in xml
    assert 'silent="true"' in xml


@pytest.mark.skipif(not n.AVAILABLE, reason="仅 Windows")
def test_ensure_aumid_registers():
    n.ensure_aumid()
    import winreg

    with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\AppUserModelId\{n.AUMID}") as key:
        name, _ = winreg.QueryValueEx(key, "")
    assert name == n.DISPLAY_NAME
