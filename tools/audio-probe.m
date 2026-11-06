// Bounded synthetic system-output tone, with a visible per-user window.
#import <AppKit/AppKit.h>
#import <AudioToolbox/AudioToolbox.h>
#include <math.h>
#include <stdatomic.h>
#include <sys/stat.h>
typedef struct {double phase, frequency; _Atomic(uint64_t) frames;} Tone;
static void fill(void *context,AudioQueueRef queue,AudioQueueBufferRef buffer) {
    Tone *tone=context;float *samples=buffer->mAudioData;
    for(int i=0;i<960;i++) {
        float value=.12*sin(tone->phase);tone->phase+=2*M_PI*tone->frequency/48000;
        if(tone->phase>2*M_PI)tone->phase-=2*M_PI;
        samples[i*2]=value;samples[i*2+1]=value;
    }
    tone->frames+=960;buffer->mAudioDataByteSize=960*8;AudioQueueEnqueueBuffer(queue,buffer,0,NULL);
}
int main(int argc,const char *argv[]) {
    @autoreleasepool {
        if(argc!=3)return 2;
        char *end;long frequency=strtol(argv[1],&end,10);if(*end || frequency<200 || frequency>2000)return 2;
        long seconds=strtol(argv[2],&end,10);if(*end || seconds<1 || seconds>7200)return 2;
        [NSApplication sharedApplication];[NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        NSWindow *window=[[NSWindow alloc] initWithContentRect:NSMakeRect(200,100,450,100) styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
        window.title=[NSString stringWithFormat:@"ZMacRDS Audio Probe | UID %u | %ld Hz",getuid(),frequency];
        [window makeKeyAndOrderFront:nil];[NSApp activateIgnoringOtherApps:YES];
        AudioStreamBasicDescription format={.mSampleRate=48000,.mFormatID=kAudioFormatLinearPCM,.mFormatFlags=kAudioFormatFlagIsFloat|kAudioFormatFlagIsPacked,.mBytesPerPacket=8,.mFramesPerPacket=1,.mBytesPerFrame=8,.mChannelsPerFrame=2,.mBitsPerChannel=32};
        Tone tone={.frequency=frequency};AudioQueueRef queue=NULL;
        if(AudioQueueNewOutput(&format,fill,&tone,NULL,NULL,0,&queue)!=noErr)return 1;
        for(int i=0;i<3;i++){AudioQueueBufferRef buffer;if(AudioQueueAllocateBuffer(queue,960*8,&buffer)!=noErr)return 1;fill(&tone,queue,buffer);}
        AudioQueueSetParameter(queue,kAudioQueueParam_Volume,1.0);
        OSStatus started=AudioQueueStart(queue,NULL);
        NSString *folder=[NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/ZMacRDS/AudioProbe"];
        [NSFileManager.defaultManager createDirectoryAtPath:folder withIntermediateDirectories:YES attributes:@{NSFilePosixPermissions:@0700} error:NULL];
        NSString *path=[folder stringByAppendingPathComponent:[NSString stringWithFormat:@"probe-%d.json",getpid()]];
        __block Tone *active=&tone;
        [NSTimer scheduledTimerWithTimeInterval:1 repeats:YES block:^(NSTimer *timer){
            (void)timer;UInt32 running=0,size=sizeof(running);AudioQueueGetProperty(queue,kAudioQueueProperty_IsRunning,&running,&size);
            AudioDeviceID device=0;AudioObjectPropertyAddress addr={kAudioHardwarePropertyDefaultOutputDevice,kAudioObjectPropertyScopeGlobal,kAudioObjectPropertyElementMain};size=sizeof(device);
            AudioObjectGetPropertyData(kAudioObjectSystemObject,&addr,0,NULL,&size,&device);
            UInt32 mute=999;addr=(AudioObjectPropertyAddress){kAudioDevicePropertyMute,kAudioDevicePropertyScopeOutput,kAudioObjectPropertyElementMain};size=sizeof(mute);
            OSStatus muted=AudioObjectGetPropertyData(device,&addr,0,NULL,&size,&mute);
            AudioQueueParameterValue volume=0;AudioQueueGetParameter(queue,kAudioQueueParam_Volume,&volume);
            NSDictionary *state=@{@"queue_volume":@(volume),@"uid":@(getuid()),@"pid":@(getpid()),@"hz":@(frequency),@"frames_generated":@(active->frames),@"queue_running":@(running),@"start_status":@(started),@"device":@(device),@"mute":@(mute),@"mute_status":@(muted)};
            [[NSJSONSerialization dataWithJSONObject:state options:NSJSONWritingSortedKeys error:NULL] writeToFile:path options:NSDataWritingAtomic error:NULL];chmod(path.fileSystemRepresentation,0600);
        }];
        if(started!=noErr)return 1;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW,seconds*NSEC_PER_SEC),dispatch_get_main_queue(),^{[NSApp terminate:nil];});
        [NSApp run];AudioQueueStop(queue,true);AudioQueueDispose(queue,true);
    }return 0;
}
