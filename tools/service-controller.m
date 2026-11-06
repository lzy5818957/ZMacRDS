#import "service-protocol.h"
#import <Security/Authorization.h>
#import <Security/AuthorizationDB.h>
#import <OpenDirectory/OpenDirectory.h>
#include <ifaddrs.h>
#include <arpa/inet.h>
#include <launch.h>
#include <pwd.h>
#include <grp.h>
#include <membership.h>
#include <libproc.h>
#include <sys/proc_info.h>
#include <sys/sysctl.h>
#include <sys/acl.h>
#include <fcntl.h>
#include <signal.h>
#include <time.h>

static NSString *const Config=@"/Library/Application Support/ZMacRDS/server.json";
static NSString *const Agent=@"/Applications/ZMacRDS Desktop.app/Contents/MacOS/M1Agent";
static NSLock *ActionLock;
static BOOL Installing;
static NSLock *AuthenticationLock;
static NSMutableDictionary *AuthenticationFailures;
static NSMutableDictionary *LoginMessages;
static NSLock *DesktopOpeningLock;

static uint64_t uptimeMS(void) {
    struct timespec ts;clock_gettime(CLOCK_MONOTONIC_RAW,&ts);
    return (uint64_t)ts.tv_sec*1000+(uint64_t)ts.tv_nsec/1000000;
}
static NSDictionary *failure(NSString *code,NSString *message) {
    return @{@"ok":@NO,@"error":code,@"message":message};
}
static BOOL integer(id value,uid_t *out) {
    if(![value isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)value)==CFBooleanGetTypeID())return NO;
    double d=[value doubleValue];if(d<501 || d>INT_MAX || d!=(double)[value unsignedIntValue])return NO;
    *out=[value unsignedIntValue];return YES;
}
static NSData *privateFile(NSString *path,uid_t owner,NSUInteger maximum,BOOL private) {
    int fd=open(path.fileSystemRepresentation,O_RDONLY|O_NOFOLLOW|O_NONBLOCK);
    if(fd<0)return nil;
    struct stat s;NSData *result;
    if(fstat(fd,&s)==0 && S_ISREG(s.st_mode) && s.st_uid==owner && s.st_size>=0 && (uint64_t)s.st_size<=maximum && !(s.st_mode&(private ? 077 : 022))) {
        NSMutableData *data=[NSMutableData new];uint8_t buffer[4096];ssize_t n;
        while((n=read(fd,buffer,sizeof(buffer)))>0 && data.length+(NSUInteger)n<=maximum)[data appendBytes:buffer length:(NSUInteger)n];
        if(n==0)result=data;
    }
    close(fd);return result;
}
static NSDictionary *jsonFile(NSString *path,uid_t owner,BOOL private) {
    NSData *data=privateFile(path,owner,65536,private);
    id object=data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL] : nil;
    return [object isKindOfClass:NSDictionary.class] ? object : nil;
}
static NSString *bootID(void) {
    char value[128]={0};size_t length=sizeof(value);
    if(sysctlbyname("kern.bootsessionuuid",value,&length,NULL,0)!=0)return @"";
    return @(value);
}
// Use the admin group's UUID rather than treating a request's username as identity.
static BOOL adminPeer(uid_t uid) {
    if(uid==0)return YES;
    uuid_t user,group;int member=0;
    return mbr_uid_to_uuid(uid,user)==0 && mbr_gid_to_uuid(80,group)==0 && mbr_check_membership(user,group,&member)==0 && member;
}
static BOOL authorized(uid_t peer,NSDictionary *request) {
    if(peer==0)return YES;
    id raw=request[@"authorization"];
    if(![raw isKindOfClass:NSString.class] || [raw length]!=44)return NO;
    NSData *data=[[NSData alloc] initWithBase64EncodedString:raw options:0];
    if(data.length!=sizeof(AuthorizationExternalForm))return NO;
    AuthorizationExternalForm form;memcpy(&form,data.bytes,sizeof(form));
    AuthorizationRef auth=NULL;
    OSStatus status=AuthorizationCreateFromExternalForm(&form,&auth);volatile uint8_t *wipe=(volatile uint8_t *)&form;for(size_t i=0;i<sizeof(form);i++)wipe[i]=0;
    if(status!=errAuthorizationSuccess)return NO;
    AuthorizationItem item={ZR_CONTROL_RIGHT,0,NULL,0};AuthorizationRights rights={1,&item};
    // The daemon never displays a dialog. Interaction belongs to the client UI.
    status=AuthorizationCopyRights(auth,&rights,kAuthorizationEmptyEnvironment,kAuthorizationFlagExtendRights,NULL);
    AuthorizationFree(auth,kAuthorizationFlagDefaults);
    return status==errAuthorizationSuccess;
}
static NSDictionary *task(NSArray<NSString *> *args) {
    NSTask *process=[NSTask new];process.executableURL=[NSURL fileURLWithPath:@"/bin/launchctl"];process.arguments=args;
    NSPipe *pipe=[NSPipe pipe];process.standardOutput=pipe;process.standardError=pipe;process.standardInput=[NSFileHandle fileHandleWithNullDevice];
    NSError *error;if(![process launchAndReturnError:&error])return @{@"success":@NO,@"output":@""};
    int fd=pipe.fileHandleForReading.fileDescriptor;
    fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)|O_NONBLOCK);
    NSMutableData *data=[NSMutableData new];uint8_t buffer[4096];uint64_t deadline=uptimeMS()+2000;BOOL bounded=YES;
    while(YES) {
        ssize_t n;while((n=read(fd,buffer,sizeof(buffer)))>0) {
            if(data.length+(NSUInteger)n>131072){bounded=NO;break;}
            [data appendBytes:buffer length:(NSUInteger)n];
        }
        if(!bounded || uptimeMS()>deadline) {
            if(process.running)kill(process.processIdentifier,SIGKILL);
            bounded=NO;break;
        }
        if(!process.running)break;
        usleep(1000);
    }
    [process waitUntilExit];[pipe.fileHandleForReading closeFile];
    return @{@"success":@(bounded && process.terminationStatus==0),@"output":[[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] ?: @""};
}
static NSDictionary *job(NSString *target) {
    NSDictionary *result=task(@[@"print",target]);NSMutableDictionary *out=[@{@"loaded":result[@"success"]} mutableCopy];
    if(![result[@"success"] boolValue])return out;
    for(NSString *line in [result[@"output"] componentsSeparatedByString:@"\n"]) {
        NSString *s=[line stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
        if([s hasPrefix:@"pid = "] && !out[@"pid"])out[@"pid"]=@([[s substringFromIndex:6] intValue]);
        if([s hasPrefix:@"state = "] && !out[@"state"])out[@"state"]=[s substringFromIndex:8];
        if([s hasPrefix:@"last exit code = "] && !out[@"last_exit"])out[@"last_exit"]=[s substringFromIndex:17];
    }
    return out;
}
static NSArray *routes(void) {
    NSDictionary *config=jsonFile(Config,0,YES);id users=config[@"users"];
    if(![users isKindOfClass:NSArray.class] || [users count]<1 || [users count]>128)return nil;
    NSMutableArray *rows=[NSMutableArray new];NSMutableSet *seen=[NSMutableSet new];
    for(id value in users) {
        if(![value isKindOfClass:NSDictionary.class])return nil;
        uid_t uid;if(!integer(value[@"uid"],&uid) || [seen containsObject:@(uid)])return nil;
        struct passwd storage,*account=NULL;char buffer[16384];
        if(getpwuid_r(uid,&storage,buffer,sizeof(buffer),&account)!=0 || !account || !account->pw_dir || !account->pw_name)return nil;
        [seen addObject:@(uid)];
        [rows addObject:@{@"uid":@(uid),@"username":@(account->pw_name),@"home":@(account->pw_dir)}];
    }
    return rows;
}
static NSDictionary *agentState(NSDictionary *route) {
    uid_t uid=[route[@"uid"] unsignedIntValue];
    NSString *path=[route[@"home"] stringByAppendingPathComponent:@"Library/Application Support/ZMacRDS/Service/state.json"];
    NSDictionary *state=jsonFile(path,uid,YES);
    if(!state || ![state[@"uid"] isEqual:@(uid)] || ![state[@"gui_uid"] isEqual:@(uid)] || ![state[@"boot_id"] isEqual:bootID()])return nil;
    // Each desktop owns this file. Treat it as untrusted input even when its
    // path and owner are correct; malformed metadata must not crash root.
    for(NSString *key in @[@"uptime_ms",@"login_complete",@"nonce",@"audit_session_id"])
        if(![state[key] isKindOfClass:NSNumber.class])return nil;
    for(NSString *key in @[@"last_capture_ms",@"capturing",@"capture_paused",@"screen_capture",@"accessibility"])
        if(state[key] && ![state[key] isKindOfClass:NSNumber.class])return nil;
    if([state[@"uptime_ms"] doubleValue]<0 || [state[@"uptime_ms"] doubleValue]>1e15 ||
       ![state[@"nonce"] unsignedIntValue] || [state[@"nonce"] unsignedLongLongValue]>UINT32_MAX ||
       ![state[@"audit_session_id"] unsignedIntValue])return nil;
    id raw=state[@"pid"];if(![raw isKindOfClass:NSNumber.class] || [raw longLongValue]<=1 || [raw longLongValue]>INT_MAX)return nil;
    struct proc_bsdinfo info={0};pid_t pid=[raw intValue];char name[PROC_PIDPATHINFO_MAXSIZE]={0};
    if(proc_pidinfo(pid,PROC_PIDTBSDINFO,0,&info,sizeof(info))!=sizeof(info) || info.pbi_uid!=uid || info.pbi_ruid!=uid || proc_pidpath(pid,name,sizeof(name))<=0 || ![Agent isEqual:@(name)])return nil;
    return state;
}
static NSDictionary *status(uid_t peer,NSDictionary *request,NSArray *configured) {
    BOOL all=adminPeer(peer) || authorized(peer,request);NSMutableArray *users=[NSMutableArray new];
    for(NSDictionary *route in configured) {
        uid_t uid=[route[@"uid"] unsignedIntValue];if(!all && uid!=peer)continue;
        NSMutableDictionary *row=[@{@"uid":@(uid),@"username":route[@"username"],@"job":job([NSString stringWithFormat:@"gui/%u/org.zmacrds.desktop",uid])} mutableCopy];
        NSDictionary *state=agentState(route);uint64_t now=uptimeMS(),at=[state[@"uptime_ms"] unsignedLongLongValue];
        BOOL fresh=state && at<=now && now-at<6000;
        row[@"fresh"]=@(fresh);row[@"session_available"]=@(state && [state[@"login_complete"] boolValue]);
        row[@"logged_in"]=job([NSString stringWithFormat:@"gui/%u",uid])[@"loaded"];
        [AuthenticationLock lock];NSString *loginMessage=LoginMessages[@(uid)];[AuthenticationLock unlock];
        if(loginMessage)row[@"login_error"]=loginMessage;
        if(fresh) {
            // Explicit allowlist: never copy a configuration, document, token or argv.
            for(NSString *key in @[@"pid",@"background",@"screen_capture",@"accessibility",@"capturing",@"rdp_connected",@"capture_paused",@"display_id",@"audit_session_id"])
                if([state[key] isKindOfClass:NSNumber.class])row[key]=state[key];
            id error=state[@"capture_error"];
            if([error isKindOfClass:NSString.class])row[@"capture_error"]=[error substringToIndex:MIN((NSUInteger)512,[error length])];
        }
        [users addObject:row];
    }
    NSDictionary *config=jsonFile(Config,0,YES);NSMutableArray *addresses=NSMutableArray.new;
    struct ifaddrs *interfaces=NULL;
    if(getifaddrs(&interfaces)==0) {
        for(struct ifaddrs *p=interfaces;p;p=p->ifa_next) {
            if(!p->ifa_addr || p->ifa_addr->sa_family!=AF_INET)continue;
            struct sockaddr_in *address=(struct sockaddr_in *)p->ifa_addr;uint32_t ip=ntohl(address->sin_addr.s_addr);
            if((ip>>24)!=10 && (ip>>20)!=0xac1 && (ip>>16)!=0xc0a8 && (ip>>16)!=0xa9fe && (ip&0xffc00000)!=0x64400000)continue;
            char text[INET_ADDRSTRLEN];if(inet_ntop(AF_INET,&address->sin_addr,text,sizeof(text)) && ![addresses containsObject:@(text)])[addresses addObject:@(text)];
        }
        freeifaddrs(interfaces);
    }
    return @{@"ok":@YES,@"schema":@1,@"peer_uid":@(peer),@"all_accounts":@(all),@"broker":job(@"system/org.zmacrds.server"),@"users":users,
        @"local_auth":@([config[@"local_auth"] boolValue]),@"lan_port":config[@"lan_port"] ?: NSNull.null,@"addresses":addresses};
}
static BOOL trustedFile(NSString *path) {return privateFile(path,0,1048576,NO)!=nil;}
static BOOL trustedRuntime(void) {
    for(NSString *path in @[@"/Library/Application Support/ZMacRDS",@"/Library/Application Support/ZMacRDS/Runtime",@"/Library/Application Support/ZMacRDS/Runtime/bin"]) {
        struct stat s;if(lstat(path.fileSystemRepresentation,&s)!=0 || !S_ISDIR(s.st_mode) || s.st_uid!=0 || (s.st_mode&022))return NO;
    }
    int fd=open("/Library/Application Support/ZMacRDS/Runtime/bin/python3.13",O_RDONLY|O_NOFOLLOW|O_NONBLOCK);
    if(fd<0)return NO;struct stat s;BOOL valid=fstat(fd,&s)==0 && S_ISREG(s.st_mode) && s.st_uid==0 && !(s.st_mode&022) && s.st_size>0 && s.st_size<64*1024*1024;close(fd);return valid;
}
static NSDictionary *setupTask(NSString *operation,NSDictionary *value) {
    if(!trustedRuntime() || !trustedFile(@"/usr/local/libexec/zmacrds-configure.py") ||
       !trustedFile(@"/usr/local/libexec/zmacrds-install-service.py"))return failure(@"installation",@"安装组件不完整，请重新安装软件包");
    NSData *input=[NSJSONSerialization dataWithJSONObject:value options:0 error:NULL];
    if(!input || input.length>8192)return failure(@"invalid_request",@"配置请求过长");
    NSTask *process=[NSTask new];process.executableURL=[NSURL fileURLWithPath:@"/Library/Application Support/ZMacRDS/Runtime/bin/python3.13"];
    process.arguments=@[@"-I",@"-B",@"/usr/local/libexec/zmacrds-configure.py",operation];
    process.environment=@{@"PATH":@"/usr/bin:/bin:/usr/sbin:/sbin",@"HOME":@"/var/root"};
    NSPipe *incoming=NSPipe.pipe,*outgoing=NSPipe.pipe;process.standardInput=incoming;process.standardOutput=outgoing;process.standardError=NSFileHandle.fileHandleWithNullDevice;
    NSError *error;if(![process launchAndReturnError:&error])return failure(@"installation",@"配置组件无法启动");
    BOOL sent=write(incoming.fileHandleForWriting.fileDescriptor,input.bytes,input.length)==(ssize_t)input.length;
    [incoming.fileHandleForWriting closeFile];
    int fd=outgoing.fileHandleForReading.fileDescriptor;fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)|O_NONBLOCK);
    NSMutableData *data=NSMutableData.new;uint8_t bytes[4096];uint64_t deadline=uptimeMS()+(([operation hasPrefix:@"configure"] || [operation isEqual:@"sync-local-users"] || [operation isEqual:@"open-desktop"]) ? 90000 : 3000);
    BOOL bounded=sent;
    while(bounded) {
        ssize_t n;while((n=read(fd,bytes,sizeof(bytes)))>0) {
            if(data.length+(NSUInteger)n>131072){bounded=NO;break;}[data appendBytes:bytes length:(NSUInteger)n];
        }
        if(!bounded || uptimeMS()>deadline){bounded=NO;break;}
        if(!process.running)break;usleep(1000);
    }
    if(!bounded && process.running)kill(process.processIdentifier,SIGKILL);
    [process waitUntilExit];[outgoing.fileHandleForReading closeFile];
    id reply=bounded ? [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL] : nil;
    return [reply isKindOfClass:NSDictionary.class] ? reply : failure(@"operation_failed",@"配置组件未能完成操作，请刷新服务状态");
}
static BOOL disconnect(NSDictionary *route) {
    NSDictionary *state=agentState(route);if(!state || ![state[@"management_schema"] isEqual:@1])return NO;
    NSString *directory=@ZR_COMMAND_DIR;struct stat st;
    if(lstat(directory.fileSystemRepresentation,&st)!=0 || !S_ISDIR(st.st_mode) || st.st_uid!=0 || (st.st_mode&022))return NO;
    NSMutableDictionary *command=[@{@"schema":@1,@"action":@"disconnect",@"id":NSUUID.UUID.UUIDString,@"issued_ms":@(uptimeMS())} mutableCopy];
    for(NSString *key in @[@"uid",@"nonce",@"audit_session_id",@"boot_id"]) {if(!state[key])return NO;command[key]=state[key];}
    NSData *data=[NSJSONSerialization dataWithJSONObject:command options:0 error:NULL];
    NSString *path=[directory stringByAppendingPathComponent:[NSString stringWithFormat:@"%@.json",route[@"uid"]]];
    NSString *temporary=[directory stringByAppendingPathComponent:NSUUID.UUID.UUIDString];
    int fd=open(temporary.fileSystemRepresentation,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW,0600);if(fd<0)return NO;
    // Keep the envelope root-owned, but readable only by its target desktop.
    // A shared staff group must not expose other users' session metadata.
    uuid_t target;acl_t acl=acl_init(1);acl_entry_t entry;acl_permset_t permissions;
    BOOL granted=acl && mbr_uid_to_uuid([route[@"uid"] unsignedIntValue],target)==0 &&
        acl_create_entry(&acl,&entry)==0 && acl_set_tag_type(entry,ACL_EXTENDED_ALLOW)==0 &&
        acl_set_qualifier(entry,target)==0 && acl_get_permset(entry,&permissions)==0 &&
        acl_clear_perms(permissions)==0 && acl_add_perm(permissions,ACL_READ_DATA)==0 &&
        acl_add_perm(permissions,ACL_READ_ATTRIBUTES)==0 && acl_add_perm(permissions,ACL_READ_SECURITY)==0 &&
        acl_set_permset(entry,permissions)==0 && acl_set_fd_np(fd,acl,ACL_TYPE_EXTENDED)==0;
    if(acl)acl_free(acl);
    BOOL ok=granted && write(fd,data.bytes,data.length)==(ssize_t)data.length && fsync(fd)==0;close(fd);
    if(ok)ok=rename(temporary.fileSystemRepresentation,path.fileSystemRepresentation)==0;
    unlink(temporary.fileSystemRepresentation);return ok;
}
static NSDictionary *authenticateRDP(uid_t peer,pid_t peerPID,NSDictionary *request) {
    NSDictionary *config=jsonFile(Config,0,YES);uid_t target;
    if(![config[@"local_auth"] isEqual:@YES] || ![config[@"broker_uid"] isEqual:@(peer)] ||
        !integer(request[@"uid"],&target))return failure(@"denied",@"登录未通过");
    struct proc_bsdinfo process={0};char executable[PROC_PIDPATHINFO_MAXSIZE]={0};
    if(peerPID<=1 || proc_pidinfo(peerPID,PROC_PIDTBSDINFO,0,&process,sizeof(process))!=sizeof(process) ||
        process.pbi_uid!=peer || process.pbi_ruid!=peer || proc_pidpath(peerPID,executable,sizeof(executable))<=0 ||
        strcmp(executable,"/usr/local/libexec/zmacrds-server"))return failure(@"denied",@"登录未通过");
    NSString *name=request[@"username"],*password=request[@"password"];
    if(![name isKindOfClass:NSString.class] || ![password isKindOfClass:NSString.class] ||
        name.length<1 || name.length>256 || password.length<1 || [password lengthOfBytesUsingEncoding:NSUTF8StringEncoding]>1024 ||
        [password rangeOfString:[NSString stringWithFormat:@"%C",(unichar)0]].location!=NSNotFound)return failure(@"denied",@"登录未通过");
    struct passwd storage,*account=NULL;char buffer[16384];
    if(getpwuid_r(target,&storage,buffer,sizeof(buffer),&account)!=0 || !account || account->pw_name[0]=='_' ||
        strncmp(account->pw_dir,"/Users/",7) || ![name isEqual:@(account->pw_name)])return failure(@"denied",@"登录未通过");
    BOOL configured=NO;for(NSDictionary *r in routes())if([r[@"uid"] isEqual:@(target)] && [r[@"username"] isEqual:name])configured=YES;
    if(!configured)return failure(@"denied",@"登录未通过");
    [AuthenticationLock lock];uint64_t now=uptimeMS();NSDictionary *limit=AuthenticationFailures[@(target)];
    if(limit && now-[limit[@"at"] unsignedLongLongValue]<60000 && [limit[@"count"] intValue]>=5){[AuthenticationLock unlock];return failure(@"denied",@"登录未通过");}
    ODNode *node=[ODNode nodeWithSession:ODSession.defaultSession name:@"/Local/Default" error:NULL];
    ODRecord *record=[node recordWithRecordType:kODRecordTypeUsers name:name attributes:@[kODAttributeTypeUniqueID] error:NULL];
    NSArray *ids=[record valuesForAttribute:kODAttributeTypeUniqueID error:NULL];
    BOOL valid=ids.count==1 && [ids.firstObject isEqual:[@(target) stringValue]] &&
        [record authenticationAllowedAndReturnError:NULL] && [record verifyPassword:password error:NULL];
    if(valid)[AuthenticationFailures removeObjectForKey:@(target)];
    else AuthenticationFailures[@(target)]=@{@"at":limit && now-[limit[@"at"] unsignedLongLongValue]<60000 ? limit[@"at"] : @(now),
        @"count":@(limit && now-[limit[@"at"] unsignedLongLongValue]<60000 ? [limit[@"count"] intValue]+1 : 1)};
    [AuthenticationLock unlock];
    if(!valid)return failure(@"denied",@"登录未通过");
    [AuthenticationLock lock];[LoginMessages removeObjectForKey:@(target)];[AuthenticationLock unlock];
    NSDictionary *desktopRoute=@{@"uid":@(target),@"home":@(account->pw_dir)};
    NSDictionary *priorState=agentState(desktopRoute);
    NSDictionary *nativeLease=jsonFile([@ZR_COMMAND_DIR stringByAppendingPathComponent:[NSString stringWithFormat:@"native-%u.lease",target]],0,NO);
    BOOL nativeDesktop=nativeLease && [nativeLease[@"uid"] isEqual:@(target)] &&
        [nativeLease[@"boot_id"] isEqual:priorState[@"boot_id"]] &&
        [nativeLease[@"audit_session_id"] isEqual:priorState[@"audit_session_id"]];
    uint64_t signal=[priorState[@"last_capture_ms"] unsignedLongLongValue],nowCapture=uptimeMS();
    BOOL needsDesktop=![job([NSString stringWithFormat:@"gui/%u",target])[@"loaded"] boolValue] ||
        !priorState || ![priorState[@"capturing"] boolValue] || [priorState[@"capture_paused"] boolValue] ||
        !signal || signal>nowCapture || nowCapture-signal>3000 ||
        nativeDesktop;
    if(needsDesktop) {
        if(![DesktopOpeningLock lockBeforeDate:[NSDate dateWithTimeIntervalSinceNow:5]])return failure(@"desktop_busy",@"正在启动其他用户桌面，请稍后重新连接");
        NSDictionary *opened;
        @try {
            opened=setupTask(@"open-desktop",@{@"uid":@(target),@"username":name,@"password":password});
        } @finally {[DesktopOpeningLock unlock];}
        if(![opened[@"ok"] boolValue]) {
            NSString *message=[opened[@"error"] isEqual:@"desktop_capacity"] ? opened[@"message"] : @"请开启系统屏幕共享并允许此用户，或完成该用户的首次系统设置和授权";
            [AuthenticationLock lock];LoginMessages[@(target)]=message;[AuthenticationLock unlock];
            return failure(@"desktop_setup_required",message);
        }
        NSDictionary *state=agentState(desktopRoute);
        uint64_t captured=[state[@"last_capture_ms"] unsignedLongLongValue],now=uptimeMS();
        if(!state || ![state[@"login_complete"] boolValue] || ![state[@"screen_capture"] boolValue] || ![state[@"accessibility"] boolValue] || !captured || captured>now || now-captured>3000)return failure(@"desktop_setup_required",@"桌面或系统授权尚未就绪");
    }
    return @{@"ok":@YES,@"uid":@(target)};
}
static NSDictionary *handle(uid_t peer,pid_t peerPID,NSDictionary *request) {
    NSString *action=request[@"action"];
    BOOL configuring=[action isEqual:@"configure"];
    BOOL network=[action isEqual:@"configure-local"] || [action isEqual:@"configure-network"];
    BOOL authentication=[action isEqual:@"authenticate-rdp"];
    NSSet *keys=[NSSet setWithArray:configuring ? @[@"action",@"broker_uid",@"desktop_uids",@"authorization"] : network ? @[@"action",@"enabled",@"port",@"authorization"] : authentication ? @[@"action",@"uid",@"username",@"password"] : @[@"action",@"uid",@"authorization"]];
    if(!request || request.count>keys.count)return failure(@"invalid_request",@"请求格式无效");
    for(id key in request)if(![keys containsObject:key])return failure(@"invalid_request",@"请求包含不支持的字段");
    NSSet *actions=[NSSet setWithArray:@[@"status",@"setup-info",@"configure",@"configure-local",@"configure-network",@"authenticate-rdp",@"connection-info",@"prepare-upgrade",@"start-server",@"stop-server",@"restart-server",@"start-agent",@"stop-agent",@"restart-agent",@"disconnect-rdp"]];
    if(![action isKindOfClass:NSString.class] || ![actions containsObject:action])return failure(@"invalid_action",@"不支持的操作");
    if(authentication)return authenticateRDP(peer,peerPID,request);
    if(network) {
        if(!authorized(peer,request))return failure(@"authorization_required",@"启动服务及修改连接设置需要管理员验证");
        id port=request[@"port"],enabled=request[@"enabled"];
        if(![port isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)port)==CFBooleanGetTypeID() ||
            [port doubleValue]!=[port intValue] || [port intValue]<1024 || [port intValue]>65535 || [port intValue]==3390 ||
            ![enabled isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)enabled)!=CFBooleanGetTypeID())return failure(@"invalid_request",@"连接端口必须为 1024–65535，且不能使用保留端口 3390");
        [ActionLock lock];NSDictionary *reply=Installing ? failure(@"installing",@"软件正在安装") : setupTask(action,@{@"enabled":enabled,@"port":port});[ActionLock unlock];return reply;
    }
    if([action isEqual:@"prepare-upgrade"]) {
        if(peer!=0 || request[@"uid"])return failure(@"authorization_required",@"此操作仅供系统安装器使用");
        [ActionLock lock];Installing=YES;[ActionLock unlock];return @{@"ok":@YES};
    }
    if(configuring || [action isEqual:@"setup-info"]) {
        if(!authorized(peer,request))return failure(@"authorization_required",@"配置账号需要系统管理员验证");
        if(!configuring && request[@"uid"])return failure(@"invalid_request",@"账号配置不接受目标账号参数");
        NSDictionary *input=@{};
        if(configuring) {
            uid_t broker;if(!integer(request[@"broker_uid"],&broker) || ![request[@"desktop_uids"] isKindOfClass:NSArray.class] ||
                [request[@"desktop_uids"] count]<1 || [request[@"desktop_uids"] count]>8)return failure(@"invalid_uid",@"请选择有效的本地账号");
            NSMutableSet *seen=NSMutableSet.new;
            for(id raw in request[@"desktop_uids"]) {uid_t value;if(!integer(raw,&value) || value==broker || [seen containsObject:@(value)])return failure(@"invalid_uid",@"桌面账号必须互不重复，且与服务运行账号分开");[seen addObject:@(value)];}
            input=@{@"broker_uid":@(broker),@"desktop_uids":request[@"desktop_uids"]};
        }
        [ActionLock lock];NSDictionary *reply=Installing ? failure(@"installing",@"软件正在安装，请稍后重试") : setupTask(action,input);[ActionLock unlock];return reply;
    }
    NSArray *configured=routes();
    if(!configured && [action isEqual:@"status"] && access(Config.fileSystemRepresentation,F_OK)!=0 && errno==ENOENT) {
        NSMutableDictionary *reply=[status(peer,request,@[]) mutableCopy];reply[@"setup_required"]=@YES;return reply;
    }
    if(!configured)return failure(@"configuration",@"无法读取有效的服务账号配置");
    if([action isEqual:@"status"])return status(peer,request,configured);
    BOOL server=[action hasSuffix:@"-server"];NSDictionary *route;
    if(!server) {
        uid_t uid;if(!integer(request[@"uid"],&uid))return failure(@"invalid_uid",@"请选择已配置的账号");
        for(NSDictionary *r in configured)if([r[@"uid"] unsignedIntValue]==uid){route=r;break;}
        if(!route)return failure(@"unknown_user",@"该账号未加入远程桌面服务");
    } else if(request[@"uid"])return failure(@"invalid_request",@"网络服务操作不接受账号参数");
    if((server || peer!=[route[@"uid"] unsignedIntValue]) && !authorized(peer,request))
        return failure(@"authorization_required",@"此操作需要系统管理员验证");
    if([action isEqual:@"connection-info"])return setupTask(action,@{@"uid":route[@"uid"]});
    [ActionLock lock];BOOL success=NO;
    if(Installing){[ActionLock unlock];return failure(@"installing",@"软件正在安装，请稍后重试");}
    if([action isEqual:@"disconnect-rdp"])success=disconnect(route);
    else {
        NSString *target=server ? @"system/org.zmacrds.server" : [NSString stringWithFormat:@"gui/%@/org.zmacrds.desktop",route[@"uid"]];
        if([action hasPrefix:@"stop-"])success=[task(@[@"bootout",target])[@"success"] boolValue];
        else if([action hasPrefix:@"restart-"])success=[task(@[@"kickstart",@"-k",target])[@"success"] boolValue];
        else if([job(target)[@"loaded"] boolValue])success=[task(@[@"kickstart",target])[@"success"] boolValue];
        else {
            NSString *domain=server ? @"system" : [NSString stringWithFormat:@"gui/%@",route[@"uid"]];
            if(server) {
                NSString *file=@"/Library/LaunchDaemons/org.zmacrds.server.plist";
                success=trustedFile(file) && [task(@[@"bootstrap",domain,file])[@"success"] boolValue];
            } else {
                // launchd accepts the installed user LaunchAgent in its home.
                // Drop to that UID BEFORE launchctl reads this user-controlled
                // file: even a modified plist must never launch code as root.
                NSString *uid=[route[@"uid"] stringValue];
                NSString *file=[route[@"home"] stringByAppendingPathComponent:@"Library/LaunchAgents/org.zmacrds.desktop.plist"];
                success=[task(@[@"asuser",uid,@"/usr/bin/sudo",@"-n",@"-H",@"-u",[@"#" stringByAppendingString:uid],@"/bin/launchctl",@"bootstrap",domain,file])[@"success"] boolValue];
            }
        }
    }
    [ActionLock unlock];
    return success ? @{@"ok":@YES,@"message":@"操作已提交；桌面应用会保留，远程连接可能需要重连"} : failure(@"operation_failed",@"操作未完成，请刷新状态；账号可能尚未登录图形桌面");
}
static int controlDaemon(void) {
    if(geteuid()!=0)return 2;
    int *fds=NULL;size_t count=0;
    if(launch_activate_socket("Control",&fds,&count)!=0 || count!=1){free(fds);return 1;}
    int listener=fds[0];free(fds);fcntl(listener,F_SETFD,FD_CLOEXEC);ActionLock=[NSLock new];AuthenticationLock=NSLock.new;DesktopOpeningLock=NSLock.new;AuthenticationFailures=NSMutableDictionary.new;LoginMessages=NSMutableDictionary.new;
    NSOperationQueue *queue=[NSOperationQueue new];queue.maxConcurrentOperationCount=4;
    dispatch_semaphore_t slots=dispatch_semaphore_create(12);
    dispatch_source_t sync=dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER,0,0,dispatch_get_global_queue(QOS_CLASS_UTILITY,0));
    dispatch_source_set_timer(sync,dispatch_time(DISPATCH_TIME_NOW,30*NSEC_PER_SEC),30*NSEC_PER_SEC,NSEC_PER_SEC);
    dispatch_source_set_event_handler(sync,^{@autoreleasepool {
        if(![ActionLock tryLock])return;
        if(!Installing && [jsonFile(Config,0,YES)[@"local_auth"] boolValue] && [job(@"system/org.zmacrds.server")[@"loaded"] boolValue])setupTask(@"sync-local-users",@{});
        [ActionLock unlock];
    }});dispatch_resume(sync);
    while(YES) {
        int fd=accept(listener,NULL,NULL);if(fd>=0)fcntl(fd,F_SETFD,FD_CLOEXEC);if(fd<0){if(errno==EINTR)continue;return 1;}
        if(dispatch_semaphore_wait(slots,DISPATCH_TIME_NOW)!=0){close(fd);continue;}
        [queue addOperationWithBlock:^{@autoreleasepool {
            int yes=1;setsockopt(fd,SOL_SOCKET,SO_NOSIGPIPE,&yes,sizeof(yes));
            struct timeval timeout={3,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
            uid_t uid;gid_t gid;NSDictionary *reply=failure(@"invalid_request",@"请求格式无效");
            if(getpeereid(fd,&uid,&gid)==0) {
                NSData *data=zrReadMessage(fd,8192,3000);id object=data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL] : nil;
                pid_t peerPID=0;socklen_t size=sizeof(peerPID);getsockopt(fd,SOL_LOCAL,LOCAL_PEERPID,&peerPID,&size);
                if([object isKindOfClass:NSDictionary.class])reply=handle(uid,peerPID,object);
            }
            zrWriteMessage(fd,reply);close(fd);dispatch_semaphore_signal(slots);
        }}];
    }
}
int main(int argc,const char *argv[]) {@autoreleasepool {
    umask(077);signal(SIGPIPE,SIG_IGN);
    if(argc==2 && !strcmp(argv[1],"--daemon"))return controlDaemon();
    if(argc==2 && !strcmp(argv[1],"--install-right")) {
        if(geteuid()!=0)return 2;
        CFDictionaryRef existing=NULL;OSStatus status=AuthorizationRightGet(ZR_CONTROL_RIGHT,&existing);
        NSDictionary *previous=(__bridge NSDictionary *)existing;
        BOOL migrate=[previous[@"identifier"] isEqual:@"org.zmacrds.control"] &&
            [previous[@"rule"] isEqual:@[@kAuthorizationRuleAuthenticateAsAdmin]] && [previous[@"version"] intValue]==0;
        if(existing)CFRelease(existing);
        // Preserve an administrator's custom policy; migrate only our initial
        // zero-timeout alias. Never alter the system authenticate-admin rule.
        if(status==errAuthorizationSuccess && !migrate)return 0;
        AuthorizationRef installer=NULL;
        if(AuthorizationCreate(NULL,NULL,kAuthorizationFlagDefaults,&installer)!=errAuthorizationSuccess)return 1;
        NSDictionary *definition=@{@"class":@"user",@"group":@"admin",@"authenticate-user":@YES,
            @"allow-root":@NO,@"session-owner":@NO,@"shared":@NO,@"timeout":@300,@"tries":@3,@"version":@1};
        status=AuthorizationRightSet(installer,ZR_CONTROL_RIGHT,(__bridge CFDictionaryRef)definition,CFSTR("管理 ZMacRDS 的网络服务和其他账号的代理"),NULL,NULL);
        AuthorizationFree(installer,kAuthorizationFlagDefaults);return status==errAuthorizationSuccess ? 0 : 1;
    }
    if(argc>=3 && argc<=4 && !strcmp(argv[1],"--client")) {
        NSMutableDictionary *request=[@{@"action":@(argv[2])} mutableCopy];
        if(argc==4){char *end;long uid=strtol(argv[3],&end,10);if(*end || uid<501 || uid>INT_MAX)return 2;request[@"uid"]=@(uid);}
        NSDictionary *reply=zrRequest(request);NSData *data=[NSJSONSerialization dataWithJSONObject:reply options:NSJSONWritingPrettyPrinted error:NULL];
        fwrite(data.bytes,1,data.length,stdout);putchar('\n');return [reply[@"ok"] boolValue] ? 0 : 1;
    }
    fprintf(stderr,"Usage: zmacrds-control --client ACTION [UID]\n");return 2;
}}
