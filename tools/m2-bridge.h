// Opt-in M2 transport inside the already isolated ordinary GUI agent.
// Private Unix socket; one same-UID peer; no network listener or frame files.
#include <sys/socket.h>
#include <sys/un.h>
#include <fcntl.h>
#include <arpa/inet.h>

@interface M2Bridge : NSObject
@property int listener, client;
@property NSString *socketPath, *socketDirectory;
@property NSDictionary *session;
@property BOOL (^ready)(void);
@property void (^logEvent)(NSString *, NSDictionary *);
@property(strong) dispatch_source_t listenerSource, readSource, writeSource, watchdog;
@property NSMutableData *incoming;
@property NSData *outgoing;
@property NSMutableArray<NSDictionary *> *audioQueue;
@property(strong) id pendingBuffer;
@property(strong) id latestBuffer;
@property NSMutableData *previousPixels;
@property BOOL regions;
@property BOOL capturePaused, clientAudio;
@property uint32_t pauseFrameLease;
@property uint64_t captureGeneration, sentGeneration;
@property NSUInteger offset, width, height;
@property CGRect bounds;
@property uint64_t lastPeer, lastFrame, lastCaptureSignal, lastHeartbeat, sequence, frameSequence;
@property uint32_t resyncID;
@property uint32_t lease;
@property NSMutableIndexSet *keys;
@property unsigned buttons;
@property CGPoint point;
- (instancetype)initWithSession:(NSDictionary *)session ready:(BOOL (^)(void))ready log:(void (^)(NSString *,NSDictionary *))log;
- (void)publish:(CVPixelBufferRef)buffer;
- (void)publishAudio:(NSData *)pcm captured:(uint64_t)captured;
- (void)clearAudio;
- (void)captureIdle;
- (void)pauseCapture;
- (void)releaseInputs;
- (void)armWrite;
- (void)poll:(NSTimer *)timer;
- (void)closePeer;
- (void)stop;
@end

@implementation M2Bridge
- (instancetype)initWithSession:(NSDictionary *)session ready:(BOOL (^)(void))ready log:(void (^)(NSString *,NSDictionary *))log {
    if(!(self=[super init]))return nil;
    _listener=-1;_client=-1;_session=session;_ready=ready;_logEvent=log;_keys=[NSMutableIndexSet new];
    char path[96];snprintf(path,sizeof(path),"/tmp/zmacrds-m2-%u-XXXXXX",getuid());
    if(!mkdtemp(path))return nil;
    _socketDirectory=@(path);_socketPath=[_socketDirectory stringByAppendingPathComponent:@"agent.sock"];
    _listener=socket(AF_UNIX,SOCK_STREAM,0);
    struct sockaddr_un address={0};address.sun_family=AF_UNIX;
    strlcpy(address.sun_path,_socketPath.fileSystemRepresentation,sizeof(address.sun_path));
    if(_listener<0 || bind(_listener,(struct sockaddr *)&address,sizeof(address)) || chmod(address.sun_path,0600) || listen(_listener,1)) { [self stop]; return nil; }
    fcntl(_listener,F_SETFL,O_NONBLOCK);fcntl(_listener,F_SETFD,FD_CLOEXEC);
    __weak M2Bridge *weak=self;
    _listenerSource=dispatch_source_create(DISPATCH_SOURCE_TYPE_READ,(uintptr_t)_listener,0,dispatch_get_main_queue());
    dispatch_source_set_event_handler(_listenerSource,^{[weak poll:nil];});
    dispatch_resume(_listenerSource);
    // The timer checks leases only; socket readiness drives actual I/O.
    _watchdog=dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER,0,0,dispatch_get_main_queue());
    dispatch_source_set_timer(_watchdog,dispatch_time(DISPATCH_TIME_NOW,250*NSEC_PER_MSEC),250*NSEC_PER_MSEC,10*NSEC_PER_MSEC);
    dispatch_source_set_event_handler(_watchdog,^{[weak poll:nil];});
    dispatch_resume(_watchdog);
    return self;
}
- (BOOL)sameSession {
    NSDictionary *current=identity();
    return [current[@"uid"] isEqual:self.session[@"uid"]] && [current[@"gui_uid"] isEqual:self.session[@"uid"]] &&
        [current[@"audit_session_id"] isEqual:self.session[@"audit_session_id"]] && [current[@"login_complete"] boolValue] &&
        [kernelString("kern.bootsessionuuid") isEqual:self.session[@"boot_id"]];
}
- (CGEventFlags)flags {
    CGEventFlags f=0;
    if([self.keys containsIndex:56] || [self.keys containsIndex:60])f|=kCGEventFlagMaskShift;
    if([self.keys containsIndex:59] || [self.keys containsIndex:62])f|=kCGEventFlagMaskControl;
    if([self.keys containsIndex:58] || [self.keys containsIndex:61])f|=kCGEventFlagMaskAlternate;
    if([self.keys containsIndex:55] || [self.keys containsIndex:54])f|=kCGEventFlagMaskCommand;
    return f;
}
- (void)post:(CGEventRef)event {
    if(!event)return;
    CGEventSetFlags(event,[self flags]);
    CGEventSetIntegerValueField(event,kCGEventSourceUserData,(int64_t)(([self.session[@"nonce"] unsignedLongLongValue]<<32)| (self.sequence & 0xffffffff)));
    CGEventPost(kCGSessionEventTap,event);CFRelease(event);
}
- (void)closePeer {
    if(self.readSource){dispatch_source_cancel(self.readSource);self.readSource=nil;}
    if(self.writeSource){dispatch_source_cancel(self.writeSource);self.writeSource=nil;}
    if(self.client>=0) {
        close(self.client);self.client=-1;
        [self releaseInputs];
        self.logEvent(@"bridge_disconnected",@{@"lease":@(self.lease),@"last_sequence":@(self.sequence)});
    }
    [self clearAudio];
    [self.keys removeAllIndexes];self.buttons=0;self.incoming=nil;self.outgoing=nil;self.offset=0;self.lease=0;self.clientAudio=NO;
    self.pendingBuffer=nil;self.previousPixels=nil;self.regions=NO;self.pauseFrameLease=0;self.sentGeneration=0;
}
- (void)releaseInputs {
        if([self sameSession] && AXIsProcessTrusted()) {
            CGEventSourceRef source=CGEventSourceCreate(kCGEventSourceStatePrivate);
            while(self.keys.count){NSUInteger key=self.keys.firstIndex;[self.keys removeIndex:key];[self post:CGEventCreateKeyboardEvent(source,(CGKeyCode)key,false)];}
            CGEventType ups[]={kCGEventLeftMouseUp,kCGEventRightMouseUp,kCGEventOtherMouseUp};
            for(unsigned b=0;b<3;b++)if(self.buttons&(1u<<b))[self post:CGEventCreateMouseEvent(source,ups[b],self.point,(CGMouseButton)b)];
            if(source)CFRelease(source);
        }
    [self.keys removeAllIndexes];self.buttons=0;
}
- (void)stop {
    if(self.listenerSource){dispatch_source_cancel(self.listenerSource);self.listenerSource=nil;}
    if(self.watchdog){dispatch_source_cancel(self.watchdog);self.watchdog=nil;}
    [self closePeer];self.latestBuffer=nil;
    if(self.listener>=0){close(self.listener);self.listener=-1;}
    if(self.socketPath)unlink(self.socketPath.fileSystemRepresentation);
    if(self.socketDirectory)rmdir(self.socketDirectory.fileSystemRepresentation);
}
- (void)queueKind:(uint8_t)kind payload:(NSData *)payload {
    if(self.client<0 || self.outgoing)return;
    uint32_t length=htonl((uint32_t)payload.length+1);
    NSMutableData *packet=[NSMutableData dataWithBytes:&length length:4];
    [packet appendBytes:&kind length:1];[packet appendData:payload];self.outgoing=packet;self.offset=0;[self armWrite];
}
- (void)armWrite {
    if(self.client<0 || !self.outgoing || self.writeSource)return;
    int fd=self.client;uint32_t lease=self.lease;__weak M2Bridge *weak=self;
    self.writeSource=dispatch_source_create(DISPATCH_SOURCE_TYPE_WRITE,(uintptr_t)fd,0,dispatch_get_main_queue());
    dispatch_source_set_event_handler(self.writeSource,^{
        M2Bridge *s=weak;if(s && s.client==fd && s.lease==lease)[s poll:nil];
    });
    dispatch_resume(self.writeSource);
}
- (void)jsonKind:(uint8_t)kind value:(NSDictionary *)value {
    NSData *data=[NSJSONSerialization dataWithJSONObject:value options:0 error:NULL];
    if(data)[self queueKind:kind payload:data];
}
- (BOOL)input:(NSDictionary *)m {
    if(![m isKindOfClass:NSDictionary.class])return NO;
    for(NSString *key in @[@"uid",@"audit_session_id",@"nonce",@"epoch",@"boot_id"])
        if(![m[key] isEqual:self.session[key]])return NO;
    if(![m[@"lease"] isEqual:@(self.lease)] || ![m[@"seq"] isKindOfClass:NSNumber.class] || [m[@"seq"] unsignedLongLongValue]!=self.sequence+1)return NO;
    self.sequence++;NSString *type=m[@"type"];
    if([type isEqual:@"ping"]){
        if(m[@"audio"] && ![m[@"audio"] isKindOfClass:NSNumber.class])return NO;
        if(m[@"audio"])self.clientAudio=[m[@"audio"] boolValue];self.lastPeer=nowMS();if([m[@"regions"] isEqual:@YES])self.regions=YES;
        if(m[@"repaint"]) {
            if(![m[@"repaint"] isEqual:@YES] && ![m[@"repaint"] isEqual:@NO])return NO;
            if([m[@"repaint"] boolValue] && self.ready() && [self sameSession] && !self.capturePaused && self.latestBuffer &&
               self.lastCaptureSignal && nowMS()-self.lastCaptureSignal<=3000) {
                self.previousPixels=nil;self.pendingBuffer=self.latestBuffer;
            }
        }
        id requested=m[@"resync"];
        if(requested) {
            if(![requested isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)requested)==CFBooleanGetTypeID() ||
               [requested unsignedLongLongValue]>UINT32_MAX || [requested doubleValue]!=(double)[requested unsignedIntValue] ||
               [requested unsignedIntValue]<=self.resyncID)return NO;
            self.resyncID=[requested unsignedIntValue];self.captureGeneration++;
            [self releaseInputs];[self clearAudio];self.previousPixels=nil;
            BOOL live=self.ready() && [self sameSession] && !self.capturePaused && self.latestBuffer &&
                self.lastCaptureSignal && nowMS()-self.lastCaptureSignal<=3000;
            if(live)self.pendingBuffer=self.latestBuffer;
            else [self pauseCapture];
            self.logEvent(@"bridge_resync",@{@"id":@(self.resyncID),@"live":@(live)});
        }
        return YES;
    }
    if(!self.ready() || ![self sameSession])return NO;
    // A well-formed event while capture is suspended is discarded, never
    // replayed after unlock. Protocol/identity errors still revoke the lease.
    BOOL validShape=NO;
    BOOL (^number)(NSString *)=^BOOL(NSString *k){return [m[k] isKindOfClass:NSNumber.class];};
    if([type isEqual:@"key"])validShape=number(@"code") && number(@"down") && [m[@"code"] intValue]>=0 && [m[@"code"] intValue]<=127;
    else if([type isEqual:@"text"])validShape=[m[@"text"] isKindOfClass:NSString.class] && [m[@"text"] length]>0 && [m[@"text"] length]<=2;
    else if([type isEqual:@"move"])validShape=number(@"x") && number(@"y") && [m[@"x"] intValue]>=0 && [m[@"y"] intValue]>=0 && [m[@"x"] unsignedIntegerValue]<self.width && [m[@"y"] unsignedIntegerValue]<self.height;
    else if([type isEqual:@"button"])validShape=number(@"button") && number(@"down") && [m[@"button"] intValue]>=0 && [m[@"button"] intValue]<=2;
    else if([type isEqual:@"scroll"])validShape=number(@"x") && number(@"y") && [m[@"x"] longLongValue]>=-1200 && [m[@"x"] longLongValue]<=1200 && [m[@"y"] longLongValue]>=-1200 && [m[@"y"] longLongValue]<=1200;
    if(!validShape)return NO;
    if(m[@"capture_generation"] && ![m[@"capture_generation"] isKindOfClass:NSNumber.class])return NO;
    BOOL staleGeneration=[m[@"capture_generation"] unsignedLongLongValue]!=self.captureGeneration;
    if(self.capturePaused || !self.lastCaptureSignal || nowMS()-self.lastCaptureSignal>3000 || staleGeneration) {
        if(!staleGeneration || !self.lastCaptureSignal || nowMS()-self.lastCaptureSignal>3000)
        [self pauseCapture];
        self.logEvent(@"bridge_input_discarded",@{@"reason":staleGeneration ? @"capture_generation_changed" : @"capture_suspended",@"type":type,@"sequence":@(self.sequence)});
        return YES;
    }
    CGEventSourceRef source=CGEventSourceCreate(kCGEventSourceStatePrivate);
    if(!source)return NO;
    BOOL valid=YES;
    if([type isEqual:@"key"]) {
        NSNumber *code=m[@"code"],*down=m[@"down"];
        if(![code isKindOfClass:NSNumber.class] || code.intValue<0 || code.intValue>127 || ![down isKindOfClass:NSNumber.class])valid=NO;
        else {if(down.boolValue)[self.keys addIndex:code.unsignedIntegerValue];else [self.keys removeIndex:code.unsignedIntegerValue];[self post:CGEventCreateKeyboardEvent(source,code.unsignedShortValue,down.boolValue)];}
    } else if([type isEqual:@"text"]) {
        NSString *text=m[@"text"];
        if(![text isKindOfClass:NSString.class] || text.length<1 || text.length>2)valid=NO;
        else {UniChar chars[2];[text getCharacters:chars range:NSMakeRange(0,text.length)];for(int down=1;down>=0;down--){CGEventRef event=CGEventCreateKeyboardEvent(source,0,down);if(event)CGEventKeyboardSetUnicodeString(event,text.length,chars);[self post:event];}}
    } else if([type isEqual:@"move"]) {
        NSNumber *x=m[@"x"],*y=m[@"y"];
        if(![x isKindOfClass:NSNumber.class] || ![y isKindOfClass:NSNumber.class] || x.intValue<0 || y.intValue<0 || x.unsignedIntegerValue>=self.width || y.unsignedIntegerValue>=self.height)valid=NO;
        else {self.point=CGPointMake(self.bounds.origin.x+x.doubleValue*self.bounds.size.width/self.width,self.bounds.origin.y+y.doubleValue*self.bounds.size.height/self.height);
            CGEventType t=self.buttons&1 ? kCGEventLeftMouseDragged : self.buttons&2 ? kCGEventRightMouseDragged : self.buttons&4 ? kCGEventOtherMouseDragged : kCGEventMouseMoved;
            [self post:CGEventCreateMouseEvent(source,t,self.point,self.buttons&2 ? kCGMouseButtonRight : self.buttons&4 ? kCGMouseButtonCenter : kCGMouseButtonLeft)];}
    } else if([type isEqual:@"button"]) {
        NSNumber *button=m[@"button"],*down=m[@"down"];
        if(![button isKindOfClass:NSNumber.class] || button.intValue<0 || button.intValue>2 || ![down isKindOfClass:NSNumber.class])valid=NO;
        else {unsigned b=button.unsignedIntValue;CGEventType downs[]={kCGEventLeftMouseDown,kCGEventRightMouseDown,kCGEventOtherMouseDown},ups[]={kCGEventLeftMouseUp,kCGEventRightMouseUp,kCGEventOtherMouseUp};
            if(down.boolValue)self.buttons|=1u<<b;else self.buttons&=~(1u<<b);
            [self post:CGEventCreateMouseEvent(source,down.boolValue ? downs[b] : ups[b],self.point,(CGMouseButton)b)];}
    } else if([type isEqual:@"scroll"]) {
        NSNumber *x=m[@"x"],*y=m[@"y"];
        if(![x isKindOfClass:NSNumber.class] || ![y isKindOfClass:NSNumber.class] || abs(x.intValue)>1200 || abs(y.intValue)>1200)valid=NO;
        else [self post:CGEventCreateScrollWheelEvent(source,kCGScrollEventUnitPixel,2,y.intValue,x.intValue)];
    } else valid=NO;
    CFRelease(source);
    if(valid)self.logEvent(@"bridge_input",@{@"type":type,@"sequence":@(self.sequence),@"lease":@(self.lease)});
    return valid;
}
- (void)poll:(NSTimer *)timer {
    (void)timer;
    int peer=accept(self.listener,NULL,NULL);
    if(peer>=0) {
        uid_t uid=(uid_t)-1;gid_t gid=0;
        if(self.client>=0 || getpeereid(peer,&uid,&gid)!=0 || uid!=getuid() || !self.ready() || !self.width)close(peer);
        else {
            self.client=peer;fcntl(peer,F_SETFL,O_NONBLOCK);fcntl(peer,F_SETFD,FD_CLOEXEC);int one=1;setsockopt(peer,SOL_SOCKET,SO_NOSIGPIPE,&one,sizeof(one));
            // macOS Unix sockets otherwise commonly allow only a small chunk
            // per timer tick; a 1920x1200 frame must not take many seconds.
            int capacity=1024*1024;
            if(setsockopt(peer,SOL_SOCKET,SO_SNDBUF,&capacity,sizeof(capacity))!=0){[self closePeer];return;}
            do{self.lease=arc4random();}while(!self.lease);
            self.resyncID=0;
            self.lastPeer=nowMS();self.sequence=0;self.incoming=[NSMutableData new];
            self.pendingBuffer=self.latestBuffer;
            int fd=self.client;uint32_t lease=self.lease;__weak M2Bridge *weak=self;
            self.readSource=dispatch_source_create(DISPATCH_SOURCE_TYPE_READ,(uintptr_t)fd,0,dispatch_get_main_queue());
            dispatch_source_set_event_handler(self.readSource,^{
                M2Bridge *s=weak;if(s && s.client==fd && s.lease==lease)[s poll:nil];
            });
            dispatch_resume(self.readSource);
            NSMutableDictionary *hello=[self.session mutableCopy];[hello addEntriesFromDictionary:@{@"lease":@(self.lease),@"width":@(self.width),@"height":@(self.height),@"schema":@1}];
            [self jsonKind:1 value:hello];self.logEvent(@"bridge_connected",@{@"lease":@(self.lease),@"peer_uid":@(uid)});
        }
    }
    if(self.client<0)return;
    // WindowServer transitions can stall this GUI main queue for several
    // seconds. Capture/input freshness remains 3s; only transport expiry gets
    // a bounded 30s grace so a valid session can recover without reconnecting.
    if(nowMS()-self.lastPeer>30000 || !self.ready() || ![self sameSession]){[self closePeer];return;}
    if(!self.lastCaptureSignal || nowMS()-self.lastCaptureSignal>3000)[self pauseCapture];
    uint8_t bytes[8192];ssize_t n=recv(self.client,bytes,sizeof(bytes),0);
    if(n==0 || (n<0 && errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR)){[self closePeer];return;}
    if(n>0)[self.incoming appendBytes:bytes length:(NSUInteger)n];
    if(self.incoming.length>32768){[self closePeer];return;}
    for(int count=0;count<32 && self.incoming.length>=4;count++) {
        uint32_t length;memcpy(&length,self.incoming.bytes,4);length=ntohl(length);
        if(length<2 || length>4096){[self closePeer];return;}
        if(self.incoming.length<length+4)break;
        const uint8_t *raw=self.incoming.bytes;
        if(raw[4]!=4){[self closePeer];return;}
        NSData *data=[NSData dataWithBytes:raw+5 length:length-1];
        id value=[NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
        [self.incoming replaceBytesInRange:NSMakeRange(0,length+4) withBytes:NULL length:0];
        if(![self input:value]){[self closePeer];return;}
    }
    if(!self.outgoing && self.sentGeneration!=self.captureGeneration) {
        NSMutableDictionary *state=[@{@"lease":@(self.lease),@"generation":@(self.captureGeneration),@"paused":@(self.capturePaused)} mutableCopy];
        if(self.resyncID)state[@"resync"]=@(self.resyncID);
        [self jsonKind:6 value:state];
        self.sentGeneration=self.captureGeneration;
    }
    if(!self.outgoing && self.audioQueue.count) {
        NSDictionary *audio=self.audioQueue.firstObject;[self.audioQueue removeObjectAtIndex:0];
        if(nowMS()-[audio[@"at"] unsignedLongLongValue]<=200 && !self.capturePaused)
            [self queueKind:7 payload:audio[@"packet"]];
    }
    if(!self.outgoing && self.pendingBuffer) {
        id pending=self.pendingBuffer;
        self.pendingBuffer=nil;
        [self enqueueFrame:(__bridge CVPixelBufferRef)pending];
    }
    if(!self.outgoing && nowMS()-self.lastHeartbeat>=500){self.lastHeartbeat=nowMS();[self jsonKind:3 value:@{@"lease":@(self.lease)}];}
    uint64_t began=nowMS();
    for(int attempts=0;self.outgoing && attempts<16 && nowMS()-began<2;attempts++) {
        const uint8_t *raw=self.outgoing.bytes;
        ssize_t sent=send(self.client,raw+self.offset,self.outgoing.length-self.offset,0);
        if(sent>0){self.offset+=(NSUInteger)sent;if(self.offset==self.outgoing.length){self.outgoing=nil;self.offset=0;}}
        else if(sent<0 && errno==EINTR)continue;
        else if(sent<0 && (errno==EAGAIN || errno==EWOULDBLOCK))break;
        else {[self closePeer];break;}
    }
    if(!self.outgoing && self.writeSource){dispatch_source_cancel(self.writeSource);self.writeSource=nil;}
    if(self.client>=0 && !self.outgoing && (self.pendingBuffer || self.audioQueue.count)) {
        __weak M2Bridge *weak=self;dispatch_async(dispatch_get_main_queue(),^{[weak poll:nil];});
    }
}
- (void)clearAudio {self.audioQueue=nil;}
- (void)publishAudio:(NSData *)pcm captured:(uint64_t)captured {
    if(self.client<0 || !self.clientAudio || self.capturePaused || !self.ready() || ![self sameSession] ||
       !self.lastCaptureSignal || nowMS()-self.lastCaptureSignal>3000 ||
       pcm.length<4 || pcm.length>19200 || pcm.length%4 || captured>nowMS() || nowMS()-captured>200)return;
    if(!self.audioQueue)self.audioQueue=[NSMutableArray new];
    while(self.audioQueue.count>=5)[self.audioQueue removeObjectAtIndex:0];
    uint32_t lease=htonl(self.lease);uint64_t gen=CFSwapInt64HostToBig(self.captureGeneration),at=CFSwapInt64HostToBig(captured);
    NSMutableData *packet=[NSMutableData dataWithBytes:&lease length:4];[packet appendBytes:&gen length:8];[packet appendBytes:&at length:8];[packet appendData:pcm];
    [self.audioQueue addObject:@{@"at":@(captured),@"packet":packet}];[self poll:nil];
}
- (void)publish:(CVPixelBufferRef)buffer {
    // Invalidate pre-stall queued input even if a capture callback beats the
    // watchdog when the GUI thread resumes.
    if(self.lastCaptureSignal && nowMS()-self.lastCaptureSignal>3000)[self pauseCapture];
    if(self.capturePaused){self.previousPixels=nil;self.captureGeneration++;self.logEvent(@"bridge_capture_resumed",@{@"lease":@(self.lease)});}
    self.capturePaused=NO;self.pauseFrameLease=0;
    self.lastFrame=nowMS();self.lastCaptureSignal=self.lastFrame;
    self.latestBuffer=(__bridge id)buffer;
    // Only pending captures may be replaced. A transmitted patch is always
    // computed against the last queued pixels for this specific connection.
    if(self.client>=0){self.pendingBuffer=self.latestBuffer;[self poll:nil];}
}
- (void)captureIdle {
    // An idle ScreenCaptureKit sample is a live source with unchanged pixels,
    // not capture failure. Never authorize before a complete baseline exists.
    if(self.lastCaptureSignal && nowMS()-self.lastCaptureSignal>3000)[self pauseCapture];
    if(!self.capturePaused && self.latestBuffer && self.ready())[self setLastCaptureSignal:nowMS()];
}
- (void)pauseCapture {
    if(!self.capturePaused) {
        [self clearAudio];self.capturePaused=YES;self.captureGeneration++;[self releaseInputs];
        self.latestBuffer=nil;self.pendingBuffer=nil;self.previousPixels=nil;
        self.logEvent(@"bridge_capture_suspended",@{@"lease":@(self.lease)});
    }
    if(self.client<0 || self.pauseFrameLease==self.lease || !self.width || !self.height)return;
    // This is a new synthetic frame, never a cached user's desktop. Preserve
    // any partially transmitted packet; the placeholder follows its boundary.
    CVPixelBufferRef frame=NULL;
    if(CVPixelBufferCreate(NULL,self.width,self.height,kCVPixelFormatType_32BGRA,NULL,&frame)!=kCVReturnSuccess)return;
    if(CVPixelBufferLockBaseAddress(frame,0)!=kCVReturnSuccess){CVPixelBufferRelease(frame);return;}
    CGColorSpaceRef space=CGColorSpaceCreateDeviceRGB();
    CGContextRef context=CGBitmapContextCreate(CVPixelBufferGetBaseAddress(frame),self.width,self.height,8,CVPixelBufferGetBytesPerRow(frame),space,kCGBitmapByteOrder32Little|kCGImageAlphaPremultipliedFirst);
    if(context) {
        CGContextSetRGBFillColor(context,.08,.10,.14,1);CGContextFillRect(context,CGRectMake(0,0,self.width,self.height));
        [NSGraphicsContext saveGraphicsState];[NSGraphicsContext setCurrentContext:[NSGraphicsContext graphicsContextWithCGContext:context flipped:NO]];
        NSDictionary *style=@{NSFontAttributeName:[NSFont systemFontOfSize:24],NSForegroundColorAttributeName:NSColor.whiteColor};
        [[NSString stringWithFormat:@"ZMacRDS | UID %u\nDesktop capture paused.\nUnlock this Mac session to resume.\nInput is disabled until a fresh desktop frame arrives.",getuid()] drawInRect:NSMakeRect(40,40,MAX(1,(double)self.width-80),MAX(1,(double)self.height-80)) withAttributes:style];
        [NSGraphicsContext restoreGraphicsState];CGContextRelease(context);
    }
    CGColorSpaceRelease(space);CVPixelBufferUnlockBaseAddress(frame,0);
    if(context){self.pendingBuffer=(__bridge id)frame;self.pauseFrameLease=self.lease;}
    CVPixelBufferRelease(frame);
}
- (void)enqueueFrame:(CVPixelBufferRef)buffer {
    size_t w=CVPixelBufferGetWidth(buffer),h=CVPixelBufferGetHeight(buffer);
    size_t stride=CVPixelBufferGetBytesPerRow(buffer);
    if(!self.ready() || w!=self.width || h!=self.height || !w || !h ||
       w>4096 || h>2160 || stride<w*4 ||
       CVPixelBufferLockBaseAddress(buffer,kCVPixelBufferLock_ReadOnly)!=kCVReturnSuccess) {
        [self closePeer];return;
    }
    const uint8_t *pixels=CVPixelBufferGetBaseAddress(buffer);
    if(!pixels) {
        CVPixelBufferUnlockBaseAddress(buffer,kCVPixelBufferLock_ReadOnly);
        [self closePeer];return;
    }
    size_t x0=0,y0=0,x1=w,y1=h;
    BOOL delta=self.regions && self.previousPixels!=nil;
    if(delta) {
        // Compare against what this viewer can reconstruct, including changes
        // in capture samples skipped while the previous packet was in flight.
        const uint8_t *prior=self.previousPixels.bytes;
        x0=w;y0=h;x1=0;y1=0;
        for(size_t y=0;y<h;y++) {
            const uint8_t *row=pixels+y*stride,*old=prior+y*w*4;
            if(memcmp(row,old,w*4)==0)continue;
            y0=MIN(y0,y);y1=y+1;
            for(size_t x=0;x<w;x+=32) {
                size_t end=MIN(x+32,w);
                if(x>=x0 && end<=x1)continue;
                if(memcmp(row+x*4,old+x*4,(end-x)*4)!=0) {
                    x0=MIN(x0,x);x1=MAX(x1,end);
                }
            }
        }
        if(x1==0) {
            CVPixelBufferUnlockBaseAddress(buffer,kCVPixelBufferLock_ReadOnly);
            return;
        }
    }
    size_t width=x1-x0,height=y1-y0,rowBytes=width*4;
    size_t headerSize=delta ? 20 : 12;
    size_t body=1+headerSize+height*rowBytes;
    NSMutableData *packet=[NSMutableData dataWithLength:4+body];
    uint8_t *out=packet.mutableBytes;
    uint32_t length=htonl((uint32_t)body);
    memcpy(out,&length,4);out[4]=delta ? 5 : 2;
    uint32_t header[]={htonl((uint32_t)x0),htonl((uint32_t)y0),htonl((uint32_t)width),
                       htonl((uint32_t)height),htonl((uint32_t)rowBytes)};
    memcpy(out+5,header+(delta ? 0 : 2),headerSize);
    if(self.regions && !self.previousPixels)
        self.previousPixels=[NSMutableData dataWithLength:w*h*4];
    uint8_t *prior=self.previousPixels.mutableBytes;
    for(size_t y=y0;y<y1;y++) {
        const uint8_t *src=pixels+y*stride+x0*4;
        memcpy(out+5+headerSize+(y-y0)*rowBytes,src,rowBytes);
        if(prior)memcpy(prior+(y*w+x0)*4,src,rowBytes);
    }
    CVPixelBufferUnlockBaseAddress(buffer,kCVPixelBufferLock_ReadOnly);
    self.outgoing=packet;self.offset=0;self.frameSequence++;[self armWrite];
}
@end
