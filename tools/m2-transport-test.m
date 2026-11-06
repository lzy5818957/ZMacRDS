// Synthetic transport integration test. No screen capture or input injection.
#include "m1-common.h"
#import <CoreVideo/CoreVideo.h>
#include "m1-capture.h"
static NSDictionary *transportIdentity(void) {
    return @{@"uid":@(getuid()),@"gui_uid":@(getuid()),@"audit_session_id":@123,@"login_complete":@YES};
}
#define identity transportIdentity
#include "m2-bridge.h"
#undef identity

static void require(BOOL ok,const char *message) {
    if(!ok){fprintf(stderr,"transport test: %s\n",message);exit(1);}
}
static BOOL readAll(int fd,void *data,size_t length) {
    uint8_t *p=data;
    while(length){ssize_t n=recv(fd,p,length,0);if(n<=0)return NO;p+=n;length-=(size_t)n;}
    return YES;
}
static NSData *packet(int fd,uint8_t *kind) {
    uint32_t length;if(!readAll(fd,&length,4))return nil;length=ntohl(length);
    require(length>0 && length<=36*1024*1024,"bounded packet length");
    require(readAll(fd,kind,1),"packet kind");
    NSMutableData *body=[NSMutableData dataWithLength:length-1];
    require(readAll(fd,body.mutableBytes,body.length),"complete packet body");return body;
}
static void expectClosed(int fd) {
    // Revocation is immediate, including midway through an in-flight frame.
    // Only positive traffic requires complete packet framing.
    uint8_t buffer[65536];
    for(;;){ssize_t n=recv(fd,buffer,sizeof(buffer),0);if(n==0)return;if(n<0 && errno==EINTR)continue;require(n>0,"revoked connection must close");}
}
static void ping(int fd,NSDictionary *session,uint32_t lease,int seq) {
    NSMutableDictionary *m=[session mutableCopy];
    [m addEntriesFromDictionary:@{@"lease":@(lease),@"seq":@(seq),@"type":@"ping",@"audio":@(seq==30)}];
    NSData *json=[NSJSONSerialization dataWithJSONObject:m options:0 error:NULL];
    uint32_t length=htonl((uint32_t)json.length+1);NSMutableData *p=[NSMutableData dataWithBytes:&length length:4];
    uint8_t kind=4;[p appendBytes:&kind length:1];[p appendData:json];
    require(send(fd,p.bytes,p.length,0)==(ssize_t)p.length,"complete ping");
}
int main(void) {
    @autoreleasepool {
        umask(077);
        require(captureTimeFresh(10000000000ULL,10001000000ULL,1,1),"fresh capture timestamp");
        require(!captureTimeFresh(10000000000ULL,14000000000ULL,1,1),"pre-stall queued capture rejected");
        require(!captureTimeFresh(15000000000ULL,10000000000ULL,1,1),"impossible future capture rejected");
        require(!captureTimeFresh(0,10000000000ULL,1,1),"missing capture timestamp rejected");
        require(captureIdleTimeFresh(16.8),"fresh unchanged SCK sample is live");
        require(!captureIdleTimeFresh(3001),"pre-stall idle sample cannot resume input");
        require(!captureIdleTimeFresh(-101),"impossible future idle sample rejected");
        require(!captureIdleTimeFresh(NAN) && !captureIdleTimeFresh(INFINITY),"missing idle time rejected");
        NSMutableDictionary *session=[transportIdentity() mutableCopy];
        [session addEntriesFromDictionary:@{@"nonce":@12345,@"epoch":@1,@"boot_id":kernelString("kern.bootsessionuuid")}];
        __block NSUInteger discarded=0;__block BOOL authorized=YES;
        M2Bridge *bridge=[[M2Bridge alloc] initWithSession:session ready:^BOOL{return authorized;} log:^(NSString *k,NSDictionary *v){(void)v;if([k isEqual:@"bridge_input_discarded"])discarded++;}];
        require(bridge!=nil,"private listener");bridge.width=1920;bridge.height=1200;bridge.bounds=CGRectMake(0,0,1920,1200);
        CVPixelBufferRef buffer=NULL;
        require(CVPixelBufferCreate(NULL,1920,1200,kCVPixelFormatType_32BGRA,NULL,&buffer)==kCVReturnSuccess,"synthetic pixel allocation");
        require(CVPixelBufferLockBaseAddress(buffer,0)==kCVReturnSuccess,"pixel lock");
        memset(CVPixelBufferGetBaseAddress(buffer),0x5a,CVPixelBufferGetBytesPerRow(buffer)*1200);
        CVPixelBufferUnlockBaseAddress(buffer,0);
        CVPixelBufferRef owned=copyCapturePixels(buffer);require(owned!=NULL && owned!=buffer,"independent capture allocation");
        CVPixelBufferLockBaseAddress(buffer,0);memset(CVPixelBufferGetBaseAddress(buffer),0xa5,CVPixelBufferGetBytesPerRow(buffer)*1200);CVPixelBufferUnlockBaseAddress(buffer,0);
        CVPixelBufferLockBaseAddress(owned,kCVPixelBufferLock_ReadOnly);
        require(((uint8_t *)CVPixelBufferGetBaseAddress(owned))[0]==0x5a &&
            ((uint8_t *)CVPixelBufferGetBaseAddress(owned))[1199*CVPixelBufferGetBytesPerRow(owned)+1919*4]==0x5a,"producer reuse cannot mutate cached first/last rows");
        CVPixelBufferUnlockBaseAddress(owned,kCVPixelBufferLock_ReadOnly);CVPixelBufferRelease(owned);
        CVPixelBufferLockBaseAddress(buffer,0);memset(CVPixelBufferGetBaseAddress(buffer),0x5a,CVPixelBufferGetBytesPerRow(buffer)*1200);CVPixelBufferUnlockBaseAddress(buffer,0);
        [bridge publish:buffer]; // Must be available even before a viewer connects.
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW,15*NSEC_PER_SEC),dispatch_get_main_queue(),^{require(NO,"test deadline");});
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
            @autoreleasepool {
                int fd=socket(AF_UNIX,SOCK_STREAM,0);struct sockaddr_un address={0};address.sun_family=AF_UNIX;
                strlcpy(address.sun_path,bridge.socketPath.fileSystemRepresentation,sizeof(address.sun_path));
                struct timeval deadline={5,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&deadline,sizeof(deadline));
                require(connect(fd,(struct sockaddr *)&address,sizeof(address))==0,"same-UID connection");
                uint8_t kind=0;NSData *hello=packet(fd,&kind);require(kind==1,"bridge greeting");
                NSDictionary *greeting=[NSJSONSerialization JSONObjectWithData:hello options:0 error:NULL];
                uint32_t lease=[greeting[@"lease"] unsignedIntValue];require(lease!=0,"new lease");
                dispatch_sync(dispatch_get_main_queue(),^{[bridge publishAudio:[NSData dataWithBytes:"abcd" length:4] captured:nowMS()];require(bridge.audioQueue.count==0,"no audio before explicit client request");});
                uint64_t start=nowMS();int frames=0;
                while(frames<30) {
                    NSData *body=packet(fd,&kind);require(body!=nil,"live transport");if(kind!=2)continue;
                    require(body.length==12+1920*1200*4,"complete frame geometry");
                    const uint8_t *p=body.bytes;require(p[12]==0x5a && p[body.length-1]==0x5a,"first and last pixels");
                    frames++;ping(fd,session,lease,frames);
                    if(frames<30)dispatch_async(dispatch_get_main_queue(),^{[bridge publish:buffer];});
                }
                uint64_t elapsed=nowMS()-start;
                dispatch_sync(dispatch_get_main_queue(),^{[bridge poll:nil];require(bridge.clientAudio,"negotiated audio enabled by authenticated ping");[bridge publishAudio:[NSData dataWithBytes:"abcd" length:4] captured:nowMS()];});
                NSData *sound;do{sound=packet(fd,&kind);require(sound!=nil,"audio on existing authenticated transport");}while(kind!=7);
                require(sound.length==24 && memcmp((const uint8_t *)sound.bytes+20,"abcd",4)==0,"bounded PCM packet retains complete data");
                dispatch_sync(dispatch_get_main_queue(),^{bridge.lastCaptureSignal=nowMS()-4000;[bridge pauseCapture];});
                NSData *paused;
                do {paused=packet(fd,&kind);require(paused!=nil,"paused transport remains live");} while(kind!=2);
                require(paused.length==12+1920*1200*4 && ((const uint8_t *)paused.bytes)[12]!=0x5a,"pause frame replaces private desktop pixels");
                NSMutableDictionary *key=[session mutableCopy];
                [key addEntriesFromDictionary:@{@"lease":@(lease),@"seq":@31,@"type":@"key",@"code":@0,@"down":@YES}];
                NSData *json=[NSJSONSerialization dataWithJSONObject:key options:0 error:NULL];
                uint32_t size=htonl((uint32_t)json.length+1);uint8_t inputKind=4;
                NSMutableData *message=[NSMutableData dataWithBytes:&size length:4];[message appendBytes:&inputKind length:1];[message appendData:json];
                require(send(fd,message.bytes,message.length,0)==(ssize_t)message.length,"key during suspension");
                ping(fd,session,lease,32);
                do {require(packet(fd,&kind)!=nil,"suspended key must not disconnect");} while(kind!=3);
                dispatch_sync(dispatch_get_main_queue(),^{
                    require(discarded==1 && bridge.keys.count==0 && bridge.sequence==32,"suspended key discarded, not queued");
                    CVPixelBufferLockBaseAddress(buffer,0);memset(CVPixelBufferGetBaseAddress(buffer),0xa5,CVPixelBufferGetBytesPerRow(buffer)*1200);CVPixelBufferUnlockBaseAddress(buffer,0);
                    [bridge publish:buffer];
                });
                NSData *resumed;
                do {resumed=packet(fd,&kind);require(resumed!=nil,"resume on existing transport");} while(kind!=2);
                require(resumed.length==12+1920*1200*4 && ((const uint8_t *)resumed.bytes)[12]==0xa5,"fresh full baseline after suspension");
                key[@"seq"]=@33;key[@"capture_generation"]=@0;
                json=[NSJSONSerialization dataWithJSONObject:key options:0 error:NULL];size=htonl((uint32_t)json.length+1);
                message=[NSMutableData dataWithBytes:&size length:4];[message appendBytes:&inputKind length:1];[message appendData:json];
                require(send(fd,message.bytes,message.length,0)==(ssize_t)message.length,"old-generation queued key after recovery");ping(fd,session,lease,34);
                do{require(packet(fd,&kind)!=nil,"stale queued key must not disconnect");}while(kind!=3);
                dispatch_sync(dispatch_get_main_queue(),^{require(discarded==2 && bridge.keys.count==0,"pre-stall input never replays after recovery");});
                ping(fd,session,lease,34); // Replay still ends this controller.
                expectClosed(fd);
                close(fd);
                fd=socket(AF_UNIX,SOCK_STREAM,0);setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&deadline,sizeof(deadline));
                require(connect(fd,(struct sockaddr *)&address,sizeof(address))==0,"transport after replay rejection");
                hello=packet(fd,&kind);require(kind==1,"new controller greeting");greeting=[NSJSONSerialization JSONObjectWithData:hello options:0 error:NULL];lease=[greeting[@"lease"] unsignedIntValue];ping(fd,session,lease,1);
                do{require(packet(fd,&kind)!=nil,"new controller heartbeat");}while(kind!=3);
                dispatch_sync(dispatch_get_main_queue(),^{
                    NSMutableDictionary *refresh=[session mutableCopy];
                    [refresh addEntriesFromDictionary:@{@"lease":@(lease),@"seq":@2,@"type":@"ping",@"resync":@1}];
                    require([bridge input:refresh],"authenticated fresh resynchronization request");[bridge poll:nil];
                });
                NSDictionary *ack;
                do {NSData *body=packet(fd,&kind);require(body!=nil,"resynchronization acknowledgement");if(kind==6)ack=[NSJSONSerialization JSONObjectWithData:body options:0 error:NULL];}while(!ack || ![ack[@"resync"] isEqual:@1]);
                require(![ack[@"paused"] boolValue],"fresh source can acknowledge live state");
                do{resumed=packet(fd,&kind);require(resumed!=nil,"full baseline after explicit resync");}while(kind!=2);
                require(((const uint8_t *)resumed.bytes)[12]==0xa5,"resynchronization retained the current source pixels");
                dispatch_sync(dispatch_get_main_queue(),^{
                    [bridge pauseCapture];NSMutableDictionary *refresh=[session mutableCopy];
                    [refresh addEntriesFromDictionary:@{@"lease":@(lease),@"seq":@3,@"type":@"ping",@"resync":@2}];
                    require([bridge input:refresh] && bridge.capturePaused,"resync cannot authorize a suspended source");[bridge poll:nil];
                });
                ack=nil;
                do {NSData *body=packet(fd,&kind);require(body!=nil,"suspended resync acknowledgement");if(kind==6)ack=[NSJSONSerialization JSONObjectWithData:body options:0 error:NULL];}while(!ack || ![ack[@"resync"] isEqual:@2]);
                require([ack[@"paused"] boolValue],"suspended source stays paused in its acknowledgement");
                dispatch_sync(dispatch_get_main_queue(),^{
                    bridge.lastPeer=nowMS()-4000;[bridge poll:nil];require(bridge.client>=0,"4s GUI pause must retain lease");
                    bridge.lastPeer=nowMS()-31000;[bridge poll:nil];require(bridge.client<0,"31s peer silence must expire lease");
                });
                expectClosed(fd);close(fd);
                fd=socket(AF_UNIX,SOCK_STREAM,0);setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&deadline,sizeof(deadline));
                require(connect(fd,(struct sockaddr *)&address,sizeof(address))==0,"new controller for revocation test");
                require(packet(fd,&kind)!=nil && kind==1,"revocation greeting");
                dispatch_sync(dispatch_get_main_queue(),^{authorized=NO;[bridge poll:nil];require(bridge.client<0,"permission revocation must remain immediate");});
                expectClosed(fd);close(fd);
                dispatch_async(dispatch_get_main_queue(),^{
                    [bridge stop];CVPixelBufferRelease(buffer);
                    printf("{\"event\":\"transport_test\",\"result\":\"PASS\",\"frames\":30,\"frame_bytes\":9216000,\"elapsed_ms\":%llu,\"cached_baseline\":true,\"suspended_input_discarded\":true,\"same_lease_resumed\":true,\"stale_generation_discarded\":true,\"bounded_lease_grace\":true,\"permission_revocation_immediate\":true,\"replay_rejected\":true,\"audio_opt_in\":true,\"screen_capture\":false,\"input_injection\":false}\n",(unsigned long long)elapsed);
                    exit(0);
                });
            }
        });
        dispatch_main();
    }
}
