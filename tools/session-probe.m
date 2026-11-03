#import <ApplicationServices/ApplicationServices.h>
#import <Foundation/Foundation.h>
#include <bsm/libbsm.h>
#include <mach/mach.h>
#include <unistd.h>

int main(void) {
    @autoreleasepool {
        audit_token_t token;
        mach_msg_type_number_t count = TASK_AUDIT_TOKEN_COUNT;
        kern_return_t result = task_info(mach_task_self(), TASK_AUDIT_TOKEN,
                                         (task_info_t)&token, &count);
        if (result != KERN_SUCCESS || count != TASK_AUDIT_TOKEN_COUNT) {
            fprintf(stderr, "Could not read the process audit token.\n");
            return 1;
        }

        NSDictionary *session = CFBridgingRelease(CGSessionCopyCurrentDictionary());
        NSDictionary *report = @{
            @"uid": @(getuid()),
            @"audit_session_id": @(audit_token_to_asid(token)),
            @"has_gui_session": session != nil ? @YES : @NO,
            @"gui_uid": session[(__bridge NSString *)kCGSessionUserIDKey] ?: NSNull.null,
            @"on_console": session[(__bridge NSString *)kCGSessionOnConsoleKey] ?: NSNull.null,
            @"login_complete": session[(__bridge NSString *)kCGSessionLoginDoneKey] ?: NSNull.null,
            @"screen_capture_preflight": @(CGPreflightScreenCaptureAccess()),
            @"accessibility_trusted": AXIsProcessTrusted() ? @YES : @NO
        };
        NSError *error = nil;
        NSData *json = [NSJSONSerialization dataWithJSONObject:report
                                                      options:NSJSONWritingSortedKeys
                                                        error:&error];
        if (!json) {
            fprintf(stderr, "%s\n", error.localizedDescription.UTF8String);
            return 1;
        }
        fwrite(json.bytes, 1, json.length, stdout);
        return puts("") == EOF || ferror(stdout) ? 1 : 0;
    }
}
