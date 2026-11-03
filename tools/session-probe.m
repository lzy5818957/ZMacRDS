#import <ApplicationServices/ApplicationServices.h>
#import <Foundation/Foundation.h>
#import <SystemConfiguration/SystemConfiguration.h>
#include <bsm/libbsm.h>
#include <mach/mach.h>
#include <sys/sysctl.h>
#include <time.h>
#include <unistd.h>

static NSString *kernelString(const char *name) {
    char value[128];
    size_t length = sizeof(value);
    if (sysctlbyname(name, value, &length, NULL, 0) != 0 || !length) {
        fprintf(stderr, "Could not read %s.\n", name);
        return nil;
    }
    return [NSString stringWithUTF8String:value];
}

static BOOL writeSample(NSDictionary *run) {
    audit_token_t token;
    mach_msg_type_number_t count = TASK_AUDIT_TOKEN_COUNT;
    kern_return_t result = task_info(mach_task_self(), TASK_AUDIT_TOKEN,
                                     (task_info_t)&token, &count);
    if (result != KERN_SUCCESS || count != TASK_AUDIT_TOKEN_COUNT) {
        fprintf(stderr, "Could not read the process audit token.\n");
        return NO;
    }

    NSDictionary *session = CFBridgingRelease(CGSessionCopyCurrentDictionary());
    NSMutableDictionary *report = [@{
        @"uid": @(getuid()),
        @"audit_session_id": @(audit_token_to_asid(token)),
        @"has_gui_session": session != nil ? @YES : @NO,
        @"gui_uid": session[(__bridge NSString *)kCGSessionUserIDKey] ?: NSNull.null,
        @"on_console": session[(__bridge NSString *)kCGSessionOnConsoleKey] ?: NSNull.null,
        @"login_complete": session[(__bridge NSString *)kCGSessionLoginDoneKey] ?: NSNull.null
    } mutableCopy];
    if (run) {
        [report addEntriesFromDictionary:run];
        uid_t consoleUID = 0;
        NSString *consoleUser = CFBridgingRelease(SCDynamicStoreCopyConsoleUser(NULL, &consoleUID, NULL));
        report[@"console_uid"] = consoleUser ? @(consoleUID) : NSNull.null;
        report[@"uptime_ms"] = @(clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW) / 1000000);
    }
    report[@"screen_capture_preflight"] = @(CGPreflightScreenCaptureAccess());
    report[@"accessibility_trusted"] = AXIsProcessTrusted() ? @YES : @NO;

    NSError *error = nil;
    NSData *json = [NSJSONSerialization dataWithJSONObject:report options:NSJSONWritingSortedKeys error:&error];
    if (!json) {
        fprintf(stderr, "%s\n", error.localizedDescription.UTF8String);
        return NO;
    }
    return fwrite(json.bytes, 1, json.length, stdout) == json.length
        && putchar('\n') != EOF && fflush(stdout) == 0;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc == 1) return writeSample(nil) ? 0 : 1;
        if (argc != 3 || strcmp(argv[1], "--m0-agent") != 0 || getppid() != 1) {
            fprintf(stderr, "Use session-probe for one sample or m0 start RUN for a LaunchAgent.\n");
            return 2;
        }
        NSString *boot = kernelString("kern.bootsessionuuid");
        NSString *build = kernelString("kern.osversion");
        if (!boot || !build) return 1;
        NSDictionary *run = @{
            @"source": @"m0-launchagent", @"run_id": @(argv[2]),
            @"boot_id": boot, @"os_build": build
        };
        for (int i = 0; i < 300; ++i) {
            @autoreleasepool {
                if (!writeSample(run)) return 1;
            }
            if (i < 299) sleep(2);
        }
        return 0;
    }
}
