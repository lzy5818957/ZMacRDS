// Hardware H.264 and Core Audio Opus, with no network or device access.
// fd 3: 1920x1200 BGRA; fd 4: stereo 48 kHz S16LE; fd 5/6: framed access units.
#import <Foundation/Foundation.h>
#import <AudioToolbox/AudioToolbox.h>
#import <VideoToolbox/VideoToolbox.h>
#include <arpa/inet.h>
#include <errno.h>
#include <signal.h>
#include <unistd.h>

enum { WIDTH=1920, HEIGHT=1200, AUDIO_FRAMES=960, PCM_SIZE=AUDIO_FRAMES*4 };
static BOOL ioAll(int fd, void *data, size_t length, BOOL output) {
    uint8_t *p=data;
    while(length) {
        ssize_t n=output ? write(fd,p,length) : read(fd,p,length);
        if(n<0 && errno==EINTR)continue;
        if(n<=0)return NO;
        p+=n;length-=(size_t)n;
    }
    return YES;
}
static BOOL sendUnit(int fd, uint32_t index, NSData *data) {
    if(!data.length || data.length>4*1024*1024)return NO;
    uint32_t header[]={htonl((uint32_t)data.length),htonl(index)};
    return ioAll(fd,header,sizeof(header),YES) && ioAll(fd,(void *)data.bytes,data.length,YES);
}
static void quitCode(const char *operation, OSStatus status) {
    fprintf(stderr,"media encoder: %s (%d)\n",operation,(int)status);
    _exit(1);
}
static void videoOut(void *ctx, void *token, OSStatus status, VTEncodeInfoFlags flags, CMSampleBufferRef sample) {
    (void)ctx;(void)token;
    if(status!=noErr)quitCode("video output",status);
    if((flags&kVTEncodeInfo_FrameDropped) || !sample)return;
    @autoreleasepool {
        NSData *start=[NSData dataWithBytes:(const uint8_t[]){0,0,0,1} length:4];
        NSMutableData *output=[NSMutableData new];
        CFArrayRef attachments=CMSampleBufferGetSampleAttachmentsArray(sample,false);
        BOOL key=!attachments || CFArrayGetCount(attachments)==0 ||
            !CFDictionaryContainsKey(CFArrayGetValueAtIndex(attachments,0),kCMSampleAttachmentKey_NotSync);
        CMFormatDescriptionRef format=CMSampleBufferGetFormatDescription(sample);
        size_t count=0;int lengthSize=0;
        if(CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format,0,NULL,NULL,&count,&lengthSize)!=noErr ||
           lengthSize!=4 || count>16)quitCode("invalid H.264 format",0);
        if(key)for(size_t i=0;i<count;i++){
            const uint8_t *bytes=NULL;size_t size=0;
            if(CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format,i,&bytes,&size,NULL,NULL)!=noErr)
                quitCode("missing H.264 parameters",0);
            [output appendData:start];[output appendBytes:bytes length:size];
        }
        CMBlockBufferRef data=CMSampleBufferGetDataBuffer(sample);
        size_t size=CMBlockBufferGetDataLength(data);
        if(!size || size>4*1024*1024)quitCode("invalid access unit size",0);
        NSMutableData *bytes=[NSMutableData dataWithLength:size];
        if(CMBlockBufferCopyDataBytes(data,0,size,bytes.mutableBytes)!=noErr)quitCode("copy access unit",0);
        for(size_t offset=0;offset<size;){
            uint32_t len=0;
            if(size-offset<4)quitCode("short NAL header",0);
            memcpy(&len,(const uint8_t *)bytes.bytes+offset,4);len=ntohl(len);offset+=4;
            if(!len || len>size-offset)quitCode("invalid NAL size",0);
            [output appendData:start];[output appendBytes:(const uint8_t *)bytes.bytes+offset length:len];offset+=len;
        }
        uint32_t index=(uint32_t)CMTimeConvertScale(CMSampleBufferGetPresentationTimeStamp(sample),30,kCMTimeRoundingMethod_Default).value;
        if(!sendUnit(5,index,output))_exit(0);
    }
}
static void video(void) {
    NSDictionary *encoder=@{(__bridge NSString *)kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder:@YES};
    NSDictionary *attrs=@{(__bridge NSString *)kCVPixelBufferPixelFormatTypeKey:@(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
        (__bridge NSString *)kCVPixelBufferWidthKey:@(WIDTH),(__bridge NSString *)kCVPixelBufferHeightKey:@(HEIGHT)};
    VTCompressionSessionRef session=NULL;
    OSStatus status=VTCompressionSessionCreate(NULL,WIDTH,HEIGHT,kCMVideoCodecType_H264,
        (__bridge CFDictionaryRef)encoder,(__bridge CFDictionaryRef)attrs,NULL,videoOut,NULL,&session);
    if(status!=noErr)quitCode("hardware video initialization",status);
    NSDictionary *properties=@{
        (__bridge NSString *)kVTCompressionPropertyKey_RealTime:@YES,
        (__bridge NSString *)kVTCompressionPropertyKey_AllowFrameReordering:@NO,
        // Match the Constrained Baseline subprofile advertised by WebRTC.
        (__bridge NSString *)kVTCompressionPropertyKey_ProfileLevel:(__bridge NSString *)kVTProfileLevel_H264_ConstrainedBaseline_AutoLevel,
        (__bridge NSString *)kVTCompressionPropertyKey_AverageBitRate:@8000000,
        (__bridge NSString *)kVTCompressionPropertyKey_ExpectedFrameRate:@30,
        (__bridge NSString *)kVTCompressionPropertyKey_MaxKeyFrameInterval:@30,
        (__bridge NSString *)kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration:@1,
    };
    status=VTSessionSetProperties(session,(__bridge CFDictionaryRef)properties);
    if(status!=noErr)quitCode("hardware video settings",status);
    if((status=VTCompressionSessionPrepareToEncodeFrames(session))!=noErr)quitCode("video preparation",status);
    uint8_t *raw=malloc(WIDTH*HEIGHT*4);
    if(!raw)quitCode("video memory",0);
    CVPixelBufferRef source=NULL;
    if(CVPixelBufferCreateWithBytes(NULL,WIDTH,HEIGHT,kCVPixelFormatType_32BGRA,
        raw,WIDTH*4,NULL,NULL,NULL,&source)!=kCVReturnSuccess)quitCode("BGRA pixel buffer",0);
    VTPixelTransferSessionRef conversion=NULL;
    status=VTPixelTransferSessionCreate(NULL,&conversion);
    if(status!=noErr)quitCode("pixel conversion initialization",status);
    CVPixelBufferPoolRef pool=VTCompressionSessionGetPixelBufferPool(session);
    if(!pool)quitCode("missing hardware pixel pool",0);
    uint32_t index;
    while(ioAll(3,&index,sizeof(index),NO) && ioAll(3,raw,WIDTH*HEIGHT*4,NO)) {
        CVPixelBufferRef pixel=NULL;
        if(CVPixelBufferPoolCreatePixelBuffer(NULL,pool,&pixel)!=kCVReturnSuccess)quitCode("video pixel buffer",0);
        status=VTPixelTransferSessionTransferImage(conversion,source,pixel);
        if(status!=noErr)quitCode("BGRA to NV12 conversion",status);
        status=VTCompressionSessionEncodeFrame(session,pixel,CMTimeMake(ntohl(index),30),CMTimeMake(1,30),NULL,NULL,NULL);
        CVPixelBufferRelease(pixel);
        if(status!=noErr)quitCode("encode video",status);
    }
    VTPixelTransferSessionInvalidate(conversion);
    CFRelease(conversion);
    CVPixelBufferRelease(source);
    free(raw);
    VTCompressionSessionCompleteFrames(session,kCMTimeInvalid);
    CFRelease(session);
}
typedef struct { uint8_t data[PCM_SIZE]; UInt32 at; BOOL ended; } AudioInput;
static OSStatus providePCM(AudioConverterRef converter, UInt32 *packets, AudioBufferList *data,
    AudioStreamPacketDescription **descriptions, void *context) {
    (void)converter;
    if(descriptions)*descriptions=NULL;
    AudioInput *input=context;
    if(input->at==AUDIO_FRAMES) {
        if(!ioAll(4,input->data,sizeof(input->data),NO)) {
            input->ended=YES;*packets=0;return noErr;
        }
        input->at=0;
    }
    *packets=MIN(*packets,AUDIO_FRAMES-input->at);
    data->mNumberBuffers=1;
    data->mBuffers[0]=(AudioBuffer){.mNumberChannels=2,.mDataByteSize=*packets*4,.mData=input->data+input->at*4};
    input->at+=*packets;
    return noErr;
}
static void audio(void) {
    AudioStreamBasicDescription from={.mSampleRate=48000,.mFormatID=kAudioFormatLinearPCM,
        .mFormatFlags=kLinearPCMFormatFlagIsSignedInteger|kLinearPCMFormatFlagIsPacked,
        .mBytesPerPacket=4,.mFramesPerPacket=1,.mBytesPerFrame=4,.mChannelsPerFrame=2,.mBitsPerChannel=16};
    AudioStreamBasicDescription to={.mSampleRate=48000,.mFormatID=kAudioFormatOpus,.mFramesPerPacket=AUDIO_FRAMES,.mChannelsPerFrame=2};
    AudioConverterRef codec=NULL;OSStatus status=AudioConverterNew(&from,&to,&codec);
    if(status!=noErr)quitCode("Opus initialization",status);
    UInt32 bitrate=96000;
    if((status=AudioConverterSetProperty(codec,kAudioConverterEncodeBitRate,sizeof(bitrate),&bitrate))!=noErr)
        quitCode("Opus bitrate",status);
    AudioInput input={.at=AUDIO_FRAMES};
    uint32_t index=0;
    while(!input.ended) {
        @autoreleasepool {
            uint8_t packet[2048];UInt32 count=1;
            AudioBufferList output={.mNumberBuffers=1,.mBuffers={{.mNumberChannels=2,.mDataByteSize=sizeof(packet),.mData=packet}}};
            AudioStreamPacketDescription desc={0};
            status=AudioConverterFillComplexBuffer(codec,providePCM,&input,&count,&output,&desc);
            if(status!=noErr)quitCode("encode Opus",status);
            if(count && (desc.mDataByteSize>sizeof(packet) ||
                !sendUnit(6,index++,[NSData dataWithBytes:packet length:desc.mDataByteSize])))_exit(0);
        }
    }
    AudioConverterDispose(codec);
}
int main(void) {
    signal(SIGPIPE,SIG_IGN);
    @autoreleasepool {
        dispatch_group_t work=dispatch_group_create();
        dispatch_queue_t queue=dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0);
        dispatch_group_async(work,queue,^{audio();});
        dispatch_group_async(work,queue,^{video();});
        dispatch_group_wait(work,DISPATCH_TIME_FOREVER);
    }
    return 0;
}
