#ifndef ZMACRDS_M1_MARKER_H
#define ZMACRDS_M1_MARKER_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// 3 coloured sync cells, then 128 monochrome bits: magic, UID, nonce,
// counter and CRC16. Decode the captured pixels, never the drawing state.
enum { M1_CELL = 4, M1_CELLS = 131, M1_BYTES = 16 };
typedef struct { uint32_t uid, nonce, tick; } M1Marker;
static inline uint16_t m1CRC(const uint8_t *bytes, size_t count) {
    uint16_t crc = 0xffff;
    for (size_t i = 0; i < count; i++) {
        crc ^= (uint16_t)bytes[i] << 8;
        for (int bit = 0; bit < 8; bit++)
            crc = (uint16_t)((crc << 1) ^ ((crc & 0x8000) ? 0x1021 : 0));
    }
    return crc;
}
static inline void m1Put32(uint8_t *p, uint32_t n) {
    for (int i = 0; i < 4; i++) p[i] = (uint8_t)(n >> (24 - 8*i));
}
static inline uint32_t m1Get32(const uint8_t *p) {
    return (uint32_t)p[0]<<24 | (uint32_t)p[1]<<16 | (uint32_t)p[2]<<8 | p[3];
}
static inline void m1Encode(M1Marker marker, uint8_t bytes[M1_BYTES]) {
    bytes[0] = 0xa5; bytes[1] = 0x3c;
    m1Put32(bytes+2, marker.uid); m1Put32(bytes+6, marker.nonce); m1Put32(bytes+10, marker.tick);
    uint16_t crc = m1CRC(bytes, 14);
    bytes[14] = crc >> 8; bytes[15] = crc & 255;
}
static inline bool m1Magenta(const uint8_t *p) { return p[0]>180 && p[1]<80 && p[2]>180; }
static inline bool m1Cyan(const uint8_t *p) { return p[0]>180 && p[1]>180 && p[2]<80; }
static inline bool m1Decode(const uint8_t *bgra, size_t width, size_t height,
                            size_t stride, M1Marker *marker) {
    if (!bgra || width < M1_CELLS*M1_CELL || height < 4 || stride < width*4) return false;
    for (size_t y = 2; y < height; y += 4) {
        const uint8_t *row = bgra + y*stride;
        for (size_t x = 0; x + M1_CELLS*M1_CELL <= width; x++) {
            const uint8_t *p = row + x*4;
            if (!m1Magenta(p) || !m1Cyan(p+M1_CELL*4) || !m1Magenta(p+M1_CELL*8)) continue;
            uint8_t bytes[M1_BYTES] = {0};
            bool valid = true;
            for (size_t bit = 0; bit < 128; bit++) {
                const uint8_t *v = p + (bit+3)*M1_CELL*4;
                bool white = v[0]>180 && v[1]>180 && v[2]>180;
                bool black = v[0]<80 && v[1]<80 && v[2]<80;
                if (!white && !black) { valid = false; break; }
                bytes[bit/8] |= white << (7-bit%8);
            }
            uint16_t crc = (uint16_t)bytes[14]<<8 | bytes[15];
            if (!valid || bytes[0]!=0xa5 || bytes[1]!=0x3c || m1CRC(bytes,14)!=crc) continue;
            *marker = (M1Marker){m1Get32(bytes+2), m1Get32(bytes+6), m1Get32(bytes+10)};
            return true;
        }
    }
    return false;
}
#endif
