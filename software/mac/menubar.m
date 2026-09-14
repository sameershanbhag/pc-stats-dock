// PC Stats Panel's menu bar item: a small gauge in the menu bar with the things you reach for.
// Started and supervised by the agent (PCSTATS_PORT says where the agent listens); quits when the agent is gone.
// Build: clang -fobjc-arc -framework AppKit -framework Foundation -o PCStatsMenu menubar.m
#import <AppKit/AppKit.h>
#include <unistd.h>

@interface PCMenu : NSObject <NSApplicationDelegate, NSMenuDelegate>
@property (strong) NSStatusItem *item;
@property (strong) NSMenuItem *status;
@property (copy) NSString *base;
@end

@implementation PCMenu

- (NSImage *)icon {
    NSImage *img = [NSImage imageWithSize:NSMakeSize(18, 18) flipped:NO drawingHandler:^BOOL(NSRect rect) {
        [[NSColor blackColor] set];
        NSBezierPath *arc = [NSBezierPath bezierPath];               // a gauge: an arc open at the bottom, a needle, a hub
        [arc appendBezierPathWithArcWithCenter:NSMakePoint(9, 7.5) radius:6.5 startAngle:200 endAngle:-20 clockwise:YES];
        arc.lineWidth = 2.0;
        arc.lineCapStyle = NSLineCapStyleRound;
        [arc stroke];
        NSBezierPath *needle = [NSBezierPath bezierPath];
        [needle moveToPoint:NSMakePoint(9, 7.5)];
        [needle lineToPoint:NSMakePoint(13.0, 12.0)];
        needle.lineWidth = 2.0;
        needle.lineCapStyle = NSLineCapStyleRound;
        [needle stroke];
        [[NSBezierPath bezierPathWithOvalInRect:NSMakeRect(7.3, 5.8, 3.4, 3.4)] fill];
        return YES;
    }];
    [img setTemplate:YES];                                           // black + alpha: macOS tints it for light and dark menu bars
    return img;
}

- (void)post:(NSString *)path {
    NSMutableURLRequest *req = [NSMutableURLRequest requestWithURL:[NSURL URLWithString:[self.base stringByAppendingString:path]]];
    req.HTTPMethod = @"POST";
    [req setValue:@"application/json" forHTTPHeaderField:@"Content-Type"];
    req.HTTPBody = [@"{}" dataUsingEncoding:NSUTF8StringEncoding];
    req.timeoutInterval = 10;
    [[[NSURLSession sharedSession] dataTaskWithRequest:req] resume];
}

- (void)openAdmin:(id)sender { [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:[self.base stringByAppendingString:@"/admin"]]]; }
- (void)openDashboard:(id)sender { [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:[self.base stringByAppendingString:@"/"]]]; }
- (void)showFace:(id)sender { [self post:@"/api/admin/face/preview"]; }
- (void)fixArrangement:(id)sender { [self post:@"/api/admin/arrange"]; }

- (void)restart:(id)sender {
    NSTask *t = [NSTask new];
    t.executableURL = [NSURL fileURLWithPath:@"/bin/launchctl"];
    t.arguments = @[@"kickstart", @"-k", [NSString stringWithFormat:@"gui/%d/com.pcstatsdock.agent", getuid()]];
    [t launchAndReturnError:nil];
}

- (void)menuWillOpen:(NSMenu *)menu {
    self.status.title = @"PC Stats Panel";
    NSURL *url = [NSURL URLWithString:[self.base stringByAppendingString:@"/api/health"]];
    NSURLRequest *req = [NSURLRequest requestWithURL:url cachePolicy:NSURLRequestReloadIgnoringLocalCacheData timeoutInterval:3];
    [[[NSURLSession sharedSession] dataTaskWithRequest:req completionHandler:^(NSData *data, NSURLResponse *resp, NSError *err) {
        NSString *line = @"PC Stats Panel · agent not running";
        if (data) {
            NSDictionary *h = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
            if ([h isKindOfClass:[NSDictionary class]]) {
                BOOL connected = [h[@"panel"][@"connected"] boolValue];
                NSString *touch = h[@"touch"][@"state"] ?: @"off";
                BOOL ax = [h[@"caps"][@"accessibility"] boolValue];
                line = connected ? [NSString stringWithFormat:@"Panel connected · touch %@", touch] : @"Panel not connected";
                if (!ax) line = [line stringByAppendingString:@" · needs Accessibility"];
            }
        }
        dispatch_async(dispatch_get_main_queue(), ^{ self.status.title = line; });
    }] resume];
}

- (void)applicationDidFinishLaunching:(NSNotification *)note {
    [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];   // no Dock icon, no menu of its own
    self.item = [[NSStatusBar systemStatusBar] statusItemWithLength:NSSquareStatusItemLength];
    self.item.button.image = [self icon];
    self.item.button.toolTip = @"PC Stats Panel";
    NSMenu *m = [[NSMenu alloc] initWithTitle:@"PC Stats Panel"];
    m.delegate = self;
    self.status = [m addItemWithTitle:@"PC Stats Panel" action:nil keyEquivalent:@""];
    self.status.enabled = NO;
    [m addItem:[NSMenuItem separatorItem]];
    [[m addItemWithTitle:@"Open the admin page" action:@selector(openAdmin:) keyEquivalent:@""] setTarget:self];
    [[m addItemWithTitle:@"Open the dashboard in a browser" action:@selector(openDashboard:) keyEquivalent:@""] setTarget:self];
    [[m addItemWithTitle:@"Show the face on the panel for 20 s" action:@selector(showFace:) keyEquivalent:@""] setTarget:self];
    [[m addItemWithTitle:@"Fix the screen arrangement" action:@selector(fixArrangement:) keyEquivalent:@""] setTarget:self];
    [m addItem:[NSMenuItem separatorItem]];
    [[m addItemWithTitle:@"Restart the dock" action:@selector(restart:) keyEquivalent:@""] setTarget:self];
    self.item.menu = m;
    [NSTimer scheduledTimerWithTimeInterval:3 repeats:YES block:^(NSTimer *t) {
        if (getppid() == 1) [NSApp terminate:nil];                     // the agent that started us is gone
    }];
}

@end

int main(void) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        PCMenu *menu = [PCMenu new];
        const char *port = getenv("PCSTATS_PORT");
        menu.base = [NSString stringWithFormat:@"http://localhost:%s", (port && *port) ? port : "4400"];
        app.delegate = menu;
        [app run];
    }
    return 0;
}
