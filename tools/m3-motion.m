#import <AppKit/AppKit.h>
#include <unistd.h>
#include "m1-marker.h"

// A bounded visual workload for a logged-in test desktop. The strip identifies
// the GUI user and source tick. No desktop capture or input permissions needed.
@interface MotionView : NSView
@property uint32_t tick, nonce;
@end
@implementation MotionView
- (BOOL)isFlipped {return YES;}
- (void)drawRect:(NSRect)dirty {
    (void)dirty;
    [NSColor.darkGrayColor setFill]; NSRectFill(self.bounds);
    uint8_t bytes[M1_BYTES];
    m1Encode((M1Marker){getuid(),self.nonce,self.tick},bytes);
    for(int i=0;i<M1_CELLS;i++) {
        NSColor *c;
        if(i<3)c=i==1 ? NSColor.cyanColor : NSColor.magentaColor;
        else {int b=i-3;c=(bytes[b/8]&(1<<(7-b%8))) ? NSColor.whiteColor : NSColor.blackColor;}
        [c setFill]; NSRectFill(NSMakeRect(24+i*M1_CELL,16,M1_CELL,24));
    }
    [NSColor.orangeColor setFill];
    NSRectFill(NSMakeRect(24+self.tick%400,72,96,48));
}
@end
int main(int argc,const char **argv) {
    @autoreleasepool {
        if(argc!=3 && argc!=4)return 2;
        BOOL corner=argc==4 && strcmp(argv[3],"--corner")==0;
        if(argc==4 && !corner)return 2;
        char *end=NULL;
        unsigned long nonce=strtoul(argv[1],&end,10);
        if(!nonce || nonce>UINT32_MAX || *end)return 2;
        long seconds=strtol(argv[2],&end,10);
        if(seconds<10 || seconds>3600 || *end)return 2;
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
        NSRect screen=NSScreen.mainScreen.frame;
        NSWindow *w=[[NSWindow alloc] initWithContentRect:corner ? NSMakeRect(24,40,600,80) : NSMakeRect(980,screen.size.height-480,600,160)
              styleMask:NSWindowStyleMaskBorderless backing:NSBackingStoreBuffered defer:NO];
        MotionView *view=[[MotionView alloc] initWithFrame:NSMakeRect(0,0,600,160)];
        view.nonce=(uint32_t)nonce;
        w.contentView=view; w.level=NSFloatingWindowLevel;w.ignoresMouseEvents=YES;[w orderFrontRegardless];
        NSWindow *moving=[[NSWindow alloc] initWithContentRect:corner ? NSMakeRect(1500,200,260,100) : NSMakeRect(950,200,420,200)
             styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
        moving.title=@"ZMacRDS moving test window"; moving.level=NSFloatingWindowLevel;moving.ignoresMouseEvents=YES;
        moving.backgroundColor=NSColor.systemBlueColor; [moving orderFrontRegardless];
        NSTimeInterval started=NSProcessInfo.processInfo.systemUptime;
        [NSTimer scheduledTimerWithTimeInterval:1./30 repeats:YES block:^(NSTimer *timer) {
            if(NSProcessInfo.processInfo.systemUptime-started>=seconds) {
                [timer invalidate];[NSApp terminate:nil];return;
            }
            view.tick++;
            [moving setFrameOrigin:NSMakePoint((corner ? 1390 : 840)+abs((int)(view.tick%240)-120)*2,200)];
            [view setNeedsDisplay:YES];[view displayIfNeeded];
        }];
        [NSApp run];
    }
    return 0;
}
