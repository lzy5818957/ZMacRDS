#import <CoreVideo/CoreVideo.h>

// Never keep ScreenCaptureKit's finite producer-owned surface pool as the
// transport/idle cache. Copy visible rows before returning the callback.
static inline CVPixelBufferRef copyCapturePixels(CVPixelBufferRef source) CF_RETURNS_RETAINED {
    if(!source || CVPixelBufferGetPixelFormatType(source)!=kCVPixelFormatType_32BGRA ||
       CVPixelBufferIsPlanar(source))return NULL;
    size_t width=CVPixelBufferGetWidth(source),height=CVPixelBufferGetHeight(source);
    if(!width || !height || width>4096 || height>2160 || CVPixelBufferGetBytesPerRow(source)<width*4)return NULL;
    CVPixelBufferRef copy=NULL;
    if(CVPixelBufferCreate(NULL,width,height,kCVPixelFormatType_32BGRA,NULL,&copy)!=kCVReturnSuccess)return NULL;
    if(CVPixelBufferLockBaseAddress(source,kCVPixelBufferLock_ReadOnly)!=kCVReturnSuccess){CVPixelBufferRelease(copy);return NULL;}
    if(CVPixelBufferLockBaseAddress(copy,0)!=kCVReturnSuccess){
        CVPixelBufferUnlockBaseAddress(source,kCVPixelBufferLock_ReadOnly);CVPixelBufferRelease(copy);return NULL;
    }
    const uint8_t *from=CVPixelBufferGetBaseAddress(source);uint8_t *to=CVPixelBufferGetBaseAddress(copy);
    BOOL valid=from && to;
    if(valid)for(size_t row=0;row<height;row++)
        memcpy(to+row*CVPixelBufferGetBytesPerRow(copy),from+row*CVPixelBufferGetBytesPerRow(source),width*4);
    CVPixelBufferUnlockBaseAddress(copy,0);CVPixelBufferUnlockBaseAddress(source,kCVPixelBufferLock_ReadOnly);
    if(!valid){CVPixelBufferRelease(copy);return NULL;}return copy;
}
