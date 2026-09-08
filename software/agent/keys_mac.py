"""Key presses and the Accessibility permission on macOS, through CoreGraphics via ctypes.

Why not AppleScript "System Events": that routes through the osascript helper, which macOS
will not grant Accessibility to by itself. Posting the events from this process means the
permission belongs to the agent, macOS can prompt for it, and the prompt names the agent.
"""
import ctypes
import os
import select
import threading
import time

# When started by the "PC Stats Panel" launcher, it hands us two pipe descriptors. Everything
# permission-related goes through the launcher, because that executable is what macOS trusts.
_REQ = os.environ.get("PCSTATS_KEY_REQ")
_REP = os.environ.get("PCSTATS_KEY_REP")
_lock = threading.Lock()


def via_launcher():
    return bool(_REQ and _REP)


def _ask(line, timeout=8.0):
    """One request line to the launcher, one reply line back."""
    with _lock:
        os.write(int(_REQ), (line + "\n").encode())
        buf = b""
        deadline = time.time() + timeout
        while not buf.endswith(b"\n"):
            ready, _, _ = select.select([int(_REP)], [], [], max(0.0, deadline - time.time()))
            if not ready:
                raise RuntimeError("the launcher did not answer")
            chunk = os.read(int(_REP), 256)
            if not chunk:
                raise RuntimeError("the launcher went away")
            buf += chunk
        return buf.decode().strip()

_CG = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_AS = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")

_CG.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
_CG.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
_CG.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_CG.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_CF.CFRelease.argtypes = [ctypes.c_void_p]
_CF.CFDictionaryCreate.restype = ctypes.c_void_p
_CF.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
_AS.AXIsProcessTrusted.restype = ctypes.c_bool
_AS.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
_AS.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]

KCG_HID_EVENT_TAP = 0

# Virtual key codes for a US layout (kVK_*), plus the names the dashboard uses.
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36,
    "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "tab": 48, "space": 49, "`": 50, "delete": 51, "backspace": 51, "esc": 53, "escape": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80, "f20": 90,
}
MODIFIER_FLAGS = {"cmd": 1 << 20, "command": 1 << 20, "shift": 1 << 17, "alt": 1 << 19, "option": 1 << 19,
                  "ctrl": 1 << 18, "control": 1 << 18, "fn": 1 << 23}
MODIFIER_KEYCODES = {"cmd": 55, "command": 55, "shift": 56, "alt": 58, "option": 58, "ctrl": 59, "control": 59, "fn": 63}
MODIFIERS = set(MODIFIER_FLAGS)

# The flag bits a real Apple keyboard puts on these keys. macOS's own shortcuts (Spaces, Mission
# Control, Show Desktop) are matched with them, so a ⌃→ posted without the fn bit does nothing.
FN, NUMPAD = 1 << 23, 1 << 21
HARDWARE_FLAGS = {code: FN for code in (123, 124, 125, 126)}                     # arrows (the keypad bit breaks the match)
HARDWARE_FLAGS.update({code: FN for code in (114, 115, 116, 117, 119, 121)})     # help, home, page up, fwd delete, end, page down
HARDWARE_FLAGS.update({code: FN for name, code in KEYCODES.items() if name[:1] == "f" and name[1:].isdigit()})


def ax_state():
    """'trusted', 'untrusted', or 'restart' (granted after launch: the launcher must restart)."""
    if via_launcher():
        try:
            answer = _ask("ax")
        except RuntimeError:
            return "untrusted"
        return {"1": "trusted", "restart": "restart"}.get(answer, "untrusted")
    return "trusted" if bool(_AS.AXIsProcessTrusted()) else "untrusted"


def ax_trusted(prompt=False):
    """Is the agent allowed to control the computer? With prompt=True macOS shows its
    'allow in Accessibility settings' dialog and lists the app there, ready to be switched on."""
    if via_launcher():
        try:
            return _ask("prompt" if prompt else "ax") == "1"
        except RuntimeError:
            return False
    if not prompt:
        return bool(_AS.AXIsProcessTrusted())
    key = ctypes.c_void_p.in_dll(_AS, "kAXTrustedCheckOptionPrompt")
    yes = ctypes.c_void_p.in_dll(_CF, "kCFBooleanTrue")
    keys = (ctypes.c_void_p * 1)(key.value)
    vals = (ctypes.c_void_p * 1)(yes.value)
    kcb = ctypes.addressof(ctypes.c_char.in_dll(_CF, "kCFTypeDictionaryKeyCallBacks"))
    vcb = ctypes.addressof(ctypes.c_char.in_dll(_CF, "kCFTypeDictionaryValueCallBacks"))
    opts = _CF.CFDictionaryCreate(None, keys, vals, 1, kcb, vcb)
    try:
        return bool(_AS.AXIsProcessTrustedWithOptions(opts))
    finally:
        if opts:
            _CF.CFRelease(opts)


def parse(keys):
    """['cmd','shift','4'] -> (flags, [modifier keycodes], keycode). Raises ValueError."""
    keys = [str(k).lower() for k in keys]
    mods = [k for k in keys if k in MODIFIERS]
    rest = [k for k in keys if k not in MODIFIERS]
    if not rest:
        raise ValueError("a key combination needs a key besides the modifiers")
    key = rest[-1]
    if key not in KEYCODES:
        raise ValueError(f"unknown key {key!r}")
    flags = 0
    for m in mods:
        flags |= MODIFIER_FLAGS[m]
    return flags, [MODIFIER_KEYCODES[m] for m in mods], KEYCODES[key]


def _post(keycode, down, flags):
    ev = _CG.CGEventCreateKeyboardEvent(None, keycode, down)
    if not ev:
        raise OSError("could not create keyboard event")
    _CG.CGEventSetFlags(ev, flags)
    _CG.CGEventPost(KCG_HID_EVENT_TAP, ev)
    _CF.CFRelease(ev)


def event_sequence(keys):
    """The events a keyboard would produce: modifiers down, key down/up, modifiers up.
    Arrow and function keys get the fn modifier pressed around them: macOS matches its own shortcuts
    (Spaces, Mission Control, Show Desktop) on the fn state, so ⌃→ without it does nothing."""
    flags, mod_codes, keycode = parse(keys)
    mod_names = [k for k in (str(x).lower() for x in keys) if k in MODIFIERS]
    if HARDWARE_FLAGS.get(keycode, 0) & FN and "fn" not in mod_names:
        mod_names.insert(0, "fn")
        mod_codes.insert(0, MODIFIER_KEYCODES["fn"])
        flags |= FN
    events, held = [], 0
    for code, name in zip(mod_codes, mod_names):
        held |= MODIFIER_FLAGS[name]
        events.append((code, True, held))
    events += [(keycode, True, flags), (keycode, False, flags)]
    for code, name in reversed(list(zip(mod_codes, mod_names))):
        held &= ~MODIFIER_FLAGS[name]
        events.append((code, False, held))
    return events


def post_hotkey(keys, delay=0.012):
    """Press a combination. Through the launcher when it started us, else from this process."""
    events = event_sequence(keys)
    if not ax_trusted():
        raise PermissionError("not allowed to control the computer yet")
    if via_launcher():
        answer = _ask("events " + ",".join(f"{c}:{int(d)}:{f}" for c, d, f in events))
        if answer != "ok":
            raise RuntimeError(answer.replace("err ", "", 1) or "launcher refused")
        return
    for code, down, flags in events:
        _post(code, down, flags)
        time.sleep(delay)


def describe(keys):
    nice = {"cmd": "⌘", "command": "⌘", "shift": "⇧", "alt": "⌥", "option": "⌥", "ctrl": "⌃", "control": "⌃",
            "left": "←", "right": "→", "up": "↑", "down": "↓", "space": "Space", "return": "↩", "enter": "↩", "tab": "⇥", "esc": "Esc"}
    return " ".join(nice.get(str(k).lower(), str(k).upper()) for k in keys)
