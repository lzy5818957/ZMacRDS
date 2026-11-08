#import "m1-common.h"
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#include "m1-marker.h"
#include "m2-bridge.h"
#include "m1-capture.h"
#include "m1-audio-tap.h"
#include "service-command.h"
#include <sys/file.h>

@class M1Agent;
@interface M1View : NSView
@property(weak) M1Agent *agent;
@end
@interface M1Window : NSWindow
@end
@interface DesktopAnchor : NSPanel
@end
@implementation DesktopAnchor
- (BOOL)canBecomeKeyWindow { return NO; }
- (BOOL)canBecomeMainWindow { return NO; }
@end
@implementation M1Window
- (BOOL)canBecomeKeyWindow { return YES; }
- (BOOL)canBecomeMainWindow { return YES; }
@end

@interface M1Agent : NSObject <NSApplicationDelegate, SCStreamOutput, SCStreamDelegate, NSMenuDelegate>
@property NSString *run, *mode, *directory;
@property NSFileHandle *log;
@property NSDictionary *initialIdentity;
@property NSWindow *window;
@property M1View *view;
@property SCStream *stream;
@property NSTimer *timer, *inputTimer;
@property id activity;
@property id remoteActivity;
@property uint32_t nonce, tick, capturedTick, inputTrial;
@property uint64_t started, lastFrame, lastChanged, lastFrameDiagnostic;
@property uint64_t lastDiscardDiagnostic;
@property uint64_t lastCaptureCallback, lastTickAt;
@property uint64_t lastCaptureRefresh;
@property BOOL captureRefreshing;
@property SCStreamConfiguration *captureConfiguration;
@property SCContentFilter *captureFilter;
@property uint64_t lastRecoveryAt;
@property NSUInteger duration, received, frames;
@property NSUInteger recoveryAttempts;
@property BOOL finishing, capturing, connected;
@property BOOL captureStarting;
@property BOOL awaitingCaptureProof;
@property(strong) id recoveryBuffer;
@property(strong) id idleBaseline;
@property uint64_t firstRecoverySignal;
@property NSUInteger epoch, scriptIndex, scriptEvent;
@property uint64_t nextActionAt;
@property NSArray *script;
@property NSDictionary *scriptRequest;
@property NSString *originalInputSource;
@property NSTimer *scriptTimer;
@property BOOL bridgeEnabled, serviceMode, audioEnabled;
@property BOOL backgroundMode;
@property NSStatusItem *statusItem;
@property NSString *captureError;
@property uint32_t targetDisplay;
@property NSString *lastControlCommand;
@property id audio;
@property BOOL audioRefreshing;
@property uint64_t lastAudioRefresh;
@property NSDictionary *guardian;
@property int captureFPS;
@property M2Bridge *bridge;
- (void)disconnectCapture:(NSString *)reason;
- (void)beginScript;
- (void)scriptTick:(NSTimer *)timer;
- (void)record:(NSString *)kind values:(NSDictionary *)values;
- (void)receive:(NSEvent *)event;
@end

@implementation M1View
- (BOOL)isFlipped { return YES; }
- (BOOL)acceptsFirstResponder { return YES; }
- (void)drawRect:(NSRect)rect {
    (void)rect;
    M1Agent *a = self.agent;
    [(getuid()%2 ? [NSColor colorWithSRGBRed:0.30 green:0.14 blue:0.40 alpha:1] :
                  [NSColor colorWithSRGBRed:0.05 green:0.26 blue:0.35 alpha:1]) setFill];
    NSRectFill(self.bounds);
    NSDictionary *large = @{NSFontAttributeName:[NSFont monospacedSystemFontOfSize:22 weight:NSFontWeightBold],
                            NSForegroundColorAttributeName:NSColor.whiteColor};
    NSDictionary *small = @{NSFontAttributeName:[NSFont monospacedSystemFontOfSize:14 weight:NSFontWeightRegular],
                            NSForegroundColorAttributeName:NSColor.whiteColor};
    [[NSString stringWithFormat:@"ZMacRDS M1 | UID %u",getuid()] drawAtPoint:NSMakePoint(24,24) withAttributes:large];
    [[NSString stringWithFormat:@"%@ | %@ | %08x",a.run,a.mode,a.nonce] drawAtPoint:NSMakePoint(24,62) withAttributes:small];
    uint8_t bytes[M1_BYTES]; m1Encode((M1Marker){getuid(),a.nonce,a.tick},bytes);
    for (int cell = 0; cell < M1_CELLS; cell++) {
        NSColor *color;
        if (cell<3) color = cell==1 ? NSColor.cyanColor : NSColor.magentaColor;
        else { int bit=cell-3; color = bytes[bit/8] & (1<<(7-bit%8)) ? NSColor.whiteColor : NSColor.blackColor; }
        [color setFill]; NSRectFill(NSMakeRect(24+cell*M1_CELL,104,M1_CELL,24));
    }
    [[NSString stringWithFormat:@"Drawn: %u   Captured: %u",a.tick,a.capturedTick] drawAtPoint:NSMakePoint(24,158) withAttributes:large];
    [[NSString stringWithFormat:@"Frames: %lu   Input events: %lu",(unsigned long)a.frames,(unsigned long)a.received] drawAtPoint:NSMakePoint(24,200) withAttributes:small];
    [@"Synthetic test surface. No personal content required." drawAtPoint:NSMakePoint(24,240) withAttributes:small];
    [@"Input target area (click / drag / type)" drawAtPoint:NSMakePoint(24,300) withAttributes:small];
    [NSColor.whiteColor setStroke]; NSFrameRect(NSMakeRect(24,330,560,100));
}
- (void)keyDown:(NSEvent *)e { [self.agent receive:e]; }
- (void)keyUp:(NSEvent *)e { [self.agent receive:e]; }
- (void)mouseDown:(NSEvent *)e { [self.window makeFirstResponder:self]; [self.agent receive:e]; }
- (void)mouseDragged:(NSEvent *)e { [self.agent receive:e]; }
- (void)mouseUp:(NSEvent *)e { [self.agent receive:e]; }
@end

@implementation M1Agent
- (void)record:(NSString *)kind values:(NSDictionary *)values {
    NSAssert(NSThread.isMainThread,@"Serialize evidence on the main queue");
    NSMutableDictionary *row = [identity() mutableCopy];
    [row addEntriesFromDictionary:@{@"schema":@1, @"kind":kind, @"run_id":self.run,
        @"capture_mode":self.mode, @"nonce":@(self.nonce), @"uptime_ms":@(nowMS()),
        @"boot_id":kernelString("kern.bootsessionuuid"), @"os_build":kernelString("kern.osversion")}];
    [row addEntriesFromDictionary:values];
    if(self.serviceMode && self.log.offsetInFile>=4*1024*1024) {
        [self.log closeFile];
        NSString *path=[self.directory stringByAppendingPathComponent:@"events.jsonl"];
        [NSFileManager.defaultManager removeItemAtPath:[path stringByAppendingString:@".2"] error:NULL];
        [NSFileManager.defaultManager moveItemAtPath:[path stringByAppendingString:@".1"] toPath:[path stringByAppendingString:@".2"] error:NULL];
        [NSFileManager.defaultManager moveItemAtPath:path toPath:[path stringByAppendingString:@".1"] error:NULL];
        [NSFileManager.defaultManager createFileAtPath:path contents:nil attributes:@{NSFilePosixPermissions:@0600}];
        self.log=[NSFileHandle fileHandleForWritingAtPath:path];
    }
    NSError *error;
    NSData *data = [NSJSONSerialization dataWithJSONObject:row options:NSJSONWritingSortedKeys error:&error];
    if (!data) { fprintf(stderr,"JSON: %s\n",error.localizedDescription.UTF8String); exit(1); }
    [self.log writeData:data]; [self.log writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
}
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    self.initialIdentity = identity(); self.started = nowMS();self.lastRecoveryAt=self.started; self.connected=YES; self.epoch=1;
    if (getuid()==0 || ![self.initialIdentity[@"gui_uid"] isEqual:@(getuid())] ||
        ![self.initialIdentity[@"login_complete"] boolValue]) {
        [self record:@"error" values:@{@"message":@"A logged-in ordinary GUI account is required"}];
        [NSApp terminate:nil]; return;
    }
    // Prevent App Nap from being mistaken for WindowServer suspension.
    self.activity = [NSProcessInfo.processInfo beginActivityWithOptions:NSActivityUserInitiatedAllowingIdleSystemSleep reason:@"Bounded M1 desktop isolation experiment"];
    if(self.backgroundMode) {
        // Keep a WindowServer object owned by this exact Aqua process. Never
        // choose a display without locating this window in shareable content.
        NSRect screen=NSScreen.mainScreen.frame;
        self.window=[[DesktopAnchor alloc] initWithContentRect:NSMakeRect(screen.origin.x+2,screen.origin.y+2,8,8)
            styleMask:NSWindowStyleMaskBorderless|NSWindowStyleMaskNonactivatingPanel
            backing:NSBackingStoreBuffered defer:NO];
        self.window.title=@"ZMacRDS Session Anchor";
        self.window.opaque=NO;
        self.window.backgroundColor=NSColor.clearColor;
        self.window.hasShadow=NO;
        self.window.ignoresMouseEvents=YES;
        self.window.level=NSNormalWindowLevel;
        self.window.collectionBehavior=NSWindowCollectionBehaviorStationary|NSWindowCollectionBehaviorIgnoresCycle;
        [self.window orderBack:nil];
        self.statusItem=[NSStatusBar.systemStatusBar statusItemWithLength:NSVariableStatusItemLength];
        self.statusItem.button.title=@"ZMacRDS";
        self.statusItem.button.toolTip=@"远程桌面状态与权限";
        NSMenu *menu=[NSMenu new];menu.delegate=self;self.statusItem.menu=menu;
    } else {
    self.window = [[M1Window alloc] initWithContentRect:NSMakeRect(150,220,640,480)
        styleMask:NSWindowStyleMaskBorderless backing:NSBackingStoreBuffered defer:NO];
    self.window.title = [NSString stringWithFormat:@"ZMacRDS M1 — UID %u",getuid()];
    self.window.backgroundColor = NSColor.blackColor; self.window.hasShadow = NO;
    self.window.releasedWhenClosed = NO;
    self.view = [[M1View alloc] initWithFrame:NSMakeRect(0,0,640,480)]; self.view.agent = self;
    self.window.contentView = self.view;
    [self.window makeKeyAndOrderFront:nil]; [self.window makeFirstResponder:self.view];
    [NSApp activateIgnoringOtherApps:YES];
    }
    [self record:@"start" values:@{@"pid":@(getpid()),@"window_id":@(self.window.windowNumber),@"capture_fps":@(self.captureFPS),
        @"background":@(self.backgroundMode),
        @"screen_capture_preflight":@(CGPreflightScreenCaptureAccess()),@"accessibility_trusted":@(AXIsProcessTrusted())}];
    if(self.bridgeEnabled) {
        NSMutableDictionary *session=[self.initialIdentity mutableCopy];
        [session addEntriesFromDictionary:@{@"nonce":@(self.nonce),@"epoch":@(self.epoch),@"boot_id":kernelString("kern.bootsessionuuid")}];
        __weak M1Agent *weak=self;
        self.bridge=[[M2Bridge alloc] initWithSession:session ready:^BOOL {
            M1Agent *a=weak;return a && !a.finishing && a.connected &&
                a.epoch==[session[@"epoch"] unsignedIntegerValue] && AXIsProcessTrusted() && CGPreflightScreenCaptureAccess();
        } log:^(NSString *kind,NSDictionary *values){[weak record:kind values:values];}];
        if(!self.bridge){[self record:@"error" values:@{@"message":@"Cannot create private bridge"}];[self finish];return;}
        if(!AXIsProcessTrusted() && !self.backgroundMode) {
            NSDictionary *options=@{(__bridge NSString *)kAXTrustedCheckOptionPrompt:@YES};
            (void)AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)options);
        }
    }
    self.timer = [NSTimer scheduledTimerWithTimeInterval:1 target:self selector:@selector(tickTimer:) userInfo:nil repeats:YES];
    // Keep identity/status and control acknowledgements current while a menu
    // is being tracked; the default run-loop mode pauses during menu use.
    [NSRunLoop.mainRunLoop addTimer:self.timer forMode:NSRunLoopCommonModes];
    [NSWorkspace.sharedWorkspace.notificationCenter addObserver:self selector:@selector(sessionBecameActive:) name:NSWorkspaceSessionDidBecomeActiveNotification object:nil];
    if(self.serviceMode)[NSDistributedNotificationCenter.defaultCenter addObserver:self
        selector:@selector(permissionRequest:) name:ZR_PERMISSION_NOTIFICATION object:zrPermissionObject()
        suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC),dispatch_get_main_queue(),^{ [self startCapture]; });
}
- (void)menuNeedsUpdate:(NSMenu *)menu {
    [menu removeAllItems];
    NSString *connection=self.bridge.client>=0 ? @"RDP 已连接" : @"等待 RDP 连接";
    for(NSString *title in @[@"ZMacRDS 后台服务",connection,
        CGPreflightScreenCaptureAccess() ? @"录屏：已授权" : @"录屏：需要授权",
        AXIsProcessTrusted() ? @"键盘鼠标：已授权" : @"键盘鼠标：需要授权"])
        [menu addItemWithTitle:title action:nil keyEquivalent:@""];
    [menu addItem:NSMenuItem.separatorItem];
    NSMenuItem *disconnect=[menu addItemWithTitle:@"断开本账号的 RDP" action:@selector(disconnectRdp:) keyEquivalent:@""];
    disconnect.target=self;disconnect.enabled=self.bridge.client>=0;
    NSMenuItem *manager=[menu addItemWithTitle:@"打开管理窗口…" action:@selector(openManager:) keyEquivalent:@""];
    manager.target=self;
    NSMenuItem *settings=[menu addItemWithTitle:@"打开系统设置…" action:@selector(openSettings:) keyEquivalent:@""];
    settings.target=self;
    if(!CGPreflightScreenCaptureAccess()) {
        NSMenuItem *permission=[menu addItemWithTitle:@"请求录屏权限…" action:@selector(requestScreen:) keyEquivalent:@""];permission.target=self;
    }
    if(!AXIsProcessTrusted()) {
        NSMenuItem *permission=[menu addItemWithTitle:@"请求键盘鼠标权限…" action:@selector(requestInput:) keyEquivalent:@""];permission.target=self;
    }
}
- (void)disconnectRdp:(id)sender { (void)sender;[self.bridge closePeer]; }
- (void)permissionSettings:(NSString *)pane {
    NSString *prefix=NSProcessInfo.processInfo.operatingSystemVersion.majorVersion>=26 ?
        @"x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?" :
        @"x-apple.systempreferences:com.apple.preference.security?";
    [NSWorkspace.sharedWorkspace openURL:[NSURL URLWithString:[prefix stringByAppendingString:pane]]];
}
- (void)requestScreen:(id)sender {
    (void)sender;if(!CGPreflightScreenCaptureAccess())(void)CGRequestScreenCaptureAccess();
    [self permissionSettings:@"Privacy_ScreenCapture"];
}
- (void)requestInput:(id)sender {
    (void)sender;(void)AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)@{(__bridge NSString *)kAXTrustedCheckOptionPrompt:@YES});
    [self permissionSettings:@"Privacy_Accessibility"];
}
- (void)permissionRequest:(NSNotification *)notification {
    // Notifications are hints, never authorization. Only show this signed
    // process's OS prompt in its own existing Aqua session, on explicit action.
    NSString *action=notification.userInfo[@"action"];
    if(!self.serviceMode || ![notification.object isEqual:zrPermissionObject()] ||
       ![action isKindOfClass:NSString.class] ||
       (![action isEqual:@"screen"] && ![action isEqual:@"input"]))return;
    dispatch_async(dispatch_get_main_queue(),^{
        NSDictionary *current=identity();
        if(self.finishing || ![current[@"gui_uid"] isEqual:self.initialIdentity[@"gui_uid"]] ||
           ![current[@"audit_session_id"] isEqual:self.initialIdentity[@"audit_session_id"]])return;
        if([action isEqual:@"screen"])[self requestScreen:nil];else [self requestInput:nil];
    });
}
- (void)openManager:(id)sender {
    (void)sender;
    NSURL *url=[NSURL fileURLWithPath:@"/Applications/ZMacRDS Manager.app"];
    [NSWorkspace.sharedWorkspace openApplicationAtURL:url configuration:NSWorkspaceOpenConfiguration.configuration completionHandler:nil];
}
- (void)openSettings:(id)sender {
    (void)sender;
    NSURL *url=[NSWorkspace.sharedWorkspace URLForApplicationWithBundleIdentifier:@"com.apple.systempreferences"];
    if(url)[NSWorkspace.sharedWorkspace openApplicationAtURL:url configuration:NSWorkspaceOpenConfiguration.configuration completionHandler:nil];
}
- (void)refreshAudio {
    if(!self.audioEnabled || self.finishing || nowMS()-self.lastAudioRefresh<2000)return;
    self.lastAudioRefresh=nowMS();
    if(@available(macOS 14.2,*)) {
        if(!self.audio){M1Audio *audio=[M1Audio new];audio.session=self.initialIdentity;audio.bridge=self.bridge;
            __weak M1Agent *weak=self;audio.logEvent=^(NSString *kind,NSDictionary *value){dispatch_async(dispatch_get_main_queue(),^{[weak record:kind values:value];});};self.audio=audio;}
        [(M1Audio *)self.audio refresh];
    }
}
- (void)startCapture {
    if(self.captureStarting || self.finishing || !self.connected)return;
    if(self.serviceMode && !CGPreflightScreenCaptureAccess()) {
        self.captureError=@"请在管理窗口的首次配置中授权屏幕与系统音频录制";
        return;
    }
    self.captureStarting=YES;self.lastRecoveryAt=nowMS();
    void (^ready)(SCShareableContent *, NSError *) = ^(SCShareableContent *content, NSError *error) {
        dispatch_async(dispatch_get_main_queue(),^{
            if (self.finishing || !self.connected) return;
            self.captureStarting=NO;
            if (error) { self.captureError=error.localizedDescription;[self record:@"capture_error" values:@{@"message":error.localizedDescription,@"code":@(error.code)}]; return; }
            SCWindow *own;
            for (SCWindow *w in content.windows)
                if (w.windowID == (CGWindowID)self.window.windowNumber && w.owningApplication.processID==getpid()) { own=w; break; }
            if (!own) { self.captureError=@"无法确认本账号的桌面定位窗口";[self record:@"capture_error" values:@{@"message":@"Own session anchor absent; no fallback to another window"}]; return; }
            SCContentFilter *filter;
            SCStreamConfiguration *config = [SCStreamConfiguration new];
            if ([self.mode isEqual:@"window"]) {
                filter = [[SCContentFilter alloc] initWithDesktopIndependentWindow:own];
                config.width=640; config.height=480;
            } else {
                SCDisplay *display;NSUInteger matches=0;
                for (SCDisplay *d in content.displays) if (CGRectContainsRect(d.frame,own.frame)) { display=d;matches++; }
                if (matches!=1) { self.captureError=@"无法唯一确认本账号的显示器";[self record:@"capture_error" values:@{@"message":@"Session anchor must identify exactly one display"}]; return; }
                self.targetDisplay=display.displayID;
                filter = [[SCContentFilter alloc] initWithDisplay:display excludingWindows:self.backgroundMode ? @[own] : @[]];
                config.width=display.width; config.height=display.height;
                if(self.bridge) {
                    self.bridge.width=display.width;self.bridge.height=display.height;self.bridge.bounds=display.frame;
                    NSMutableDictionary *description=[self.bridge.session mutableCopy];
                    [description addEntriesFromDictionary:@{@"socket":self.bridge.socketPath,@"width":@(display.width),@"height":@(display.height),@"schema":@1}];
                    writeJSON([self.directory stringByAppendingPathComponent:@"bridge.json"],description);
                }
                [self record:@"capture_target" values:@{@"display_id":@(display.displayID),
                    @"window_id":@(own.windowID),@"width":@(display.width),@"height":@(display.height),
                    @"recovery_attempt":@(self.recoveryAttempts)}];
            }
            config.minimumFrameInterval=CMTimeMake(1,self.captureFPS); config.pixelFormat=kCVPixelFormatType_32BGRA;
            config.showsCursor=NO; config.queueDepth=3; config.capturesAudio=NO;
            if (@available(macOS 14.0,*)) { config.ignoreShadowsSingleWindow=YES; config.captureResolution=SCCaptureResolutionNominal; }
            self.stream = [[SCStream alloc] initWithFilter:filter configuration:config delegate:self];
            self.captureConfiguration=config;self.captureFilter=filter;self.captureRefreshing=NO;
            NSError *addError;
            if (![self.stream addStreamOutput:self type:SCStreamOutputTypeScreen sampleHandlerQueue:dispatch_get_main_queue() error:&addError]) {
                [self record:@"capture_error" values:@{@"message":addError.localizedDescription}]; return;
            }
            SCStream *startingStream=self.stream;
            self.captureStarting=YES;
            [self.stream startCaptureWithCompletionHandler:^(NSError *startError) {
                dispatch_async(dispatch_get_main_queue(),^{
                    if(self.finishing || !self.connected || startingStream!=self.stream)return;
                    self.captureStarting=NO;
                    self.capturing = !startError;self.captureError=startError ? startError.localizedDescription : nil;
                    [self record:startError ? @"capture_error" : @"capture_started"
                        values:startError ? @{@"message":startError.localizedDescription,@"code":@(startError.code)} : @{}];
                });
            }];
        });
    };
    if ([self.mode isEqual:@"window"]) {
        if (@available(macOS 14.4,*)) { [SCShareableContent getCurrentProcessShareableContentWithCompletionHandler:ready]; return; }
    }
    [SCShareableContent getShareableContentExcludingDesktopWindows:NO onScreenWindowsOnly:NO completionHandler:ready];
}
- (void)sessionBecameActive:(NSNotification *)notification {
    (void)notification;
    if(self.bridgeEnabled && [self.mode isEqual:@"desktop"] && self.connected &&
       !self.captureStarting && (!self.bridge.lastCaptureSignal || nowMS()-self.bridge.lastCaptureSignal>3000))
        [self recoverStalledCapture];
}
- (void)applicationDidBecomeActive:(NSNotification *)notification {
    // Unlock can reactivate this app without changing the Aqua session.
    // This is only a retry hint; fresh capture and the same-UID checks still
    // decide when input becomes available again.
    if(self.bridge.lastCaptureSignal)[self sessionBecameActive:notification];
}
- (void)recoverStalledCapture {
    if(self.captureStarting || self.finishing || !self.connected || !CGPreflightScreenCaptureAccess())return;
    NSDictionary *current=identity();
    if(![current[@"gui_uid"] isEqual:self.initialIdentity[@"gui_uid"]] ||
       ![current[@"audit_session_id"] isEqual:self.initialIdentity[@"audit_session_id"]] || ![current[@"login_complete"] boolValue])return;
    self.idleBaseline=nil;
    [self.bridge pauseCapture];self.capturing=NO;self.recoveryAttempts++;self.lastRecoveryAt=nowMS();
    self.awaitingCaptureProof=YES;self.recoveryBuffer=nil;self.firstRecoverySignal=0;
    // After a lock or virtual display transition the small anchor can retain
    // an old origin. Reposition only this process's invisible anchor when
    // AppKit reports exactly one screen in this GUI session. startCapture
    // must still independently match that owned window to one SCDisplay.
    NSArray<NSScreen *> *screens=NSScreen.screens;
    if(self.backgroundMode && screens.count==1) {
        NSRect bounds=screens.firstObject.frame;
        if(NSWidth(bounds)>=12 && NSHeight(bounds)>=12) {
            [self.window setFrameOrigin:NSMakePoint(NSMinX(bounds)+2,NSMinY(bounds)+2)];
            [self.window orderBack:nil];
        }
    }
    SCStream *old=self.stream;self.stream=nil;
    [old stopCaptureWithCompletionHandler:^(NSError *error){(void)error;}];
    [self record:@"capture_recovery" values:@{@"reason":@"no_capture_signal",@"recovery_attempt":@(self.recoveryAttempts)}];
    [self startCapture];
}
- (void)refreshQuietCapture {
    if(!self.bridgeEnabled || !self.capturing || self.captureStarting || self.captureRefreshing ||
       self.finishing || !self.connected || !self.stream || !self.captureConfiguration || !self.captureFilter ||
       !self.lastCaptureCallback || (self.lastFrame && nowMS()-self.lastFrame<1000) ||
       nowMS()-self.lastCaptureRefresh<2000 || !CGPreflightScreenCaptureAccess() || ![self.bridge sameSession])return;
    // An Idle callback can reuse the last Complete sample's timestamp. Such
    // callbacks do not prove fresh pixels, and their arrival must not prevent
    // refresh. Also refresh during recovery, when its baseline is cleared.
    // Request a real frame through the exact same account/display filter. Discard the
    // one-shot result: only subsequent normal stream samples, with their
    // existing timestamp/status checks, may refresh pixels or authorize input.
    if(@available(macOS 14.0,*)) {
        self.captureRefreshing=YES;self.lastCaptureRefresh=nowMS();SCStream *requested=self.stream;
        uint64_t at=nowMS();
        [SCScreenshotManager captureSampleBufferWithFilter:self.captureFilter configuration:self.captureConfiguration
            completionHandler:^(CMSampleBufferRef sample,NSError *error){
            BOOL success=!error;
            double age=sample ? CMTimeGetSeconds(CMTimeSubtract(CMClockGetTime(CMClockGetHostTimeClock()),CMSampleBufferGetPresentationTimeStamp(sample)))*1000 : NAN;
            dispatch_async(dispatch_get_main_queue(),^{
                if(requested==self.stream && !self.finishing) {
                    self.captureRefreshing=NO;
                    [self record:@"capture_snapshot" values:@{@"request_succeeded":@(success),@"request_ms":@(nowMS()-at),
                        @"sample_pts_age_ms":isfinite(age) ? @(age) : NSNull.null,@"stream_proof_required":@YES}];
                }
            });
        }];
    }
}
- (BOOL)acceptRecoveryBuffer:(CVPixelBufferRef)buffer {
    if(!self.awaitingCaptureProof)return YES;
    uint64_t now=nowMS();
    if(!self.recoveryBuffer || now-self.firstRecoverySignal>=3000) {
        self.recoveryBuffer=(__bridge id)buffer;self.firstRecoverySignal=now;return NO;
    }
    if(now-self.firstRecoverySignal<100)return NO;
    self.awaitingCaptureProof=NO;self.recoveryBuffer=nil;self.recoveryAttempts=0;
    return YES;
}
- (void)stream:(SCStream *)stream didOutputSampleBuffer:(CMSampleBufferRef)sample ofType:(SCStreamOutputType)type {
    if (stream!=self.stream || type!=SCStreamOutputTypeScreen || self.finishing || !self.connected) return;
    NSArray *attachments = (__bridge NSArray *)CMSampleBufferGetSampleAttachmentsArray(sample,false);
    NSNumber *status = attachments.firstObject[SCStreamFrameInfoStatus];
    CVPixelBufferRef buffer = CMSampleBufferGetImageBuffer(sample);
    M1Marker marker = {0}; BOOL decoded=NO;
    uint64_t now=nowMS();
    if(self.lastCaptureCallback && now-self.lastCaptureCallback>1000)
        [self record:@"capture_callback_gap" values:@{@"gap_ms":@(now-self.lastCaptureCallback),@"main_tick_age_ms":self.lastTickAt ? @(now-self.lastTickAt) : NSNull.null,@"status":status ?: @(-1)}];
    self.lastCaptureCallback=now;
    BOOL diagnose=!self.bridgeEnabled || now-self.lastFrameDiagnostic>=450;
    if(self.bridgeEnabled && status && status.integerValue==SCFrameStatusComplete) {
        NSNumber *displayTime=attachments.firstObject[SCStreamFrameInfoDisplayTime];
        mach_timebase_info_data_t timebase;mach_timebase_info(&timebase);
        if(![displayTime isKindOfClass:NSNumber.class] ||
           !captureTimeFresh(displayTime.unsignedLongLongValue,mach_absolute_time(),timebase.numer,timebase.denom)) {
            [self.bridge pauseCapture];
            if(now-self.lastDiscardDiagnostic>=1000){
                double age=CMTimeGetSeconds(CMTimeSubtract(CMClockGetTime(CMClockGetHostTimeClock()),CMSampleBufferGetPresentationTimeStamp(sample)))*1000;
                self.lastDiscardDiagnostic=now;[self record:@"capture_frame_discarded" values:@{@"reason":@"stale_or_missing_display_time",
                    @"sample_pts_age_ms":isfinite(age) ? @(age) : NSNull.null,
                    @"display_time":displayTime ?: NSNull.null,@"host_time":@(mach_absolute_time())}];
            }
            return;
        }
    }
    if(status && status.integerValue==SCFrameStatusIdle) {
        // A fresh Idle sample is positive proof of an unchanged live source.
        // A watchdog may have cleared the transport's baseline during a GUI
        // stall; retain a separate source baseline until SCK invalidates it.
        double idleAge=CMTimeGetSeconds(CMTimeSubtract(CMClockGetTime(CMClockGetHostTimeClock()),CMSampleBufferGetPresentationTimeStamp(sample)))*1000;
        if(!captureIdleTimeFresh(idleAge)){
            [self.bridge pauseCapture];
            if(now-self.lastDiscardDiagnostic>=1000){self.lastDiscardDiagnostic=now;
                [self record:@"capture_frame_discarded" values:@{@"reason":@"stale_idle_sample_time",
                    @"sample_pts_age_ms":isfinite(idleAge) ? @(idleAge) : NSNull.null}];}
            return;
        }
        id cached=self.recoveryBuffer;
        if(self.awaitingCaptureProof && cached && [self acceptRecoveryBuffer:(__bridge CVPixelBufferRef)cached]) {
            self.idleBaseline=cached;
            [self.bridge publish:(__bridge CVPixelBufferRef)cached];self.lastFrame=now;
        }
        if(!self.awaitingCaptureProof && self.idleBaseline &&
           (self.bridge.capturePaused || !self.bridge.lastCaptureSignal || now-self.bridge.lastCaptureSignal>3000)) {
            [self.bridge publish:(__bridge CVPixelBufferRef)self.idleBaseline];self.lastFrame=now;
            [self record:@"capture_idle_resumed" values:@{@"sample_pts_age_ms":@(idleAge)}];
        }
        [self.bridge captureIdle];
    }
    if(status && (status.integerValue==SCFrameStatusBlank || status.integerValue==SCFrameStatusSuspended || status.integerValue==SCFrameStatusStopped)) {
        [self record:@"capture_invalidated" values:@{@"status":status}];
        self.idleBaseline=nil;self.recoveryBuffer=nil;self.awaitingCaptureProof=YES;
        [self.bridge pauseCapture];
    }
    if (buffer && status && status.integerValue==SCFrameStatusComplete) {
        if(diagnose && !self.backgroundMode && CVPixelBufferLockBaseAddress(buffer,kCVPixelBufferLock_ReadOnly)==kCVReturnSuccess) {
            decoded = m1Decode(CVPixelBufferGetBaseAddress(buffer),CVPixelBufferGetWidth(buffer),CVPixelBufferGetHeight(buffer),CVPixelBufferGetBytesPerRow(buffer),&marker);
            CVPixelBufferUnlockBaseAddress(buffer,kCVPixelBufferLock_ReadOnly);
        }
        id owned=self.bridgeEnabled ? CFBridgingRelease(copyCapturePixels(buffer)) : (__bridge id)buffer;
        if(!owned) {
            self.idleBaseline=nil;self.recoveryBuffer=nil;self.awaitingCaptureProof=YES;
            [self.bridge pauseCapture];return;
        }
        CVPixelBufferRef snapshot=(__bridge CVPixelBufferRef)owned;
        if([self acceptRecoveryBuffer:snapshot]) {
            self.idleBaseline=owned;
            [self.bridge publish:snapshot];
            self.frames++; self.lastFrame=nowMS();
        }
        if (decoded && marker.uid==getuid() && marker.nonce==self.nonce) {
            if (marker.tick>self.capturedTick) self.lastChanged=nowMS();
            self.capturedTick=marker.tick;
        }
    }
    if(!diagnose)return;
    self.lastFrameDiagnostic=now;
    double presentationAge=CMTimeGetSeconds(CMTimeSubtract(CMClockGetTime(CMClockGetHostTimeClock()),CMSampleBufferGetPresentationTimeStamp(sample)))*1000;
    [self record:@"frame" values:@{@"status":status ?: @(-1), @"decoded":@(decoded),
        @"sample_pts_age_ms":isfinite(presentationAge) ? @(presentationAge) : NSNull.null,
        @"marker_uid":@(marker.uid),@"marker_nonce":@(marker.nonce),@"marker_tick":@(marker.tick),
        @"drawn_tick":@(self.tick),@"width":buffer ? @(CVPixelBufferGetWidth(buffer)) : @0,
        @"height":buffer ? @(CVPixelBufferGetHeight(buffer)) : @0}];
}
- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    dispatch_async(dispatch_get_main_queue(),^{
        if (self.finishing || stream!=self.stream) return;
        self.capturing=NO;
        self.idleBaseline=nil;self.recoveryBuffer=nil;self.awaitingCaptureProof=YES;
        [self.bridge pauseCapture];
        // A fast user switch can replace this GUI session's display ID.
        // Re-enumerate only in this same GUI process and recheck window owner;
        // never reuse another user's filter or fall back to the physical console.
        if ([self.mode isEqual:@"desktop"] && error.code==SCStreamErrorNoCaptureSource &&
            [error.domain isEqual:SCStreamErrorDomain] && self.recoveryAttempts<3 && CGPreflightScreenCaptureAccess()) {
            self.recoveryAttempts++;
            [self record:@"capture_interrupted" values:@{@"message":error.localizedDescription,
                @"code":@(error.code),@"recovery_attempt":@(self.recoveryAttempts)}];
            self.stream=nil;
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC),dispatch_get_main_queue(),^{
                if (!self.finishing && self.connected) [self startCapture];
            });
        } else {
            [self record:@"capture_error" values:@{@"message":error.localizedDescription,@"code":@(error.code)}];
        }
    });
}
- (BOOL)takeCommand:(NSString *)name {
    NSString *path = [self.directory stringByAppendingPathComponent:name];
    struct stat s;
    if (lstat(path.fileSystemRepresentation,&s)!=0 || !S_ISREG(s.st_mode) || s.st_uid!=getuid()) return NO;
    return unlink(path.fileSystemRepresentation)==0;
}
- (void)tickTimer:(NSTimer *)timer {
    self.lastTickAt=nowMS();
    (void)timer;
    [self refreshQuietCapture];
    if(self.guardian && ![audioProcess([self.guardian[@"pid"] intValue]) isEqual:self.guardian]){[self record:@"guardian_lost" values:@{}];[self finish];return;}
    if(self.serviceMode) {
        NSDictionary *command=desktopControlCommand(self.initialIdentity,self.nonce,self.lastControlCommand);
        if(command) {
            self.lastControlCommand=command[@"id"];[self.bridge closePeer];
            [self record:@"management_disconnect" values:@{@"command_id":self.lastControlCommand}];
        }
    }
    BOOL activeRemote=self.serviceMode && self.bridge.client>=0;
    if(activeRemote && !self.remoteActivity)
        self.remoteActivity=[NSProcessInfo.processInfo beginActivityWithOptions:NSActivityUserInitiated reason:@"Active authenticated remote desktop session"];
    if(!activeRemote && self.remoteActivity) {
        [NSProcessInfo.processInfo endActivity:self.remoteActivity];self.remoteActivity=nil;
    }
    [self refreshAudio]; self.tick++;
    [self.view setNeedsDisplay:YES]; [self.view displayIfNeeded];
    [self record:@"heartbeat" values:@{@"drawn_tick":@(self.tick),@"captured_tick":@(self.capturedTick),
        @"frame_age_ms":self.lastFrame ? @(nowMS()-self.lastFrame) : NSNull.null,
        @"change_age_ms":self.lastChanged ? @(nowMS()-self.lastChanged) : NSNull.null,
        @"frontmost_pid":@(NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier),
        @"window_key":@(self.window.isKeyWindow),@"accessibility_trusted":@(AXIsProcessTrusted()),
        @"screen_capture_preflight":@(CGPreflightScreenCaptureAccess()),@"connected":@(self.connected),@"epoch":@(self.epoch)}];
    NSMutableDictionary *state=[identity() mutableCopy];
    [state addEntriesFromDictionary:@{@"run_id":self.run,@"nonce":@(self.nonce),@"epoch":@(self.epoch),@"connected":@(self.connected),@"pid":@(getpid()),@"boot_id":kernelString("kern.bootsessionuuid"),@"uptime_ms":@(nowMS()),
        @"management_schema":@1,@"guardian_pid":self.guardian[@"pid"] ?: @0,@"background":@(self.backgroundMode),@"screen_capture":@(CGPreflightScreenCaptureAccess()),
        @"accessibility":@(AXIsProcessTrusted()),@"capturing":@(self.capturing),
        @"rdp_connected":@(self.bridge.client>=0),@"capture_paused":@(self.bridge.capturePaused),
        @"last_capture_ms":@(self.bridge.lastCaptureSignal),@"display_id":@(self.targetDisplay),
        @"capture_error":self.captureError ?: @"",@"last_control_command":self.lastControlCommand ?: @""}];
    writeJSON([self.directory stringByAppendingPathComponent:@"state.json"],state);
    NSDictionary *current=identity();
    if (![current[@"gui_uid"] isEqual:self.initialIdentity[@"gui_uid"]] ||
        ![current[@"audit_session_id"] isEqual:self.initialIdentity[@"audit_session_id"]] ||
        ![current[@"login_complete"] boolValue]) { [self record:@"error" values:@{@"message":@"GUI identity changed"}]; [self finish]; return; }
    if (!self.serviceMode && [self takeCommand:@"request-accessibility"]) {
        AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)@{(__bridge NSString *)kAXTrustedCheckOptionPrompt:@YES});
    }
    if (!self.serviceMode && [self takeCommand:@"request-disconnect"]) [self disconnectCapture:@"requested"];
    if(self.bridgeEnabled && [self.mode isEqual:@"desktop"] && self.connected && !self.captureStarting &&
       (!self.bridge.lastCaptureSignal || nowMS()-self.bridge.lastCaptureSignal>5000) &&
       nowMS()-self.lastRecoveryAt>MIN(30000ULL,5000ULL*(self.recoveryAttempts+1)))
        [self recoverStalledCapture];
    if (!self.serviceMode && [self takeCommand:@"request-reconnect"]) {
        if(!self.connected && CGPreflightScreenCaptureAccess()) {
            self.connected=YES;self.epoch++;self.recoveryAttempts=0;
            [self record:@"reconnected" values:@{@"epoch":@(self.epoch)}];[self startCapture];
        } else [self record:@"reconnect_rejected" values:@{@"epoch":@(self.epoch)}];
    }
    if(!self.serviceMode) {
        if([NSFileManager.defaultManager fileExistsAtPath:[self.directory stringByAppendingPathComponent:@"request-script.json"]]) [self beginScript];
        if ([self takeCommand:@"request-input"]) [self beginInput];
        if ([self takeCommand:@"request-stop"]) [self finish];
    }
    if (self.duration && nowMS()-self.started>=self.duration*1000) [self finish];
}
- (void)receive:(NSEvent *)event {
    // M1's synthetic input diagnostics must never record service keystrokes.
    if(self.serviceMode)return;
    self.received++;
    CGEventRef cg=event.CGEvent;
    int64_t tag=cg ? CGEventGetIntegerValueField(cg,kCGEventSourceUserData) : 0;
    BOOL keyboard=event.type==NSEventTypeKeyDown || event.type==NSEventTypeKeyUp;
    // Only this process's nonce may carry text; foreign tags still identify
    // cross-session delivery without recording another source's characters.
    BOOL ownTag=(uint32_t)((uint64_t)tag>>32)==self.nonce;
    [self record:@"input_received" values:@{@"tag":[NSString stringWithFormat:@"%016llx",(unsigned long long)tag],
        @"event_type":@(event.type),@"text":keyboard && ownTag ? (event.characters ?: @"") : @"",
        @"x":@(event.locationInWindow.x),@"y":@(event.locationInWindow.y)}];
}
- (void)beginInput {
    if (!self.connected || !self.capturing || self.scriptTimer || self.inputTimer || self.inputTrial>0 || !AXIsProcessTrusted() ||
        NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier!=getpid()) {
        [self record:@"input_rejected" values:@{@"message":@"Requires Accessibility, this app as session focus, and one batch per run"}]; return;
    }
    [self record:@"input_started" values:@{@"trials":@100,@"route":@"kCGSessionEventTap"}];
    self.inputTimer=[NSTimer scheduledTimerWithTimeInterval:0.2 target:self selector:@selector(sendTrial:) userInfo:nil repeats:YES];
}
- (void)sendTrial:(NSTimer *)timer {
    if (!AXIsProcessTrusted() || NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier!=getpid()) {
        [self record:@"input_aborted" values:@{@"message":@"Permission or session focus changed"}]; [timer invalidate]; self.inputTimer=nil; return;
    }
    self.inputTrial++;
    NSRect screen=NSScreen.screens.firstObject.frame;
    NSPoint local=NSMakePoint(100+(self.inputTrial%20)*10,100);
    NSPoint p=[self.window convertPointToScreen:local];
    CGPoint start=CGPointMake(p.x,NSMaxY(screen)-p.y), end=CGPointMake(start.x+12,start.y+8);
    CGEventSourceRef source=CGEventSourceCreate(kCGEventSourceStatePrivate);
    UniChar character=(UniChar)('a'+self.inputTrial%26);
    CGEventRef events[5]={CGEventCreateKeyboardEvent(source,0,true),CGEventCreateKeyboardEvent(source,0,false),
        CGEventCreateMouseEvent(source,kCGEventLeftMouseDown,start,kCGMouseButtonLeft),
        CGEventCreateMouseEvent(source,kCGEventLeftMouseDragged,end,kCGMouseButtonLeft),
        CGEventCreateMouseEvent(source,kCGEventLeftMouseUp,end,kCGMouseButtonLeft)};
    for (NSUInteger i=0;i<5;i++) {
        if (!events[i]) { [self record:@"input_aborted" values:@{@"message":@"CGEvent allocation failed"}]; continue; }
        uint64_t tag=((uint64_t)self.nonce<<32)|((uint64_t)self.inputTrial<<8)|(i+1);
        CGEventSetIntegerValueField(events[i],kCGEventSourceUserData,(int64_t)tag);
        if (i<2) CGEventKeyboardSetUnicodeString(events[i],1,&character);
        [self record:@"input_post" values:@{@"tag":[NSString stringWithFormat:@"%016llx",(unsigned long long)tag],
            @"event_type":@(CGEventGetType(events[i])),@"trial":@(self.inputTrial),@"route":@"kCGSessionEventTap"}];
        CGEventPost(kCGSessionEventTap,events[i]); CFRelease(events[i]);
    }
    if (source) CFRelease(source);
    if (self.inputTrial>=100) { [timer invalidate]; self.inputTimer=nil; [self record:@"input_finished" values:@{}]; }
}
#include "m1-script.inc"
- (void)applicationWillTerminate:(NSNotification *)notification {
    (void)notification;if(@available(macOS 14.2,*)){[(M1Audio *)self.audio stop];}[self.bridge stop];[self.log synchronizeFile];
}
- (void)finish {
    if (self.finishing) return;
    self.finishing=YES; [self.timer invalidate]; [self.inputTimer invalidate]; [self.scriptTimer invalidate];
    if(@available(macOS 14.2,*)){[(M1Audio *)self.audio stop];}self.audio=nil;[self.bridge stop];self.bridge=nil;
    if(self.originalInputSource) selectInputSource(self.originalInputSource);
    [self record:@"stop" values:@{@"frames":@(self.frames),@"input_trials":@(self.inputTrial)}];
    if (self.capturing) [self.stream stopCaptureWithCompletionHandler:^(NSError *error) { (void)error; }];
    if (self.activity) [NSProcessInfo.processInfo endActivity:self.activity];
    if (self.remoteActivity) [NSProcessInfo.processInfo endActivity:self.remoteActivity];
    [self.log synchronizeFile]; [NSApp terminate:nil];
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        umask(077);
        if(argc==1) {
            // Opening the installed Desktop bundle should lead to useful UI,
            // rather than silently exit because service arguments are absent.
            [NSApplication sharedApplication];
            return [NSWorkspace.sharedWorkspace openURL:[NSURL fileURLWithPath:@"/Applications/ZMacRDS Manager.app"]] ? 0 : 1;
        }
        if(argc<4)return 2;
        BOOL bridge=NO,service=NO,audio=NO,background=NO;long fps=30;pid_t guardian=0;
        for(int i=4;i<argc;i++) {
            if(!strcmp(argv[i],"--rdp-bridge") && !bridge)bridge=YES;
            else if(!strcmp(argv[i],"--service") && !service)service=YES;
            else if(!strcmp(argv[i],"--audio") && !audio)audio=YES;
            else if(!strcmp(argv[i],"--background") && !background)background=YES;
            else if(!strcmp(argv[i],"--guardian") && i+1<argc && !guardian){char *e=NULL;long g=strtol(argv[++i],&e,10);if(*e || g<=1 || g>INT_MAX)return 2;guardian=(pid_t)g;}
            else if(!strcmp(argv[i],"--fps") && i+1<argc) {char *e=NULL;fps=strtol(argv[++i],&e,10);if(!*argv[i] || *e || fps<1 || fps>30)return 2;}
            else return 2;
        }
        if((strcmp(argv[2],"window") && strcmp(argv[2],"desktop")) || ((bridge || service || audio || background) && strcmp(argv[2],"desktop")) || ((service || audio || background) && !bridge))return 2;
        if(!bridge)fps=2;
        NSString *run=@(argv[1]);
        NSCharacterSet *allowed=[NSCharacterSet characterSetWithCharactersInString:@"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"];
        char *end=NULL; long duration=strtol(argv[3],&end,10);
        if (!run.length || run.length>48 || [run rangeOfCharacterFromSet:allowed.invertedSet].location!=NSNotFound ||
            !end || *end || (service ? duration!=0 : (duration<30 || duration>3600))) return 2;
        M1Agent *agent=[M1Agent new]; agent.run=run; agent.mode=@(argv[2]); agent.duration=duration;
        agent.bridgeEnabled=bridge;agent.captureFPS=(int)fps;agent.serviceMode=service;agent.audioEnabled=audio;
        agent.backgroundMode=background || service;
        if(guardian){agent.guardian=audioProcess(guardian);if(!service || !agent.guardian)return 2;}
        do { agent.nonce=arc4random(); } while (!agent.nonce);
        NSString *parent=[NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/ZMacRDS/M1"];
        NSError *error;
        if (![NSFileManager.defaultManager createDirectoryAtPath:parent withIntermediateDirectories:YES attributes:@{NSFilePosixPermissions:@0700} error:&error]) return 1;
        agent.directory=service ? [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/ZMacRDS/Service"] : [parent stringByAppendingPathComponent:run];
        if(mkdir(agent.directory.fileSystemRepresentation,0700)!=0 && !(service && errno==EEXIST)){perror("Use a new run ID");return 1;}
        if(service) {
            int fd=open([agent.directory stringByAppendingPathComponent:@"agent.lock"].fileSystemRepresentation,O_CREAT|O_RDWR|O_NOFOLLOW,0600);
            if(fd<0 || flock(fd,LOCK_EX|LOCK_NB)!=0)return 1;
            // Keep this descriptor for this GUI process lifetime.
            fcntl(fd,F_SETFD,FD_CLOEXEC);
        }
        NSString *path=[agent.directory stringByAppendingPathComponent:@"events.jsonl"];
        if (![NSFileManager.defaultManager fileExistsAtPath:path] && ![NSFileManager.defaultManager createFileAtPath:path contents:nil attributes:@{NSFilePosixPermissions:@0600}]) return 1;
        agent.log=[NSFileHandle fileHandleForWritingAtPath:path];[agent.log seekToEndOfFile];
        [NSApplication sharedApplication]; NSApp.delegate=agent; [NSApp setActivationPolicy:agent.backgroundMode ? NSApplicationActivationPolicyAccessory : NSApplicationActivationPolicyRegular];
        [NSApp run];
    }
    return 0;
}
