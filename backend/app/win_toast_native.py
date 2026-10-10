"""WinRT 原生 Toast：ctypes 直调 COM，零第三方依赖。

为什么换掉 Shell_NotifyIconW 气泡（legacy 路径保留作降级）：
  - legacy 气泡在 Win11 被转换为 toast 外观渲染，但声音、来源区应用名与图标
    均不受应用控制（无声、不显示 a4agent/logo）；
  - legacy 的消息泵有崩溃隐患：wndproc 回调在泵重启时被 GC 成野指针，实测
    崩掉整个进程（2026-10-10）。原生 toast 由系统渲染，无消息泵、无回调生命周期问题。

接口槽位以 Windows SDK 10.0.19041 的 MIDL 头为准（windows.ui.notifications.h /
windows.data.xml.dom.h），方法顺序即 vtable 顺序；Ro* / WindowsCreateString 走 combase.dll。

实现要点：
  - XML 全量字符串 + IXmlDocumentIO.LoadXml，不触碰 IXmlDocument 深层 vtable；
  - 来源身份靠 AUMID（HKCU\\Software\\Classes\\AppUserModelId\\eogee.a4agent，
    DisplayName + IconUri 写注册表即生效，与进程是否打包无关）；
  - 点击唤醒走协议激活：activationType="protocol" + launch="a4agent://wake"，
    协议处理器指向 desktop.py --toast-wake（见 ensure_aumid 注册）。不订阅
    Toast.Activated 事件——实测 Win11 24H2 的通知平台不向未打包进程派发
    Activated/Dismissed（订阅 token 有效、QI 观测系统从不回调），协议激活
    是系统级标准路径，无此依赖；
  - RoInitialize 每次调用（MTA 重复调用返回 S_FALSE 无害），适配线程池多线程；
  - 任何 HRESULT 失败抛 ToastError，由 win_toast.show 门面捕获降级气泡。
"""
import logging
import sys
from pathlib import Path
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

AVAILABLE = sys.platform == "win32"

AUMID = "eogee.a4agent.v2"
# v1（eogee.a4agent）身份被系统通知平台缓存污染：首次注册时 IconUri 还是 512px
# 原图，来源区渲染失败 → 系统把坏身份永久缓存（写对 DisplayName/IconUri、重启
# 通知宿主都不回读，实测 Win11 24H2），来源区只能显示原始 AUMID 串。换新身份
# 即时治愈（新身份首次入册即读当前注册表）；旧键由 ensure_aumid 兜底清理。
_LEGACY_AUMIDS = ("eogee.a4agent",)
DISPLAY_NAME = "a4agent"
WAKE_PROTOCOL = "a4agent"  # 点击横幅时系统打开 a4agent://wake（见 desktop.py --toast-wake）

# 接口 IID（SDK MIDL 头逐一核对）
IID_ITOAST_NOTIFICATION_MANAGER_STATICS = "50ac103f-d235-4598-bbef-98fe4d1a3ad4"
IID_ITOAST_NOTIFICATION_FACTORY = "04124b20-82c6-4229-b109-fd9ed4662b53"
IID_IXML_DOCUMENT = "f7f3a506-1e87-42d6-bcfb-b8c809fa5494"
IID_IXML_DOCUMENT_IO = "6cd0e74e-ee65-4489-9ebf-ca43e87ba637"

CLS_TOAST_MANAGER = "Windows.UI.Notifications.ToastNotificationManager"
CLS_TOAST_FACTORY = "Windows.UI.Notifications.ToastNotification"
CLS_XML_DOCUMENT = "Windows.Data.Xml.Dom.XmlDocument"


def resources_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / "resources"
    return Path(__file__).resolve().parent.parent.parent / "resources"


def _small_logo_path() -> Path:
    """来源区小图标：64px 缩版。512px 原图实测在来源区槽位渲染不出来。"""
    return resources_dir() / "logo_small.png"


def ensure_aumid() -> None:
    """注册来源身份与点击协议（幂等）：来源区显示小 logo + a4agent，
    点击横幅时系统打开 a4agent://wake → desktop.py --toast-wake 唤回主窗口。"""
    if not AVAILABLE:
        return
    import winreg

    icon = str(_small_logo_path().resolve())
    with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\AppUserModelId\{AUMID}") as key:
        # 来源应用名：Win11 24H2 实测只认 DisplayName 值（不写就整串显示原始
        # AUMID「eogee.a4agent」），老系统认键默认值——两处都写，幂等。
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, DISPLAY_NAME)
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, DISPLAY_NAME)
        try:
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, icon)
        except OSError:
            pass  # IconUri 写不上不影响身份显示，仅少来源小图标

    # 旧身份键清理：身份缓存污染过，留着只会让降级到旧版的场景继续难看
    for legacy in _LEGACY_AUMIDS:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                             rf"Software\Classes\AppUserModelId\{legacy}")
        except OSError:
            pass  # 键不存在即目标态

    # 点击协议：URL Protocol 惯例键 + open 命令。命令按运行形态生成
    # （frozen: a4agent.exe --toast-wake；dev: python desktop.py --toast-wake）。
    # dev 态 desktop.py 用模块路径推导而非 sys.argv[0]——后者受运行方式影响
    # （python -c / 其它入口时 argv[0] 不是 desktop.py）。
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}" --toast-wake'
    else:
        entry = Path(__file__).resolve().parents[2] / "desktop.py"
        cmd = f'"{sys.executable}" "{entry}" --toast-wake'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{WAKE_PROTOCOL}") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"URL:{DISPLAY_NAME}")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\{WAKE_PROTOCOL}\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, cmd)


def build_toast_xml(title: str, message: str, sound: bool = True) -> str:
    """ToastGeneric 全量 XML：系统默认提示音 + 协议激活（点击唤回主窗口）。

    不放正文大图标（appLogoOverride 圆形 logo 过大，用户决议去掉）；来源区
    的小 logo + 应用名由系统按 AUMID 注册表（DisplayName/IconUri）渲染。
    纯函数便于测试。
    """
    audio = ('<audio src="ms-winsoundevent:Notification.Default"/>'
             if sound else '<audio silent="true"/>')
    return (
        f'<toast activationType="protocol" launch="{WAKE_PROTOCOL}://wake" duration="short">'
        "<visual><binding template=\"ToastGeneric\">"
        f"<text>{escape(title)}</text><text>{escape(message)}</text>"
        f"</binding></visual>{audio}</toast>"
    )


class ToastError(Exception):
    """原生 toast 链路失败（HRESULT/detail 记在消息里），调用方据此降级。"""


if AVAILABLE:
    import ctypes
    import threading
    from ctypes import POINTER, WINFUNCTYPE, byref, c_int32, c_int64
    from ctypes import c_uint32, c_void_p, cast
    from ctypes import wintypes

    HRESULT = ctypes.c_long  # Win32 HRESULT：32 位有符号状态码

    _combase = ctypes.WinDLL("combase.dll")

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("data1", wintypes.DWORD), ("data2", wintypes.WORD),
            ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8),
        ]

    def _guid_from_str(s: str) -> _GUID:
        b = __import__("uuid").UUID(s).bytes_le
        g = _GUID()
        g.data1 = int.from_bytes(b[0:4], "little")
        g.data2 = int.from_bytes(b[4:6], "little")
        g.data3 = int.from_bytes(b[6:8], "little")
        g.data4 = (ctypes.c_ubyte * 8)(*b[8:16])
        return g

    _combase.RoInitialize.argtypes = [c_int32]
    _combase.RoInitialize.restype = HRESULT
    _combase.RoGetActivationFactory.argtypes = [c_void_p, POINTER(_GUID), POINTER(c_void_p)]
    _combase.RoGetActivationFactory.restype = HRESULT
    _combase.RoActivateInstance.argtypes = [c_void_p, POINTER(c_void_p)]
    _combase.RoActivateInstance.restype = HRESULT
    _combase.WindowsCreateString.argtypes = [wintypes.LPCWSTR, c_uint32, POINTER(c_void_p)]
    _combase.WindowsCreateString.restype = HRESULT
    _combase.WindowsDeleteString.argtypes = [c_void_p]
    _combase.WindowsDeleteString.restype = HRESULT

    RO_INIT_MULTITHREADED = 1
    S_OK, S_FALSE, RPC_E_CHANGED_MODE = 0, 1, -2147417850

    _ro_init_lock = threading.Lock()

    def _ensure_ro_initialized() -> None:
        """MTA 初始化：重复调用 S_FALSE、跨模式 RPC_E_CHANGED_MODE 均可直接用。"""
        hr = _combase.RoInitialize(RO_INIT_MULTITHREADED)
        if hr not in (S_OK, S_FALSE, RPC_E_CHANGED_MODE):
            raise ToastError(f"RoInitialize 失败：HRESULT {hr & 0xFFFFFFFF:08X}")

    def _hstring(s: str) -> c_void_p:
        h = c_void_p()
        hr = _combase.WindowsCreateString(s, len(s), byref(h))
        if hr != S_OK:
            raise ToastError(f"WindowsCreateString 失败：HRESULT {hr & 0xFFFFFFFF:08X}")
        return h

    def _free_hstring(h: c_void_p) -> None:
        if h:
            _combase.WindowsDeleteString(h)

    # ---------------- vtable 调用 ----------------
    # IInspectable 前 6 槽（QI/AddRef/Release/GetIids/GetRuntimeClassName/GetTrustLevel），
    # 自有方法从槽 6 起，与 SDK MIDL 头逐槽核对。
    _FT_QI = WINFUNCTYPE(HRESULT, c_void_p, POINTER(_GUID), POINTER(c_void_p))
    _FT_REF = WINFUNCTYPE(c_uint32, c_void_p)
    _FT_LOAD_XML = WINFUNCTYPE(HRESULT, c_void_p, c_void_p)            # LoadXml(HSTRING)
    _FT_FACTORY_CREATE = WINFUNCTYPE(HRESULT, c_void_p, c_void_p, POINTER(c_void_p))
    _FT_CREATE_NOTIFIER = WINFUNCTYPE(HRESULT, c_void_p, c_void_p, POINTER(c_void_p))
    _FT_SHOW = WINFUNCTYPE(HRESULT, c_void_p, c_void_p)

    def _vtable_fn(intf: c_void_p, slot: int, ftype):
        vtbl = cast(intf, POINTER(c_void_p))[0]
        return ftype(cast(vtbl, POINTER(c_void_p))[slot])

    def _hr_hex(hr: int) -> str:
        return f"{hr & 0xFFFFFFFF:08X}"

    def _qi(intf: c_void_p, iid_str: str) -> c_void_p:
        out = c_void_p()
        iid = _guid_from_str(iid_str)
        fn = _vtable_fn(intf, 0, _FT_QI)
        hr = fn(intf, byref(iid), byref(out))
        if hr != S_OK or not out:
            raise ToastError(f"QueryInterface({iid_str}) 失败：HRESULT {_hr_hex(hr)}")
        return out

    def _release(intf: c_void_p) -> None:
        if intf:
            _vtable_fn(intf, 2, _FT_REF)(intf)

    def _get_activation_factory(class_id: str, iid_str: str) -> c_void_p:
        h = _hstring(class_id)
        try:
            iid = _guid_from_str(iid_str)
            factory = c_void_p()
            hr = _combase.RoGetActivationFactory(h, byref(iid), byref(factory))
            if hr != S_OK or not factory:
                raise ToastError(
                    f"RoGetActivationFactory({class_id}) 失败：HRESULT {_hr_hex(hr)}")
            return factory
        finally:
            _free_hstring(h)

    def _ro_activate_instance(class_id: str) -> c_void_p:
        h = _hstring(class_id)
        try:
            insp = c_void_p()
            hr = _combase.RoActivateInstance(h, byref(insp))
            if hr != S_OK or not insp:
                raise ToastError(
                    f"RoActivateInstance({class_id}) 失败：HRESULT {_hr_hex(hr)}")
            return insp
        finally:
            _free_hstring(h)

    def show(title: str, message: str, on_click=None) -> tuple[bool, str]:
        """发一条原生 toast；返回 (成功, 失败原因)。失败由门面降级气泡。

        on_click 参数为兼容既有调用方保留：点击唤回由协议激活统一实现
        （launch="a4agent://wake" → desktop.py --toast-wake），此参数被忽略。
        """
        mgr = doc_ins = doc = doc_io = factory = notifier = toast = None
        xml_h = None
        try:
            _ensure_ro_initialized()
            ensure_aumid()

            mgr = _get_activation_factory(CLS_TOAST_MANAGER,
                                          IID_ITOAST_NOTIFICATION_MANAGER_STATICS)
            doc_ins = _ro_activate_instance(CLS_XML_DOCUMENT)
            doc = _qi(doc_ins, IID_IXML_DOCUMENT)        # CreateToastNotification 传参
            doc_io = _qi(doc_ins, IID_IXML_DOCUMENT_IO)  # LoadXml 调用
            factory = _get_activation_factory(
                CLS_TOAST_FACTORY, IID_ITOAST_NOTIFICATION_FACTORY)

            xml_h = _hstring(build_toast_xml(title, message))
            hr = _vtable_fn(doc_io, 6, _FT_LOAD_XML)(doc_io, xml_h)
            if hr != S_OK:
                raise ToastError(f"LoadXml 失败：HRESULT {_hr_hex(hr)}")

            toast = c_void_p()
            hr = _vtable_fn(factory, 6, _FT_FACTORY_CREATE)(
                factory, doc, byref(toast))
            if hr != S_OK or not toast:
                raise ToastError(f"CreateToastNotification 失败：HRESULT {_hr_hex(hr)}")

            notifier = c_void_p()
            aumid_h = _hstring(AUMID)
            try:
                hr = _vtable_fn(mgr, 7, _FT_CREATE_NOTIFIER)(
                    mgr, aumid_h, byref(notifier))
            finally:
                _free_hstring(aumid_h)
            if hr != S_OK or not notifier:
                raise ToastError(f"CreateToastNotifierWithId 失败：HRESULT {_hr_hex(hr)}")

            hr = _vtable_fn(notifier, 6, _FT_SHOW)(notifier, toast)
            if hr != S_OK:
                raise ToastError(f"Show 失败：HRESULT {_hr_hex(hr)}")
            return True, ""
        except ToastError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - 任何异常都降级，不拖累调用方
            logger.exception("原生 toast 异常")
            return False, str(exc)
        finally:
            _free_hstring(xml_h)
            for intf in (notifier, toast, factory, doc_io, doc, doc_ins, mgr):
                _release(intf)

else:

    def show(title: str, message: str, on_click=None) -> tuple[bool, str]:
        """非 Windows 占位：永远失败，由门面降级。"""
        return False, "仅 Windows 支持原生 toast"
