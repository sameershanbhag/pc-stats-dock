/* PC Stats Panel launcher, key service and touch mapper.
 *
 * The app's own executable. It starts the Python agent as a child, waits for it, and answers
 * requests from it over a pipe:
 *   "ax"                     -> "1", "0", or "restart" (granted after we started; the process must restart to use it)
 *   "prompt"                 -> "1" or "0", showing macOS's Accessibility dialog if not granted
 *   "events c:d:f,c:d:f,..." -> "ok": post keyboard events (keycode:down:flags) in order
 * macOS grants Accessibility to *this* executable ("PC Stats Panel"), so the key presses and
 * the permission check must happen here, not in python3.
 *
 * "--touch-map W H" runs the touch mapper instead: macOS aims a USB touchscreen at the main
 * display, so while the WxH panel is not the main display an event tap (Accessibility again)
 * relocates the touchscreen's mouse events onto the panel, makes the app under a finger active
 * before the tap lands (a first click on an inactive window only activates it) and returns the
 * pointer to where it was when the finger lifts. It prints "status k=v ..." lines for the agent
 * and exits 3 when the tap cannot be created (permission missing).
 * Build (install.sh, fixed paths): clang -O2 -framework ApplicationServices -framework CoreFoundation
 *        -framework IOKit -DPYTHON=... -DAGENT=... -DAGENT_DIR=... launcher.c
 * Build (package.sh, everything inside the app bundle): the same with -DBUNDLED. Then python3 is found
 * at run time, the agent lives in Contents/Resources/app, "--service" is what the login item passes,
 * and opening the app from the Finder runs agent/setup.py to install (or "--uninstall" to remove) that
 * login item.
 */
#include <ApplicationServices/ApplicationServices.h>
#include <IOKit/IOKitLib.h>
#include <dlfcn.h>
#include <mach-o/dyld.h>
#include <stdarg.h>
#include <time.h>
#include <signal.h>
#include <spawn.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child = 0;

/* Where python and the agent are: compiled in by install.sh, or inside the bundle for package.sh builds. */
static char g_python[4096], g_agent[4096], g_agent_dir[4096], g_bundle[4096];

#ifdef BUNDLED
static void parent_dir(char *path) {
    char *slash = strrchr(path, '/');
    if (slash) *slash = 0;
}

static const char *find_python(void) {
    const char *env = getenv("PCSTATS_PYTHON");
    if (env && access(env, X_OK) == 0) return env;
    static const char *cands[] = {"/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"};
    for (size_t i = 0; i < sizeof cands / sizeof cands[0]; i++) if (access(cands[i], X_OK) == 0) return cands[i];
    return "/usr/bin/python3";
}
#endif

static void resolve_paths(const char *self) {
#ifdef BUNDLED
    /* Python must not write its bytecode cache into the signed bundle (that breaks the seal): keep it in Caches. */
    const char *home = getenv("HOME");
    if (home && !getenv("PYTHONPYCACHEPREFIX")) {
        char cache[4096];
        snprintf(cache, sizeof cache, "%s/Library/Caches/pc-stats-dock/pycache", home);
        setenv("PYTHONPYCACHEPREFIX", cache, 1);
    }
    char contents[4096];
    strlcpy(contents, self, sizeof contents);
    parent_dir(contents);                    /* .../Contents/MacOS */
    parent_dir(contents);                    /* .../Contents */
    snprintf(g_agent_dir, sizeof g_agent_dir, "%s/Resources/app/agent", contents);
    snprintf(g_agent, sizeof g_agent, "%s/agent.py", g_agent_dir);
    strlcpy(g_bundle, contents, sizeof g_bundle);
    parent_dir(g_bundle);                    /* .../PC Stats Panel.app */
    strlcpy(g_python, find_python(), sizeof g_python);
#else
    (void)self;
    strlcpy(g_python, PYTHON, sizeof g_python);
    strlcpy(g_agent, AGENT, sizeof g_agent);
    strlcpy(g_agent_dir, AGENT_DIR, sizeof g_agent_dir);
    g_bundle[0] = 0;
#endif
}

/* Run agent/setup.py (install or uninstall the login item) and return its exit status. */
static int run_setup(const char *verb) {
    char setup[4096];
    snprintf(setup, sizeof setup, "%s/setup.py", g_agent_dir);
    char *args[] = {g_python, setup, (char *)verb, "--app", g_bundle, NULL};
    pid_t p = 0;
    int rc = posix_spawn(&p, g_python, NULL, NULL, args, environ);
    if (rc != 0) { fprintf(stderr, "PC Stats Panel: could not start %s: %s\n", g_python, strerror(rc)); return 1; }
    int status = 0;
    while (waitpid(p, &status, 0) < 0) { /* interrupted: keep waiting */ }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

static bool ax_trusted(bool prompt) {
    if (!prompt) return AXIsProcessTrusted();
    const void *keys[] = {kAXTrustedCheckOptionPrompt};
    const void *vals[] = {kCFBooleanTrue};
    CFDictionaryRef opts = CFDictionaryCreate(NULL, keys, vals, 1, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    bool trusted = AXIsProcessTrustedWithOptions(opts);
    if (opts) CFRelease(opts);
    return trusted;
}

static void post_key(unsigned code, bool down, unsigned long long flags) {
    CGEventRef ev = CGEventCreateKeyboardEvent(NULL, (CGKeyCode)code, down);
    if (!ev) return;
    CGEventSetFlags(ev, (CGEventFlags)flags);
    CGEventPost(kCGHIDEventTap, ev);
    CFRelease(ev);
}

/* macOS answers AXIsProcessTrusted() from a per-process cache, so a grant made after launch is
 * invisible to this process. A fresh copy of this same executable sees the truth. */
static bool ax_trusted_fresh(const char *self) {
    pid_t pid = 0;
    char *args[] = {(char *)self, "--ax-probe", NULL};
    if (posix_spawn(&pid, self, NULL, NULL, args, environ) != 0) return false;
    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {}
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

static void reply(int fd, const char *text) {
    (void)!write(fd, text, strlen(text));
    (void)!write(fd, "\n", 1);
}

static void serve(FILE *in, int out, const char *self) {
    char line[8192];
    while (fgets(line, sizeof line, in)) {
        line[strcspn(line, "\r\n")] = 0;
        if (strcmp(line, "ax") == 0) {
            if (ax_trusted(false)) reply(out, "1");
            else reply(out, ax_trusted_fresh(self) ? "restart" : "0");
        } else if (strcmp(line, "prompt") == 0) {
            reply(out, ax_trusted(true) ? "1" : "0");
        } else if (strncmp(line, "events ", 7) == 0) {
            if (!ax_trusted(false)) { reply(out, "err not trusted"); continue; }
            char *save = NULL;
            int n = 0;
            for (char *tok = strtok_r(line + 7, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
                unsigned code = 0, down = 0;
                unsigned long long flags = 0;
                if (sscanf(tok, "%u:%u:%llu", &code, &down, &flags) == 3) {
                    post_key(code, down != 0, flags);
                    usleep(12000);
                    n++;
                }
            }
            reply(out, n ? "ok" : "err no events");
        } else if (strcmp(line, "ping") == 0) {
            reply(out, "pong");
        } else {
            reply(out, "err unknown request");
        }
    }
}

/* ------------------------------------------------------------------ touch mapper ---- */
typedef void *IOHIDEventRef_;
static IOHIDEventRef_ (*fn_copy_hid)(CGEventRef);
static uint64_t (*fn_sender)(IOHIDEventRef_);
static void (*fn_suppression)(CFTimeInterval);

static struct {
    int want_w, want_h;
    CGRect panel, main;
    bool have_panel;
    uint64_t ids[16];
    int n_ids;
    char name[64];
    unsigned long seen, mapped, reported, touches, activations;
    int logged;
    CFMachPortRef tap;
    CGEventSourceRef src;
} tm;

static void tm_log(const char *fmt, ...) {
    char ts[32];
    time_t now = time(NULL);
    struct tm t;
    localtime_r(&now, &t);
    strftime(ts, sizeof ts, "%H:%M:%S", &t);
    printf("%s ", ts);
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    printf("\n");
    fflush(stdout);
}

static void tm_status(void) {
    printf("status devices=%d panel=%d mode=repost seen=%lu mapped=%lu touches=%lu activated=%lu main=%dx%d name=%s\n",
           tm.n_ids, tm.have_panel ? 1 : 0, tm.seen, tm.mapped, tm.touches, tm.activations,
           (int)tm.main.size.width, (int)tm.main.size.height, tm.name);
    fflush(stdout);
    tm.reported = tm.mapped;
}

static void tm_refresh_displays(void) {
    CGDirectDisplayID ids[16];
    uint32_t n = 0;
    CGGetActiveDisplayList(16, ids, &n);
    CGDirectDisplayID mainid = CGMainDisplayID();
    tm.main = CGDisplayBounds(mainid);
    bool had = tm.have_panel;
    tm.have_panel = false;
    for (uint32_t i = 0; i < n; i++) {
        if (ids[i] == mainid) continue;
        CGRect b = CGDisplayBounds(ids[i]);
        int w = (int)b.size.width, h = (int)b.size.height;
        int pw = (int)CGDisplayPixelsWide(ids[i]), ph = (int)CGDisplayPixelsHigh(ids[i]);
        if ((w == tm.want_w && h == tm.want_h) || (w == tm.want_h && h == tm.want_w) ||
            (pw == tm.want_w && ph == tm.want_h) || (pw == tm.want_h && ph == tm.want_w)) {
            tm.panel = b;
            tm.have_panel = true;
            break;
        }
    }
    if (tm.have_panel != had)
        tm_log(tm.have_panel ? "panel found at %.0f,%.0f (%dx%d); mapping touches onto it" : "panel is the main display or gone; touches pass through",
               tm.panel.origin.x, tm.panel.origin.y, (int)tm.panel.size.width, (int)tm.panel.size.height);
}

static int prop_int(io_registry_entry_t e, const char *key) {
    CFStringRef k = CFStringCreateWithCString(NULL, key, kCFStringEncodingUTF8);
    CFTypeRef v = IORegistryEntryCreateCFProperty(e, k, NULL, 0);
    CFRelease(k);
    int out = -1;
    if (v) {
        if (CFGetTypeID(v) == CFNumberGetTypeID()) CFNumberGetValue(v, kCFNumberIntType, &out);
        CFRelease(v);
    }
    return out;
}

static void prop_str(io_registry_entry_t e, const char *key, char *buf, size_t len) {
    CFStringRef k = CFStringCreateWithCString(NULL, key, kCFStringEncodingUTF8);
    CFTypeRef v = IORegistryEntryCreateCFProperty(e, k, NULL, 0);
    CFRelease(k);
    buf[0] = 0;
    if (v) {
        if (CFGetTypeID(v) == CFStringGetTypeID()) CFStringGetCString(v, buf, len, kCFStringEncodingUTF8);
        CFRelease(v);
    }
}

/* Every HID service/device that is a touch screen (usage page 13, usage 4); events carry the
 * registry id of their sender, which is how we tell the touchscreen from the mouse. */
static void tm_refresh_devices(void) {
    const char *classes[] = {"IOHIDEventService", "IOHIDDevice"};
    uint64_t ids[16];
    int n = 0;
    char name[64] = "";
    for (int c = 0; c < 2; c++) {
        io_iterator_t it = 0;
        if (IOServiceGetMatchingServices(0, IOServiceMatching(classes[c]), &it) != KERN_SUCCESS) continue;
        io_object_t e;
        while ((e = IOIteratorNext(it))) {
            if (n < 16 && prop_int(e, "PrimaryUsagePage") == 13 && prop_int(e, "PrimaryUsage") == 4) {
                uint64_t rid = 0;
                IORegistryEntryGetRegistryEntryID(e, &rid);
                ids[n++] = rid;
                if (!name[0]) prop_str(e, "Product", name, sizeof name);
            }
            IOObjectRelease(e);
        }
        IOObjectRelease(it);
    }
    if (n != tm.n_ids || strcmp(name, tm.name) != 0)
        tm_log("touchscreen: %s (%d hid services)", n ? name : "none connected", n);
    memcpy(tm.ids, ids, sizeof ids);
    tm.n_ids = n;
    strlcpy(tm.name, name, sizeof tm.name);
}

/* The window server puts the cursor where the hardware said, whatever a tap returns (checked: a
 * relocated event still moved the cursor to its original spot). So a touchscreen event is dropped
 * and re-posted as a fresh event at the mapped point, from an event source that does not suppress
 * the hardware events that follow it. */
static CGMouseButton button_of(CGEventType type, CGEventRef ev) {
    switch (type) {
        case kCGEventRightMouseDown: case kCGEventRightMouseUp: case kCGEventRightMouseDragged:
            return kCGMouseButtonRight;
        case kCGEventOtherMouseDown: case kCGEventOtherMouseUp: case kCGEventOtherMouseDragged:
            return (CGMouseButton)CGEventGetIntegerValueField(ev, kCGMouseEventButtonNumber);
        default:
            return kCGMouseButtonLeft;
    }
}

static void tm_post(CGEventType type, CGPoint at, CGMouseButton button, CGEventFlags flags, int64_t clicks, double pressure) {
    CGEventRef re = CGEventCreateMouseEvent(tm.src, type, at, button);
    if (!re) return;
    CGEventSetFlags(re, flags);
    CGEventSetIntegerValueField(re, kCGMouseEventClickState, clicks);
    CGEventSetDoubleValueField(re, kCGMouseEventPressure, pressure);
    CGEventSetIntegerValueField(re, kCGEventSourceUserData, 0x7058);
    CGEventPost(kCGHIDEventTap, re);
    CFRelease(re);
    tm.mapped++;
}

/* One finger on the panel = one touch. Chrome, like most Mac apps, uses the first click on a
 * window that is not active only to activate it (the page never sees it), and a button tap hands
 * focus back to the app you were using, so every tap would be such a first click. The app under
 * the finger is therefore made frontmost first and the mouse-down follows once it is. When the
 * finger lifts, the pointer goes back to where it was before the touch. */
static struct {
    bool active, down_posted, up_seen, clicked_to_activate, have_back, have_pointer;
    CGPoint q, back, pointer;      /* pointer: last spot a real mouse/trackpad put the cursor */
    CGEventFlags flags;
    int64_t clicks;
    double pressure;
    pid_t pid;
    CFAbsoluteTime since;
    CFRunLoopTimerRef timer;
    unsigned long activation_failures;
} tt;

#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
static pid_t front_pid(void) {
    static AXUIElementRef sys = NULL;
    if (!sys) sys = AXUIElementCreateSystemWide();
    CFTypeRef app = NULL;
    pid_t pid = 0;
    if (sys && AXUIElementCopyAttributeValue(sys, kAXFocusedApplicationAttribute, &app) == kAXErrorSuccess && app) {
        AXUIElementGetPid((AXUIElementRef)app, &pid);
        CFRelease(app);
    }
    if (!pid) {
        ProcessSerialNumber psn;
        if (GetFrontProcess(&psn) == noErr) GetProcessPID(&psn, &pid);
    }
    return pid;
}
#pragma clang diagnostic pop

static pid_t pid_at(CGPoint at) {
    CFArrayRef wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
    if (!wins) return 0;
    pid_t pid = 0;
    CFIndex n = CFArrayGetCount(wins);
    for (CFIndex i = 0; i < n && !pid; i++) {          /* front to back */
        CFDictionaryRef w = CFArrayGetValueAtIndex(wins, i);
        int layer = -1, owner = 0;
        CFNumberRef l = CFDictionaryGetValue(w, kCGWindowLayer);
        if (l) CFNumberGetValue(l, kCFNumberIntType, &layer);
        if (layer != 0) continue;
        CGRect b;
        CFDictionaryRef bd = CFDictionaryGetValue(w, kCGWindowBounds);
        if (!bd || !CGRectMakeWithDictionaryRepresentation(bd, &b) || !CGRectContainsPoint(b, at)) continue;
        CFNumberRef o = CFDictionaryGetValue(w, kCGWindowOwnerPID);
        if (o && CFNumberGetValue(o, kCFNumberIntType, &owner)) pid = (pid_t)owner;
    }
    CFRelease(wins);
    return pid;
}

static bool activate(pid_t pid) {
    AXUIElementRef app = AXUIElementCreateApplication(pid);
    if (!app) return false;
    AXError err = AXUIElementSetAttributeValue(app, kAXFrontmostAttribute, kCFBooleanTrue);
    CFRelease(app);
    return err == kAXErrorSuccess;
}

static void tt_later(double delay, void (^what)(void)) {
    CFRunLoopTimerRef t = CFRunLoopTimerCreateWithHandler(NULL, CFAbsoluteTimeGetCurrent() + delay, 0, 0, 0, ^(CFRunLoopTimerRef timer) {
        (void)timer;
        what();
    });
    CFRunLoopAddTimer(CFRunLoopGetMain(), t, kCFRunLoopCommonModes);
    CFRelease(t);
}

static void tt_post(CGEventType type, CGPoint at) {
    tm_post(type, at, kCGMouseButtonLeft, tt.flags, tt.clicks, tt.pressure);
}

static void tt_stop_waiting(void) {
    if (tt.timer) {
        CFRunLoopTimerInvalidate(tt.timer);
        CFRelease(tt.timer);
        tt.timer = NULL;
    }
}

/* The finger is up and everything is posted: put the pointer back shortly after the mouse-up lands. */
static void tt_finish(void) {
    CGPoint back = tt.back;
    tt.active = false;
    tt_later(0.04, ^{ if (!tt.active) CGWarpMouseCursorPosition(back); });
}

/* Post the held mouse-down, and the up too when the finger has already lifted. */
static void tt_flush(void) {
    tt_stop_waiting();
    if (!tt.active || tt.down_posted) return;
    tt_post(kCGEventLeftMouseDown, tt.q);
    tt.down_posted = true;
    if (tt.up_seen)
        tt_later(0.03, ^{ if (tt.active && tt.down_posted) { tt_post(kCGEventLeftMouseUp, tt.q); tt_finish(); } });
}

static void tt_wait_tick(CFRunLoopTimerRef timer, void *info) {
    (void)timer; (void)info;
    double waited = CFAbsoluteTimeGetCurrent() - tt.since;
    pid_t front = front_pid();
    if (front == tt.pid || waited > 0.3) {
        if (front != tt.pid) {
            tt.activation_failures++;
            if (tt.activation_failures <= 5) tm_log("app %d under the finger did not become active in time; posting the tap anyway", tt.pid);
        }
        tt_flush();
    } else if (waited > 0.12 && !tt.clicked_to_activate) {
        /* the Accessibility route did not take: do what a first click does, then keep waiting a little */
        tt.clicked_to_activate = true;
        tt_post(kCGEventLeftMouseDown, tt.q);
        tt_post(kCGEventLeftMouseUp, tt.q);
    }
}

static CGEventRef tm_tap(CGEventTapProxy proxy, CGEventType type, CGEventRef ev, void *info) {
    (void)proxy; (void)info;
    if (type == kCGEventTapDisabledByTimeout || type == kCGEventTapDisabledByUserInput) {
        if (tm.tap) CGEventTapEnable(tm.tap, true);
        tm_log("event tap re-enabled");
        return ev;
    }
    if (!tm.have_panel || !tm.n_ids || !fn_copy_hid || !fn_sender) return ev;
    if (CGEventGetIntegerValueField(ev, kCGEventSourceUserData) == 0x7058) return ev;   /* our own re-post */
    tm.seen++;
    IOHIDEventRef_ hid = fn_copy_hid(ev);
    if (!hid) return ev;
    uint64_t sender = fn_sender(hid);
    CFRelease(hid);
    bool ours = false;
    for (int i = 0; i < tm.n_ids; i++) if (tm.ids[i] == sender) { ours = true; break; }
    if (!ours) {                                        /* a real mouse or trackpad: remember where it put the pointer */
        if (type == kCGEventMouseMoved || type == kCGEventLeftMouseDragged || type == kCGEventLeftMouseDown) {
            tt.pointer = CGEventGetLocation(ev);
            tt.have_pointer = true;
        }
        return ev;
    }
    CGPoint p = CGEventGetLocation(ev);
    CGPoint q = p;
    if (!CGRectContainsPoint(tm.panel, p)) {           /* aimed at the main display: relocate */
        double u = (p.x - tm.main.origin.x) / tm.main.size.width;
        double v = (p.y - tm.main.origin.y) / tm.main.size.height;
        if (u < 0) u = 0; if (u > 0.9995) u = 0.9995;
        if (v < 0) v = 0; if (v > 0.9995) v = 0.9995;
        q = CGPointMake(tm.panel.origin.x + u * tm.panel.size.width, tm.panel.origin.y + v * tm.panel.size.height);
    }
    CGEventFlags flags = CGEventGetFlags(ev);
    int64_t clicks = CGEventGetIntegerValueField(ev, kCGMouseEventClickState);
    double pressure = CGEventGetDoubleValueField(ev, kCGMouseEventPressure);
    CFRunLoopRef main = CFRunLoopGetMain();

    if (type == kCGEventLeftMouseDown) {
        if (tt.active) {                                /* a lift went missing: end that touch first */
            tt_stop_waiting();
            if (tt.down_posted) tt_post(kCGEventLeftMouseUp, tt.q);
            tt.active = false;
        }
        /* Where the pointer goes back to: the last spot a real mouse put it. Touches themselves never
         * move that spot (the window server parks the cursor at the raw touch position for a moment,
         * so sampling it here during quick taps would capture the wrong place). */
        CGPoint c;
        if (tt.have_pointer) {
            c = tt.pointer;
        } else if (tt.have_back) {
            c = tt.back;
        } else {
            CGEventRef cur = CGEventCreate(NULL);
            c = cur ? CGEventGetLocation(cur) : CGPointMake(CGRectGetMidX(tm.main), CGRectGetMidY(tm.main));
            if (cur) CFRelease(cur);
            if (CGRectContainsPoint(tm.panel, c)) c = CGPointMake(CGRectGetMidX(tm.main), CGRectGetMidY(tm.main));
        }
        tt.active = true;
        tt.down_posted = tt.up_seen = tt.clicked_to_activate = false;
        tt.q = q;
        tt.back = c;
        tt.have_back = true;
        tt.flags = flags;
        tt.clicks = clicks;
        tt.pressure = pressure;
        tm.touches++;
        tt.pid = pid_at(q);
        pid_t front = front_pid();
        bool needs = tt.pid && front && front != tt.pid;
        bool ax_ok = false;
        if (needs) {
            ax_ok = activate(tt.pid);
            tm.activations++;
            tt.since = CFAbsoluteTimeGetCurrent();
            tt.timer = CFRunLoopTimerCreate(NULL, CFAbsoluteTimeGetCurrent() + 0.01, 0.01, 0, 0, tt_wait_tick, NULL);
            CFRunLoopAddTimer(main, tt.timer, kCFRunLoopCommonModes);
        } else {
            CFRunLoopPerformBlock(main, kCFRunLoopCommonModes, ^{ tt_flush(); });
            CFRunLoopWakeUp(main);
        }
        if (tm.logged < 20) {
            tm.logged++;
            tm_log("touch at %.0f,%.0f -> %.0f,%.0f%s%s (pointer returns to %.0f,%.0f)", p.x, p.y, q.x, q.y,
                   needs ? "; activating the app under it" : "", needs && !ax_ok ? " (accessibility refused; using a click)" : "", c.x, c.y);
        }
        return NULL;
    }
    if (type == kCGEventLeftMouseUp) {
        if (!tt.active) {
            CFRunLoopPerformBlock(main, kCFRunLoopCommonModes, ^{ tm_post(type, q, kCGMouseButtonLeft, flags, clicks, pressure); });
            CFRunLoopWakeUp(main);
            return NULL;
        }
        if (!tt.down_posted) {
            tt.up_seen = true;                          /* the flush posts it after the down */
            return NULL;
        }
        CFRunLoopPerformBlock(main, kCFRunLoopCommonModes, ^{ if (tt.active) { tt_post(kCGEventLeftMouseUp, tt.q); tt_finish(); } });
        CFRunLoopWakeUp(main);
        return NULL;
    }
    if (type == kCGEventLeftMouseDragged && tt.active) {
        if (!tt.down_posted) {
            double dx = q.x - tt.q.x, dy = q.y - tt.q.y;
            if (dx * dx + dy * dy < 12 * 12) return NULL;   /* finger wobble while the app activates */
            tt.q = q;
            CFRunLoopPerformBlock(main, kCFRunLoopCommonModes, ^{ tt_flush(); tt_post(kCGEventLeftMouseDragged, q); });
        } else {
            tt.q = q;
            CFRunLoopPerformBlock(main, kCFRunLoopCommonModes, ^{ tt_post(kCGEventLeftMouseDragged, q); });
        }
        CFRunLoopWakeUp(main);
        return NULL;
    }
    CGMouseButton button = button_of(type, ev);        /* hover, other buttons: relocate as they are */
    CFRunLoopPerformBlock(main, kCFRunLoopCommonModes, ^{ tm_post(type, q, button, flags, clicks, pressure); });
    CFRunLoopWakeUp(main);
    if (tm.logged < 20) {
        tm.logged++;
        tm_log("touch event %d at %.0f,%.0f -> %.0f,%.0f", (int)type, p.x, p.y, q.x, q.y);
    }
    return NULL;   /* the original, aimed at the main display, goes nowhere */
}

static void tm_timer(CFRunLoopTimerRef timer, void *info) {
    (void)timer; (void)info;
    static int ticks = 0;
    if (getppid() == 1) { tm_log("agent gone; stopping"); exit(0); }
    tm_refresh_displays();
    tm_refresh_devices();
    if (++ticks % 3 == 0 || tm.mapped != tm.reported) tm_status();
}

static int touch_map_main(int w, int h) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    tm.want_w = w;
    tm.want_h = h;
    fn_copy_hid = (IOHIDEventRef_ (*)(CGEventRef))dlsym(RTLD_DEFAULT, "CGEventCopyIOHIDEvent");
    fn_sender = (uint64_t (*)(IOHIDEventRef_))dlsym(RTLD_DEFAULT, "IOHIDEventGetSenderID");
    fn_suppression = (void (*)(CFTimeInterval))dlsym(RTLD_DEFAULT, "CGSetLocalEventsSuppressionInterval");
    if (!fn_copy_hid || !fn_sender)
        tm_log("warning: this macOS does not expose the sending device of an event; touch mapping is off");
    tm.src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState);
    if (tm.src) {
        CGEventSourceSetLocalEventsSuppressionInterval(tm.src, 0.0);
        CGEventSourceSetLocalEventsFilterDuringSuppressionState(tm.src, kCGEventFilterMaskPermitAllEvents, kCGEventSuppressionStateSuppressionInterval);
        CGEventSourceSetLocalEventsFilterDuringSuppressionState(tm.src, kCGEventFilterMaskPermitAllEvents, kCGEventSuppressionStateRemoteMouseDrag);
    }
    if (fn_suppression) fn_suppression(0.0);
    tm_refresh_displays();
    tm_refresh_devices();
    CGEventType types[] = {kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGEventRightMouseDown, kCGEventRightMouseUp,
                           kCGEventMouseMoved, kCGEventLeftMouseDragged, kCGEventRightMouseDragged,
                           kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGEventOtherMouseDragged};
    CGEventMask mask = 0;
    for (size_t i = 0; i < sizeof types / sizeof types[0]; i++) mask |= CGEventMaskBit(types[i]);
    tm.tap = CGEventTapCreate(kCGHIDEventTap, kCGHeadInsertEventTap, kCGEventTapOptionDefault, mask, tm_tap, NULL);
    if (!tm.tap) {
        tm_log("could not create the event tap: allow PC Stats Panel under System Settings > Privacy & Security > Accessibility");
        return 3;
    }
    CFRunLoopSourceRef src = CFMachPortCreateRunLoopSource(NULL, tm.tap, 0);
    CFRunLoopAddSource(CFRunLoopGetMain(), src, kCFRunLoopCommonModes);
    CGEventTapEnable(tm.tap, true);
    tm_log("touch mapper running for a %dx%d panel", w, h);
    tm_status();
    CFRunLoopTimerRef timer = CFRunLoopTimerCreate(NULL, CFAbsoluteTimeGetCurrent() + 3, 3, 0, 0, tm_timer, NULL);
    CFRunLoopAddTimer(CFRunLoopGetMain(), timer, kCFRunLoopCommonModes);
    CFRunLoopRun();
    return 0;
}

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--ax-probe") == 0) return AXIsProcessTrusted() ? 0 : 1;
    if (argc > 3 && strcmp(argv[1], "--touch-map") == 0) return touch_map_main(atoi(argv[2]), atoi(argv[3]));
    char self[4096];
    uint32_t self_len = sizeof self;
    if (_NSGetExecutablePath(self, &self_len) != 0) strlcpy(self, argv[0], sizeof self);
    resolve_paths(self);
    bool service = getenv("PCSTATS_SERVICE") != NULL;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--service") == 0) service = true;
        if (strcmp(argv[i], "--uninstall") == 0) return run_setup("uninstall");
    }
#ifdef BUNDLED
    if (!service) return run_setup("install");        /* opened from the Finder: set up the login item, then quit */
#else
    (void)service;
#endif
    setenv("PCSTATS_LAUNCHER", self, 1);
    if (chdir(g_agent_dir) != 0) perror("chdir");
    setenv("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", 1);

    int req[2], rep[2];           /* req: python -> us, rep: us -> python */
    if (pipe(req) != 0 || pipe(rep) != 0) { perror("pipe"); return 1; }
    char buf[32];
    snprintf(buf, sizeof buf, "%d", req[1]); setenv("PCSTATS_KEY_REQ", buf, 1);
    snprintf(buf, sizeof buf, "%d", rep[0]); setenv("PCSTATS_KEY_REP", buf, 1);

    char *args[argc + 2];
    int n = 0;
    args[n++] = g_python;
    args[n++] = g_agent;
    for (int i = 1; i < argc; i++) if (strcmp(argv[i], "--service") != 0) args[n++] = argv[i];
    args[n] = NULL;
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    signal(SIGPIPE, SIG_IGN);
    int rc = posix_spawn(&child, g_python, NULL, NULL, args, environ);
    if (rc != 0) { fprintf(stderr, "PC Stats Panel: could not start %s: %s\n", g_python, strerror(rc)); return 1; }
    close(req[1]);                /* python owns the write end of req and the read end of rep */
    close(rep[0]);

    FILE *in = fdopen(req[0], "r");
    serve(in, rep[1], self);      /* returns at EOF, i.e. when python exits */

    int status = 0;
    while (waitpid(child, &status, 0) < 0) { /* interrupted: keep waiting */ }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
