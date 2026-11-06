// An administrator-authorized disconnect is scoped to one live Aqua identity.
#import "service-protocol.h"
#include <fcntl.h>

static inline NSDictionary *desktopControlCommand(NSDictionary *session,uint32_t nonce,NSString *lastID) {
    struct stat st;const char *directory=ZR_COMMAND_DIR;
    if(lstat(directory,&st)!=0 || !S_ISDIR(st.st_mode) || st.st_uid!=0 || (st.st_mode&022))return nil;
    NSString *path=[@ZR_COMMAND_DIR stringByAppendingPathComponent:[NSString stringWithFormat:@"%u.json",getuid()]];
    int fd=open(path.fileSystemRepresentation,O_RDONLY|O_NOFOLLOW|O_NONBLOCK);
    if(fd<0)return nil;
    NSData *data;
    if(fstat(fd,&st)==0 && S_ISREG(st.st_mode) && st.st_uid==0 && !(st.st_mode&022) && st.st_size>0 && st.st_size<=4096) {
        uint8_t bytes[4097];ssize_t n=read(fd,bytes,sizeof(bytes));
        if(n>0 && n<=4096)data=[NSData dataWithBytes:bytes length:(NSUInteger)n];
    }
    close(fd);
    id value=data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL] : nil;
    if(![value isKindOfClass:NSDictionary.class])return nil;
    NSDictionary *command=value;
    if(![command[@"schema"] isEqual:@1] || ![command[@"action"] isEqual:@"disconnect"] ||
       ![command[@"uid"] isEqual:@(getuid())] || ![command[@"nonce"] isEqual:@(nonce)] ||
       ![command[@"audit_session_id"] isEqual:session[@"audit_session_id"]] ||
       ![command[@"boot_id"] isEqual:kernelString("kern.bootsessionuuid")])return nil;
    id key=command[@"id"],issued=command[@"issued_ms"];
    if(![key isKindOfClass:NSString.class] || [key length]!=36 || ![[NSUUID alloc] initWithUUIDString:key] || [key isEqual:lastID] ||
       ![issued isKindOfClass:NSNumber.class])return nil;
    uint64_t at=[issued unsignedLongLongValue],now=nowMS();
    if(at>now || now-at>15000)return nil;
    return command;
}
