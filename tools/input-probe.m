#import <Foundation/Foundation.h>
#import <Carbon/Carbon.h>
int main(void) { @autoreleasepool {
    NSArray *sources=CFBridgingRelease(TISCreateInputSourceList(NULL,true));NSMutableArray *out=[NSMutableArray new];
    for(id item in sources){TISInputSourceRef source=(__bridge TISInputSourceRef)item;
        NSString *identifier=(__bridge NSString *)TISGetInputSourceProperty(source,kTISPropertyInputSourceID);
        if([identifier containsString:@"SCIM"] || [identifier containsString:@"ABC"]) {
            [out addObject:@{@"id":identifier,@"type":(__bridge id)TISGetInputSourceProperty(source,kTISPropertyInputSourceType),@"enabled":(__bridge id)TISGetInputSourceProperty(source,kTISPropertyInputSourceIsEnabled),@"selectable":(__bridge id)TISGetInputSourceProperty(source,kTISPropertyInputSourceIsSelectCapable),@"bundle":(__bridge id)TISGetInputSourceProperty(source,kTISPropertyBundleID) ?: @""}];
        }
    }
    puts([[NSString alloc] initWithData:[NSJSONSerialization dataWithJSONObject:out options:0 error:NULL] encoding:NSUTF8StringEncoding].UTF8String);
}return 0;}
