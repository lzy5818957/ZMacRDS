#import "m1-common.h"
#import "m1-marker.h"

@class Editor;
@interface ProbeText : NSTextView
@property(weak) Editor *owner;
@end
@interface AckView : NSView
@property(weak) Editor *owner;
@end
@interface Editor : NSObject <NSApplicationDelegate,NSTextViewDelegate>
@property NSString *caseID,*directory;
@property NSFileHandle *log;
@property NSWindow *window;
@property ProbeText *text;
@property AckView *ack;
@property uint32_t nonce;
@property uint64_t started;
@property NSUInteger duration,markedCount;
@property id monitor,activity;
@property NSTimer *timer;
@property BOOL finishing;
@property BOOL secondary;
@property NSUInteger lastAcknowledged;
@property BOOL acknowledgementDrawn;
- (void)record:(NSString *)kind values:(NSDictionary *)values;
- (void)snapshot;
@end
@implementation AckView
- (BOOL)isFlipped { return YES; }
- (void)drawRect:(NSRect)dirty {
    (void)dirty;[NSColor.whiteColor setFill];NSRectFill(self.bounds);
    // A visible acknowledgement of document length, for input-to-pixel
    // measurements. It changes only when text changes, not on a timer.
    uint8_t bytes[M1_BYTES];
    m1Encode((M1Marker){getuid(),self.owner.nonce,(uint32_t)self.owner.text.string.length},bytes);
    for (int cell=0;cell<M1_CELLS;cell++) {
        NSColor *color=cell<3 ? (cell==1 ? NSColor.cyanColor : NSColor.magentaColor) :
            (bytes[(cell-3)/8] & (1<<(7-(cell-3)%8)) ? NSColor.whiteColor : NSColor.blackColor);
        [color setFill];NSRectFill(NSMakeRect(24+cell*M1_CELL,4,M1_CELL,24));
    }
    if(!self.owner.acknowledgementDrawn || self.owner.lastAcknowledged!=self.owner.text.string.length) {
        self.owner.acknowledgementDrawn=YES;self.owner.lastAcknowledged=self.owner.text.string.length;
        [self.owner record:@"ack_drawn" values:@{@"length":@(self.owner.lastAcknowledged)}];
    }
}
@end
@implementation ProbeText
- (void)setMarkedText:(id)string selectedRange:(NSRange)selection replacementRange:(NSRange)replacement {
    self.owner.markedCount++;
    [self.owner record:@"composition" values:@{@"text":[string isKindOfClass:NSAttributedString.class] ? [string string] : string}];
    [super setMarkedText:string selectedRange:selection replacementRange:replacement];
}
- (void)insertText:(id)string replacementRange:(NSRange)range {
    [super insertText:string replacementRange:range];
    [self.owner snapshot];
}
@end
@implementation Editor
- (void)record:(NSString *)kind values:(NSDictionary *)values {
    NSMutableDictionary *row=[identity() mutableCopy];
    [row addEntriesFromDictionary:@{@"schema":@1,@"kind":kind,@"case_id":self.caseID,@"pid":@(getpid()),@"nonce":@(self.nonce),@"uptime_ms":@(nowMS()),@"boot_id":kernelString("kern.bootsessionuuid"),@"input_source":inputSourceID()}];
    [row addEntriesFromDictionary:values];
    [self.log writeData:[NSJSONSerialization dataWithJSONObject:row options:NSJSONWritingSortedKeys error:NULL]];
    [self.log writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
}
- (void)snapshot {
    NSDictionary *state=@{@"text":self.text.string ?: @"",@"marked":@(self.text.hasMarkedText),@"compositions":@(self.markedCount),@"selection_start":@(self.text.selectedRange.location),@"selection_length":@(self.text.selectedRange.length),@"frontmost_pid":@(NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier)};
    [self record:@"document" values:state];
    NSMutableDictionary *registration=[identity() mutableCopy];
    [registration addEntriesFromDictionary:state];
    [registration addEntriesFromDictionary:@{@"case_id":self.caseID,@"pid":@(getpid()),@"nonce":@(self.nonce),@"window_id":@(self.window.windowNumber),@"boot_id":kernelString("kern.bootsessionuuid"),@"uptime_ms":@(nowMS()),@"input_source":inputSourceID()}];
    writeJSON([self.directory stringByAppendingPathComponent:@"state.json"],registration);
    [self.text.string writeToFile:[self.directory stringByAppendingPathComponent:@"document.txt"] atomically:YES encoding:NSUTF8StringEncoding error:NULL];
}
- (void)applicationDidFinishLaunching:(NSNotification *)n {
    (void)n;
    NSDictionary *who=identity();
    if(getuid()==0 || ![who[@"gui_uid"] isEqual:@(getuid())] || ![who[@"login_complete"] boolValue]) { [NSApp terminate:nil]; return; }
    self.activity=[NSProcessInfo.processInfo beginActivityWithOptions:NSActivityUserInitiatedAllowingIdleSystemSleep reason:@"Bounded synthetic cross-application test"];
    NSRect frame=self.secondary ? NSMakeRect(150,220,640,480) : NSMakeRect(900,250,700,550);
    CGFloat width=frame.size.width,height=frame.size.height;
    self.window=[[NSWindow alloc] initWithContentRect:frame styleMask:NSWindowStyleMaskTitled|NSWindowStyleMaskClosable|NSWindowStyleMaskMiniaturizable backing:NSBackingStoreBuffered defer:NO];
    self.window.title=[NSString stringWithFormat:@"ZMacRDS Test Editor — UID %u — %@",getuid(),self.caseID];
    self.window.releasedWhenClosed=NO;
    self.text=[[ProbeText alloc] initWithFrame:NSMakeRect(0,0,width,height-40)];self.text.owner=self;self.text.delegate=self;
    self.text.font=[NSFont systemFontOfSize:28];self.text.richText=NO;self.text.automaticQuoteSubstitutionEnabled=NO;self.text.automaticDashSubstitutionEnabled=NO;self.text.automaticSpellingCorrectionEnabled=NO;
    self.text.textContainerInset=NSMakeSize(8,4);
    NSView *container=[[NSView alloc] initWithFrame:NSMakeRect(0,0,width,height)];
    self.ack=[[AckView alloc] initWithFrame:NSMakeRect(0,height-40,width,40)];self.ack.owner=self;
    [container addSubview:self.text];[container addSubview:self.ack];self.window.contentView=container;
    [self.window makeKeyAndOrderFront:nil];[self.window makeFirstResponder:self.text];[NSApp activateIgnoringOtherApps:YES];
    NSMenu *menu=[NSMenu new],*edit=[NSMenu new];NSMenuItem *item=[NSMenuItem new];item.title=@"Edit";item.submenu=edit;[menu addItem:item];
    [edit addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
    [edit addItemWithTitle:@"Undo" action:@selector(undo:) keyEquivalent:@"z"];NSApp.mainMenu=menu;
    __weak Editor *weak=self;
    self.monitor=[NSEvent addLocalMonitorForEventsMatchingMask:NSEventMaskKeyDown|NSEventMaskKeyUp|NSEventMaskLeftMouseDown|NSEventMaskLeftMouseUp|NSEventMaskLeftMouseDragged handler:^NSEvent *(NSEvent *event) {
        Editor *e=weak;uint64_t tag=(uint64_t)CGEventGetIntegerValueField(event.CGEvent,kCGEventSourceUserData);
        BOOL keyboard=event.type==NSEventTypeKeyDown || event.type==NSEventTypeKeyUp;
        [e record:@"event" values:@{@"tag":[NSString stringWithFormat:@"%016llx",(unsigned long long)tag],@"event_type":@(event.type),@"keycode":@(keyboard ? event.keyCode : 0),@"characters":keyboard ? event.characters ?: @"" : @"",@"flags":@(event.modifierFlags),@"keyboard_type":@(CGEventGetIntegerValueField(event.CGEvent,kCGKeyboardEventKeyboardType)),@"source_state":@(CGEventGetIntegerValueField(event.CGEvent,kCGEventSourceStateID)),@"x":@(event.locationInWindow.x),@"y":@(event.locationInWindow.y)}];
        return event;
    }];
    [self record:@"start" values:@{}];[self snapshot];
    self.timer=[NSTimer scheduledTimerWithTimeInterval:1 target:self selector:@selector(tick:) userInfo:nil repeats:YES];
}
- (void)textDidChange:(NSNotification *)notification {
    (void)notification;[self snapshot];[self.ack setNeedsDisplay:YES];[self.ack displayIfNeeded];
}
- (void)textViewDidChangeSelection:(NSNotification *)notification { (void)notification;if(self.window && self.text)[self snapshot]; }
- (void)tick:(NSTimer *)timer {
    (void)timer;[self snapshot];
    NSString *stop=[self.directory stringByAppendingPathComponent:@"request-stop"];
    if(nowMS()-self.started>=self.duration*1000 || [NSFileManager.defaultManager fileExistsAtPath:stop]) { self.finishing=YES;[self record:@"stop" values:@{}];[NSApp terminate:nil]; }
}
- (void)applicationWillTerminate:(NSNotification *)n {
    (void)n;[self snapshot];if(!self.finishing)[self record:@"session_ended" values:@{}];[self.log synchronizeFile];
    [self.timer invalidate];if(self.monitor)[NSEvent removeMonitor:self.monitor];if(self.activity)[NSProcessInfo.processInfo endActivity:self.activity];
}
@end
int main(int argc,const char *argv[]) {
    @autoreleasepool {
        umask(077);if((argc!=3 && argc!=4) || !validRun(@(argv[1])) || (argc==4 && strcmp(argv[3],"--secondary")))return 2;
        char *end;long duration=strtol(argv[2],&end,10);if(*end || duration<30 || duration>3600)return 2;
        Editor *e=[Editor new];e.secondary=argc==4;e.caseID=@(argv[1]);e.duration=duration;e.started=nowMS();do{e.nonce=arc4random();}while(!e.nonce);
        NSString *parent=[NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/ZMacRDS/Editor"];
        [NSFileManager.defaultManager createDirectoryAtPath:parent withIntermediateDirectories:YES attributes:@{NSFilePosixPermissions:@0700} error:NULL];
        e.directory=[parent stringByAppendingPathComponent:e.caseID];if(mkdir(e.directory.fileSystemRepresentation,0700)!=0)return 1;
        NSString *log=[e.directory stringByAppendingPathComponent:@"events.jsonl"];
        [NSFileManager.defaultManager createFileAtPath:log contents:nil attributes:@{NSFilePosixPermissions:@0600}];e.log=[NSFileHandle fileHandleForWritingAtPath:log];
        [NSApplication sharedApplication];NSApp.delegate=e;[NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];[NSApp run];
    }return 0;
}
