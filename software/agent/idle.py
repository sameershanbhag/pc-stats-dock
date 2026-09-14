"""How long the Mac has been left alone, and whether the screen is locked. CoreGraphics, in-process, no helpers."""
import ctypes
import ctypes.util
import re
import subprocess

_cg = None
_cf = None


def _libs():
    global _cg, _cf
    if _cg is None:
        _cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        _cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        _cg.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int, ctypes.c_uint32]
        _cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
        _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        _cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        _cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        _cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _cf.CFBooleanGetValue.restype = ctypes.c_bool
        _cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        _cf.CFRelease.argtypes = [ctypes.c_void_p]
    return _cg, _cf


def seconds_idle():
    """Seconds since the last keyboard, mouse or touch event in this login session, or None."""
    try:
        cg, _ = _libs()
        return float(cg.CGEventSourceSecondsSinceLastEventType(0, 0xFFFFFFFF))     # combined session state, any input
    except Exception:
        return None


def screen_locked():
    """True when the lock screen is up, False when not, None when unknown."""
    try:
        cg, cf = _libs()
        d = cg.CGSessionCopyCurrentDictionary()
        if not d:
            return None
        key = cf.CFStringCreateWithCString(None, b"CGSSessionScreenIsLocked", 0x08000100)
        try:
            v = cf.CFDictionaryGetValue(d, key)
            return bool(v) and bool(cf.CFBooleanGetValue(v))
        finally:
            cf.CFRelease(key)
            cf.CFRelease(d)
    except Exception:
        return None


DISPLAY_HOLD = re.compile(r"^\s*pid \d+\((?P<app>[^)]*)\): \[[^\]]*\] [\d:]+ (?:PreventUserIdleDisplaySleep|NoDisplaySleepAssertion)"
                          r"(?: named: \"(?P<why>[^\"]*)\")?", re.M)


def parse_display_holders(text):
    """Processes asking macOS to keep the display awake, from `pmset -g assertions`: [{app, why}]."""
    return [{"app": m.group("app"), "why": m.group("why") or ""} for m in DISPLAY_HOLD.finditer(text)]


def display_awake_holder():
    """The app that is keeping the display awake (a video is playing, a call is on), or None. ~40 ms; cache it."""
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "assertions"], capture_output=True, timeout=5).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None
    holders = parse_display_holders(out)
    return holders[0] if holders else None
