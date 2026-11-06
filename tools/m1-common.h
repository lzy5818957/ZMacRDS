#import <AppKit/AppKit.h>
#import <ApplicationServices/ApplicationServices.h>
#import <SystemConfiguration/SystemConfiguration.h>
#import <Carbon/Carbon.h>
#include <bsm/libbsm.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <unistd.h>
#include <math.h>

static inline uint64_t nowMS(void) { return clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW)/1000000; }
// ScreenCaptureKit's DisplayTime is mach absolute time, not callback delivery
// time. Buffered pre-stall samples must not re-enable remote input on resume.
static inline BOOL captureTimeFresh(uint64_t captured,uint64_t current,uint32_t numer,uint32_t denom) {
    if(!captured || !numer || !denom)return NO;
    long double ticks=current>=captured ? (long double)(current-captured) : -(long double)(captured-current);
    long double ns=ticks*numer/denom;
    return ns>=-100000000.L && ns<=3000000000.L;
}
static inline BOOL captureIdleTimeFresh(double ageMilliseconds) {
    return isfinite(ageMilliseconds) && ageMilliseconds>=-100 && ageMilliseconds<=3000;
}
static inline NSString *kernelString(const char *key) {
    char value[128]={0}; size_t length=sizeof(value);
    return sysctlbyname(key,value,&length,NULL,0)==0 ? @(value) : @"unknown";
}
static inline NSDictionary *identity(void) {
    audit_token_t token={0}; mach_msg_type_number_t count=TASK_AUDIT_TOKEN_COUNT;
    task_info(mach_task_self(),TASK_AUDIT_TOKEN,(task_info_t)&token,&count);
    NSDictionary *session=CFBridgingRelease(CGSessionCopyCurrentDictionary());
    uid_t console=0; NSString *name=CFBridgingRelease(SCDynamicStoreCopyConsoleUser(NULL,&console,NULL));
    return @{@"uid":@(getuid()),@"audit_session_id":@(audit_token_to_asid(token)),
        @"gui_uid":session[(__bridge NSString *)kCGSessionUserIDKey] ?: NSNull.null,
        @"login_complete":session[(__bridge NSString *)kCGSessionLoginDoneKey] ?: @NO,
        @"console_uid":name ? @(console) : NSNull.null};
}
static inline NSString *inputSourceID(void) {
    TISInputSourceRef source=TISCopyCurrentKeyboardInputSource();
    NSString *value=source ? [(__bridge NSString *)TISGetInputSourceProperty(source,kTISPropertyInputSourceID) copy] : @"unknown";
    if(source) CFRelease(source); return value ?: @"unknown";
}
static inline BOOL selectInputSource(NSString *identifier) {
    // Input modes require their parent input method to be enabled first.
    if([identifier hasPrefix:@"com.apple.inputmethod.SCIM."]) {
        NSArray *parents=CFBridgingRelease(TISCreateInputSourceList((__bridge CFDictionaryRef)@{(__bridge NSString *)kTISPropertyInputSourceID:@"com.apple.inputmethod.SCIM"},true));
        for(id parent in parents)TISEnableInputSource((__bridge TISInputSourceRef)parent);
    }
    NSArray *sources=CFBridgingRelease(TISCreateInputSourceList((__bridge CFDictionaryRef)@{(__bridge NSString *)kTISPropertyInputSourceID:identifier},true));
    for(id item in sources) {
        TISInputSourceRef source=(__bridge TISInputSourceRef)item;
        if(TISEnableInputSource(source)==noErr && TISSelectInputSource(source)==noErr) return YES;
    }
    return NO;
}
static inline BOOL validRun(NSString *value) {
    return [value isKindOfClass:NSString.class] && value.length>0 && value.length<=48 && [value rangeOfCharacterFromSet:[NSCharacterSet characterSetWithCharactersInString:@"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"].invertedSet].location==NSNotFound;
}
static inline void writeJSON(NSString *path,NSDictionary *value) {
    NSData *data=[NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingSortedKeys error:NULL];
    [data writeToFile:path options:NSDataWritingAtomic error:NULL];
}
