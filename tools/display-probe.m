#import <Foundation/Foundation.h>
#import <ApplicationServices/ApplicationServices.h>
#include <unistd.h>

// Read-only follow-up when an SCStream loses its display on user switch.
// No capture, permission request, account switch or display configuration.
int main(void) {
    @autoreleasepool {
        CGDirectDisplayID online[32]={0}, active[32]={0};
        uint32_t onlineCount=0, activeCount=0;
        CGError onlineError=CGGetOnlineDisplayList(32,online,&onlineCount);
        CGError activeError=CGGetActiveDisplayList(32,active,&activeCount);
        NSMutableArray *displays=[NSMutableArray array];
        for (uint32_t i=0; i<onlineCount; i++) {
            CGDirectDisplayID d=online[i]; CGRect r=CGDisplayBounds(d);
            [displays addObject:@{@"display_id":@(d),@"active":@(CGDisplayIsActive(d)!=0),
                @"online":@(CGDisplayIsOnline(d)!=0),@"asleep":@(CGDisplayIsAsleep(d)!=0),
                @"main":@(CGDisplayIsMain(d)!=0),@"width":@(CGDisplayPixelsWide(d)),
                @"height":@(CGDisplayPixelsHigh(d)),@"bounds":@[@(r.origin.x),@(r.origin.y),@(r.size.width),@(r.size.height)]}];
        }
        NSDictionary *session=CFBridgingRelease(CGSessionCopyCurrentDictionary());
        CGEventRef event=CGEventCreate(NULL);
        CGPoint pointer=event ? CGEventGetLocation(event) : CGPointZero;
        if(event)CFRelease(event);
        NSMutableArray *hits=[NSMutableArray new];
        NSArray *windows=CFBridgingRelease(CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly|kCGWindowListExcludeDesktopElements,kCGNullWindowID));
        for(NSDictionary *window in windows) {
            CGRect bounds;
            if(CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)window[(__bridge NSString *)kCGWindowBounds],&bounds) && CGRectContainsPoint(bounds,pointer)) {
                [hits addObject:@{@"pid":window[(__bridge NSString *)kCGWindowOwnerPID] ?: @0,
                    @"layer":window[(__bridge NSString *)kCGWindowLayer] ?: @0,
                    @"bounds":@[@(bounds.origin.x),@(bounds.origin.y),@(bounds.size.width),@(bounds.size.height)]}];
                if(hits.count>=16)break;
            }
        }
        NSDictionary *report=@{@"uid":@(getuid()),@"gui_uid":session[(__bridge NSString *)kCGSessionUserIDKey] ?: NSNull.null,
            @"pointer":@[@(pointer.x),@(pointer.y)],
            @"pointer_windows":hits,
            @"on_console":session[(__bridge NSString *)kCGSessionOnConsoleKey] ?: NSNull.null,
            @"main_display":@(CGMainDisplayID()),@"online_error":@(onlineError),@"active_error":@(activeError),
            @"online_count":@(onlineCount),@"active_count":@(activeCount),@"displays":displays};
        NSData *data=[NSJSONSerialization dataWithJSONObject:report options:NSJSONWritingSortedKeys error:NULL];
        if (!data) return 1;
        fwrite(data.bytes,1,data.length,stdout); putchar('\n');
        return onlineError || activeError ? 1 : 0;
    }
}
