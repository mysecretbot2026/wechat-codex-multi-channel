"""Invoke a running macOS app's update menu through native Accessibility.

No System Events automation, shell injection, or private Electron IPC is used.
"""

import ctypes
import plistlib
import subprocess
import sys
from pathlib import Path


DEFAULT_UPDATE_MENU_TITLES = ["Check for Updates", "检查更新", "检查更新项", "檢查更新", "檢查更新項目"]


class MacAccessibility:
    def __init__(self):
        self.ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.owned = []
        ref = ctypes.c_void_p
        self.ax.AXIsProcessTrusted.argtypes = []
        self.ax.AXIsProcessTrusted.restype = ctypes.c_bool
        self.ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        self.ax.AXUIElementCreateApplication.restype = ref
        self.ax.AXUIElementCopyAttributeValue.argtypes = [ref, ref, ctypes.POINTER(ref)]
        self.ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int
        self.ax.AXUIElementPerformAction.argtypes = [ref, ref]
        self.ax.AXUIElementPerformAction.restype = ctypes.c_int
        self.ax.AXUIElementSetMessagingTimeout.argtypes = [ref, ctypes.c_float]
        self.ax.AXUIElementSetMessagingTimeout.restype = ctypes.c_int
        self.cf.CFStringCreateWithCString.argtypes = [ref, ctypes.c_char_p, ctypes.c_uint32]
        self.cf.CFStringCreateWithCString.restype = ref
        self.cf.CFStringGetCString.argtypes = [ref, ctypes.c_void_p, ctypes.c_long, ctypes.c_uint32]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFRelease.argtypes = [ref]
        self.cf.CFRelease.restype = None
        self.cf.CFGetTypeID.argtypes = [ref]
        self.cf.CFGetTypeID.restype = ctypes.c_ulong
        for name in ["CFArrayGetTypeID", "CFStringGetTypeID", "CFBooleanGetTypeID"]:
            function = getattr(self.cf, name)
            function.argtypes = []
            function.restype = ctypes.c_ulong
        self.cf.CFArrayGetCount.argtypes = [ref]
        self.cf.CFArrayGetCount.restype = ctypes.c_long
        self.cf.CFArrayGetValueAtIndex.argtypes = [ref, ctypes.c_long]
        self.cf.CFArrayGetValueAtIndex.restype = ref
        self.cf.CFBooleanGetValue.argtypes = [ref]
        self.cf.CFBooleanGetValue.restype = ctypes.c_bool

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for value in reversed(self.owned):
            self.cf.CFRelease(value)

    def trusted(self):
        return self.ax.AXIsProcessTrusted()

    def application(self, pid):
        result = self.ax.AXUIElementCreateApplication(pid)
        if not result:
            raise RuntimeError("无法连接桌面 App 的辅助功能接口。")
        self.owned.append(result)
        self.ax.AXUIElementSetMessagingTimeout(result, 3.0)
        return result

    def attribute(self, element, name):
        key = self.cf.CFStringCreateWithCString(None, name.encode(), 0x08000100)
        try:
            value = ctypes.c_void_p()
            error = self.ax.AXUIElementCopyAttributeValue(element, key, ctypes.byref(value))
            if error or not value.value:
                return None
            self.owned.append(value.value)
            return value.value
        finally:
            self.cf.CFRelease(key)

    def children(self, element):
        value = self.attribute(element, "AXChildren")
        if not value or self.cf.CFGetTypeID(value) != self.cf.CFArrayGetTypeID():
            return []
        return [self.cf.CFArrayGetValueAtIndex(value, i)
                for i in range(min(100, self.cf.CFArrayGetCount(value)))]

    def title(self, element):
        value = self.attribute(element, "AXTitle")
        if not value or self.cf.CFGetTypeID(value) != self.cf.CFStringGetTypeID():
            return ""
        buffer = ctypes.create_string_buffer(4096)
        if not self.cf.CFStringGetCString(value, buffer, len(buffer), 0x08000100):
            return ""
        return buffer.value.decode("utf-8")

    def enabled(self, element):
        value = self.attribute(element, "AXEnabled")
        if not value or self.cf.CFGetTypeID(value) != self.cf.CFBooleanGetTypeID():
            return False
        return self.cf.CFBooleanGetValue(value)

    def press(self, element):
        action = self.cf.CFStringCreateWithCString(None, b"AXPress", 0x08000100)
        try:
            error = self.ax.AXUIElementPerformAction(element, action)
        finally:
            self.cf.CFRelease(action)
        if error:
            raise RuntimeError(f"无法触发内置更新菜单（macOS AX 错误 {error}）。请在 App 菜单手动选择“检查更新”。")


def _menu_title(value):
    return value.strip().rstrip(" .…").casefold()


def _find_update_item(api, element, titles, depth=0):
    if _menu_title(api.title(element)) in titles:
        return element
    if depth < 3:
        for child in api.children(element):
            found = _find_update_item(api, child, titles, depth + 1)
            if found is not None:
                return found
    return None


def trigger_builtin_update(app_path, bundle_id, menu_titles=None):
    if sys.platform != "darwin":
        raise RuntimeError("内置更新菜单目前支持 macOS。")
    if menu_titles is not None and (not isinstance(menu_titles, list) or
                                   any(not isinstance(title, str) or not title.strip() for title in menu_titles)):
        raise ValueError("updates.desktopUpdateMenuTitles 必须是非空字符串组成的数组。")
    app = Path(app_path)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != bundle_id:
        raise RuntimeError("应用标识不匹配，未触发内置更新。")
    executable = str(app / "Contents/MacOS" / info["CFBundleExecutable"])
    processes = subprocess.run(["/bin/ps", "-axo", "pid=,command="], check=True,
                               capture_output=True, text=True, timeout=10).stdout
    pid = None
    for line in processes.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and (parts[1] == executable or parts[1].startswith(executable + " ")):
            pid = int(parts[0])
            break
    if pid is None:
        raise RuntimeError("桌面 App 未运行。请先打开 App，再触发内置更新。")
    titles = {_menu_title(title) for title in (menu_titles or DEFAULT_UPDATE_MENU_TITLES)}
    with MacAccessibility() as api:
        if not api.trusted():
            raise RuntimeError(
                "触发 App 内置更新需要 macOS 辅助功能权限。\n"
                "请在“系统设置 → 隐私与安全性 → 辅助功能”中授权运行服务的 Python 或终端，之后重启微信服务。\n"
                f"当前 Python：{sys.executable}\n"
                "也可直接在 ChatGPT / Codex App 菜单中选择“检查更新”，无需先退出应用。"
            )
        root = api.application(pid)
        menu_bar = api.attribute(root, "AXMenuBar")
        if menu_bar is None:
            raise RuntimeError("无法读取桌面 App 菜单栏，请确认辅助功能权限和 App 状态。")
        for menu in api.children(menu_bar):
            item = _find_update_item(api, menu, titles)
            if item is not None:
                if not api.enabled(item):
                    raise RuntimeError("App 的“检查更新”菜单当前不可用，请检查应用的更新策略或稍后重试。")
                # Open only the menu containing the exact update item.
                api.press(menu)
                api.press(item)
                return api.title(item)
    raise RuntimeError("未找到 App 的“检查更新”菜单。其他语言可配置 updates.desktopUpdateMenuTitles，或在 App 内手动检查更新。")
