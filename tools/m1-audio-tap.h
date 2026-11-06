// Private, inclusion-only process output. No global tap or physical input.
#import <CoreAudio/CoreAudio.h>
#import <CoreAudio/AudioHardwareTapping.h>
#import <CoreAudio/CATapDescription.h>
#import <AVFoundation/AVFoundation.h>
#include <libproc.h>
#include <sys/proc_info.h>

static inline NSDictionary *audioProcess(pid_t pid) {
    struct proc_bsdinfo p={0};
    if(proc_pidinfo(pid,PROC_PIDTBSDINFO,0,&p,sizeof(p))!=sizeof(p) || p.pbi_uid!=getuid() || p.pbi_ruid!=getuid())return nil;
    return @{@"pid":@(pid),@"seconds":@(p.pbi_start_tvsec),@"useconds":@(p.pbi_start_tvusec)};
}
static inline AudioObjectPropertyAddress audioProperty(AudioObjectPropertySelector selector) {
    return (AudioObjectPropertyAddress){selector,kAudioObjectPropertyScopeGlobal,kAudioObjectPropertyElementMain};
}
API_AVAILABLE(macos(14.2))
@interface M1Audio : NSObject
@property(weak) M2Bridge *bridge;
@property NSDictionary *session;
@property NSArray<NSDictionary *> *processes;
@property BOOL ready, stopped, updating;
@property uint64_t samples, generation, lastDiagnostic;
@property BOOL sawSignal;
@property AudioObjectID tap, device;
@property AudioDeviceIOProcID io;
@property AVAudioFormat *sourceFormat;
@property AVAudioConverter *converter;
@property dispatch_queue_t queue, control;
@property dispatch_semaphore_t slots;
@property void (^logEvent)(NSString *,NSDictionary *);
- (void)refresh;
- (void)stop;
@end
@implementation M1Audio
- (instancetype)init {
    if((self=[super init])){_control=dispatch_queue_create("org.zmacrds.audio-control",DISPATCH_QUEUE_SERIAL);_queue=dispatch_queue_create("org.zmacrds.audio",DISPATCH_QUEUE_SERIAL);_slots=dispatch_semaphore_create(4);}return self;
}
- (BOOL)authorized {
    NSDictionary *who=identity();
    return !self.stopped && self.bridge.client>=0 && self.bridge.clientAudio && !self.bridge.capturePaused && CGPreflightScreenCaptureAccess() && [who[@"uid"] isEqual:self.session[@"uid"]] &&
        [who[@"gui_uid"] isEqual:self.session[@"uid"]] && [who[@"audit_session_id"] isEqual:self.session[@"audit_session_id"]] && [who[@"login_complete"] boolValue];
}
- (BOOL)processesValid {
    if(!self.processes.count)return NO;
    for(NSDictionary *p in self.processes) {
        AudioObjectPropertyAddress address=audioProperty(kAudioProcessPropertyPID);pid_t pid=0;UInt32 size=sizeof(pid);
        if(AudioObjectGetPropertyData([p[@"object"] unsignedIntValue],&address,0,NULL,&size,&pid)!=noErr || pid!=[p[@"pid"] intValue])return NO;
        NSDictionary *current=audioProcess(pid);
        if(!current || ![current[@"seconds"] isEqual:p[@"seconds"]] || ![current[@"useconds"] isEqual:p[@"useconds"]])return NO;
    }
    return YES;
}
- (void)retire {
    self.ready=NO;self.generation++;M2Bridge *bridge=self.bridge;dispatch_async(dispatch_get_main_queue(),^{[bridge clearAudio];});
    if(self.device && self.io){AudioDeviceStop(self.device,self.io);AudioDeviceDestroyIOProcID(self.device,self.io);self.io=NULL;}
    if(self.device){AudioHardwareDestroyAggregateDevice(self.device);self.device=0;}
    if(self.tap){AudioHardwareDestroyProcessTap(self.tap);self.tap=0;}
    self.converter=nil;self.sourceFormat=nil;
}
- (void)refresh {
    if(self.stopped || self.updating)return;self.updating=YES;
    dispatch_async(self.control,^{[self refreshNow];self.updating=NO;});
}
- (void)refreshNow {
    if(![self authorized]){[self retire];return;}
    AudioObjectPropertyAddress address=audioProperty(kAudioHardwarePropertyProcessObjectList);UInt32 size=0;
    if(AudioObjectGetPropertyDataSize(kAudioObjectSystemObject,&address,0,NULL,&size)!=noErr || size>1024*1024 || size%sizeof(AudioObjectID))return;
    NSMutableData *data=[NSMutableData dataWithLength:size];
    if(AudioObjectGetPropertyData(kAudioObjectSystemObject,&address,0,NULL,&size,data.mutableBytes)!=noErr)return;
    NSMutableArray *processes=[NSMutableArray new],*objects=[NSMutableArray new];AudioObjectID *ids=data.mutableBytes;
    for(UInt32 i=0;i<size/sizeof(AudioObjectID);i++) {
        pid_t pid=0;UInt32 length=sizeof(pid);address=audioProperty(kAudioProcessPropertyPID);
        if(AudioObjectGetPropertyData(ids[i],&address,0,NULL,&length,&pid)!=noErr || pid==getpid())continue;
        NSDictionary *who=audioProcess(pid);if(!who)continue;
        UInt32 running=0;length=sizeof(running);address=audioProperty(kAudioProcessPropertyIsRunningOutput);
        if(AudioObjectGetPropertyData(ids[i],&address,0,NULL,&length,&running)!=noErr || !running)continue;
        NSMutableDictionary *p=[who mutableCopy];p[@"object"]=@(ids[i]);[processes addObject:p];[objects addObject:@(ids[i])];
    }
    if(self.ready && [self.processes isEqual:processes])return;
    [self retire];self.processes=processes;if(!objects.count)return;
    CATapDescription *description=[[CATapDescription alloc] initStereoMixdownOfProcesses:objects];
    description.name=[NSString stringWithFormat:@"ZMacRDS UID %u",getuid()];description.private=YES;description.muteBehavior=CATapMutedWhenTapped;
    AudioObjectID tap=0;OSStatus result=AudioHardwareCreateProcessTap(description,&tap);self.tap=tap;
    if(result!=noErr){self.logEvent(@"audio_error",@{@"operation":@"process_tap",@"status":@(result)});return;}
    CFStringRef rawUID=NULL;size=sizeof(rawUID);address=audioProperty(kAudioTapPropertyUID);
    result=AudioObjectGetPropertyData(tap,&address,0,NULL,&size,&rawUID);NSString *tapUID=CFBridgingRelease(rawUID);
    if(result!=noErr || !tapUID){[self retire];return;}
    AudioStreamBasicDescription format={0};size=sizeof(format);address=audioProperty(kAudioTapPropertyFormat);
    result=AudioObjectGetPropertyData(tap,&address,0,NULL,&size,&format);
    if(result!=noErr || format.mFormatID!=kAudioFormatLinearPCM || format.mChannelsPerFrame!=2 || format.mSampleRate<8000 || format.mSampleRate>192000){[self retire];return;}
    self.sourceFormat=[[AVAudioFormat alloc] initWithStreamDescription:&format];
    AVAudioFormat *output=[[AVAudioFormat alloc] initWithCommonFormat:AVAudioPCMFormatInt16 sampleRate:48000 channels:2 interleaved:YES];
    self.converter=[[AVAudioConverter alloc] initFromFormat:self.sourceFormat toFormat:output];
    if(!self.converter){[self retire];return;}
    // No physical subdevice: aggregate inputs contain only this private tap.
    NSDictionary *aggregate=@{@kAudioAggregateDeviceNameKey:description.name,@kAudioAggregateDeviceUIDKey:NSUUID.UUID.UUIDString,
        @kAudioAggregateDeviceIsPrivateKey:@YES,@kAudioAggregateDeviceTapAutoStartKey:@YES,
        @kAudioAggregateDeviceTapListKey:@[@{@kAudioSubTapUIDKey:tapUID,@kAudioSubTapDriftCompensationKey:@YES}]};
    AudioObjectID device=0;result=AudioHardwareCreateAggregateDevice((__bridge CFDictionaryRef)aggregate,&device);self.device=device;
    if(result!=noErr){self.logEvent(@"audio_error",@{@"operation":@"aggregate",@"status":@(result)});[self retire];return;}
    if(!self.queue)self.queue=dispatch_queue_create("org.zmacrds.audio",DISPATCH_QUEUE_SERIAL);
    if(!self.slots)self.slots=dispatch_semaphore_create(4);
    __weak M1Audio *weak=self;AudioDeviceIOProcID io=NULL;
    result=AudioDeviceCreateIOProcIDWithBlock(&io,device,self.queue,^(const AudioTimeStamp *now,const AudioBufferList *input,const AudioTimeStamp *inputTime,AudioBufferList *out,const AudioTimeStamp *outTime){
        (void)now;(void)out;(void)outTime;M1Audio *s=weak;
        if(!s || !s.ready || !input || !inputTime || !(inputTime->mFlags&kAudioTimeStampHostTimeValid))return;
        dispatch_semaphore_t slots=s.slots;if(dispatch_semaphore_wait(slots,DISPATCH_TIME_NOW)!=0)return;
        uint64_t generation=s.generation,host=inputTime->mHostTime;
        AVAudioFormat *source=s.sourceFormat;AVAudioConverter *converter=s.converter;const AudioStreamBasicDescription *asbd=source.streamDescription;
        if(!source || !converter || !asbd){dispatch_semaphore_signal(slots);return;}
        UInt32 buffers=asbd->mFormatFlags&kAudioFormatFlagIsNonInterleaved ? 2 : 1;
        if(input->mNumberBuffers!=buffers || !asbd->mBytesPerFrame){dispatch_semaphore_signal(slots);return;}
        UInt32 frames=input->mBuffers[0].mDataByteSize/asbd->mBytesPerFrame;
        if(!frames || frames>8192){dispatch_semaphore_signal(slots);return;}
        AVAudioPCMBuffer *pcm=[[AVAudioPCMBuffer alloc] initWithPCMFormat:source frameCapacity:frames];pcm.frameLength=frames;
        AudioBufferList *destination=pcm.mutableAudioBufferList;BOOL valid=destination->mNumberBuffers==buffers;
        if(valid)for(UInt32 b=0;b<buffers;b++) {
            if(!input->mBuffers[b].mData || input->mBuffers[b].mDataByteSize!=destination->mBuffers[b].mDataByteSize){valid=NO;break;}
            memcpy(destination->mBuffers[b].mData,input->mBuffers[b].mData,input->mBuffers[b].mDataByteSize);
        }
        if(!valid){dispatch_semaphore_signal(slots);return;}
        UInt32 capacity=(UInt32)ceil(frames*48000/source.sampleRate)+64;
        AVAudioPCMBuffer *converted=[[AVAudioPCMBuffer alloc] initWithPCMFormat:output frameCapacity:capacity];
        __block BOOL supplied=NO;NSError *error;
        [converter convertToBuffer:converted error:&error withInputFromBlock:^AVAudioBuffer *(AVAudioPacketCount requested,AVAudioConverterInputStatus *status){
            (void)requested;if(supplied){*status=AVAudioConverterInputStatus_NoDataNow;return nil;}supplied=YES;*status=AVAudioConverterInputStatus_HaveData;return pcm;
        }];
        float sourcePeak=0;
        if(asbd->mBitsPerChannel==32 && (asbd->mFormatFlags&kAudioFormatFlagIsFloat))for(UInt32 b=0;b<input->mNumberBuffers;b++) {
            const float *values=input->mBuffers[b].mData;
            for(UInt32 n=0;n<input->mBuffers[b].mDataByteSize/4;n++)if(isfinite(values[n]))sourcePeak=fmaxf(sourcePeak,fabsf(values[n]));
        }
        NSData *bytes=(!error && converted.frameLength) ? [NSData dataWithBytes:converted.audioBufferList->mBuffers[0].mData length:converted.frameLength*4] : nil;
        UInt32 sourceFlags=asbd->mFormatFlags;
        dispatch_async(s.control,^{
            M1Audio *validated=weak;BOOL validSource=validated && generation==validated.generation && [validated authorized] && [validated processesValid];
        dispatch_async(dispatch_get_main_queue(),^{
            M1Audio *a=weak;
            if(a && !a.sawSignal && nowMS()-a.lastDiagnostic>10000) {
                a.lastDiagnostic=nowMS();a.sawSignal=sourcePeak>0;
                a.logEvent(@"audio_signal",@{@"source_peak":@(sourcePeak),@"frames":@(frames),@"bytes":@(bytes.length),@"buffers":@(buffers),@"flags":@(sourceFlags)});
            }
            mach_timebase_info_data_t tb;mach_timebase_info(&tb);uint64_t current=mach_absolute_time();
            long double age=current>=host ? (long double)(current-host)*tb.numer/tb.denom/1000000.L : -1;
            if(validSource && a && a.ready && generation==a.generation && bytes && age>=0 && age<=200) {
                [a.bridge publishAudio:bytes captured:nowMS()-(uint64_t)age];a.samples+=converted.frameLength;
            }
            dispatch_semaphore_signal(slots);
        });
        });
    });
    self.io=io;if(result==noErr){self.ready=YES;result=AudioDeviceStart(device,io);}
    if(result!=noErr){self.logEvent(@"audio_error",@{@"operation":@"start",@"status":@(result)});[self retire];return;}
    self.logEvent(@"audio_filter",@{@"uid":@(getuid()),@"processes":processes,@"rate":@48000,@"channels":@2,@"source_rate":@(format.mSampleRate),@"source":@"private_process_tap"});
}
- (void)stop {self.stopped=YES;dispatch_async(self.control,^{[self retire];});}
@end
