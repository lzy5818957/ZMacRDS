// Local-only management protocol. No shell strings or arbitrary paths.
// Connection secrets are returned only for the peer's own account or after
// administrator authorization, never by the ordinary status operation.
#import <Foundation/Foundation.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <unistd.h>
#include <errno.h>
#include <poll.h>
#include <time.h>

#define ZR_CONTROL_SOCKET "/var/run/org.zmacrds.control.sock"
#define ZR_CONTROL_RIGHT "org.zmacrds.manage"
#define ZR_COMMAND_DIR "/var/run/zmacrds-control"
#define ZR_WEB_SOCKET "/var/db/zmacrds-web/runtime/status.sock"
#define ZR_CONTROL_MAX_REQUEST (64 * 1024)
#define ZR_WEB_MAX_SESSIONS 64
#define ZR_WEB_MAX_STATUS (32 * 1024)

// This session-local message only opens native permission UI in the signed
// desktop process. It cannot grant TCC access or operate another user's agent.
#define ZR_PERMISSION_NOTIFICATION @"org.zmacrds.desktop.permission-request"
static inline NSString *zrPermissionObject(void) {
    return [NSString stringWithFormat:@"uid:%u",getuid()];
}

static inline uint64_t zrControlTimeMs(void) {
    struct timespec value;clock_gettime(CLOCK_MONOTONIC,&value);
    return (uint64_t)value.tv_sec*1000+(uint64_t)value.tv_nsec/1000000;
}
static inline NSData *zrReadMessage(int fd, NSUInteger maximum, unsigned milliseconds) {
    NSMutableData *data=[NSMutableData new];uint8_t bytes[4096];
    uint64_t deadline=zrControlTimeMs()+milliseconds;
    while(data.length<maximum) {
        uint64_t now=zrControlTimeMs();if(now>=deadline)return nil;
        struct pollfd ready={fd,POLLIN,0};
        int result=poll(&ready,1,(int)(deadline-now));
        if(result<0 && errno==EINTR)continue;
        if(result<=0 || !(ready.revents&POLLIN))return nil;
        ssize_t n=read(fd,bytes,MIN(sizeof(bytes),maximum-data.length));
        if(n<0 && errno==EINTR)continue;
        if(n<=0)return nil;
        uint8_t *end=memchr(bytes,'\n',(size_t)n);
        [data appendBytes:bytes length:end ? (NSUInteger)(end-bytes) : (NSUInteger)n];
        if(end)return data;
    }
    return nil;
}
static inline BOOL zrWriteMessage(int fd, NSDictionary *value) {
    NSMutableData *data=[[NSJSONSerialization dataWithJSONObject:value options:0 error:NULL] mutableCopy];
    if(!data || data.length>262144)return NO;
    [data appendBytes:"\n" length:1];NSUInteger sent=0;
    while(sent<data.length) {
        ssize_t n=write(fd,(const uint8_t *)data.bytes+sent,data.length-sent);
        if(n<0 && errno==EINTR)continue;
        if(n<=0)return NO;sent+=(NSUInteger)n;
    }
    return YES;
}
static inline NSDictionary *zrRequest(NSDictionary *value) {
    int fd=socket(AF_UNIX,SOCK_STREAM,0);
    if(fd<0)return @{@"ok":@NO,@"error":@"unavailable",@"message":@"无法连接管理服务"};
    int yes=1;setsockopt(fd,SOL_SOCKET,SO_NOSIGPIPE,&yes,sizeof(yes));
    struct timeval timeout={5,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
    struct sockaddr_un address={0};address.sun_family=AF_UNIX;strlcpy(address.sun_path,ZR_CONTROL_SOCKET,sizeof(address.sun_path));
    uid_t uid;gid_t gid;NSDictionary *reply;
    if(connect(fd,(struct sockaddr *)&address,sizeof(address))==0 && getpeereid(fd,&uid,&gid)==0 && uid==0 && zrWriteMessage(fd,value)) {
        unsigned wait=([value[@"action"] hasPrefix:@"configure"] || [value[@"action"] hasSuffix:@"-web"] || [value[@"action"] isEqual:@"prepare-upgrade"]) ? 120000 : 10000;
        NSData *data=zrReadMessage(fd,262144,wait);
        id object=data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL] : nil;
        if([object isKindOfClass:NSDictionary.class])reply=object;
    }
    close(fd);
    return reply ?: @{@"ok":@NO,@"error":@"unavailable",@"message":@"管理服务未响应，请检查安装或服务状态"};
}
