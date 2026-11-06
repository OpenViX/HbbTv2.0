/*
 * wpe_fb_png.h - a PNG of a part of the frame, for the automatic HbbTV banner (wpe_fb --overlay-png).
 *
 * The banner runs on top of the live picture and of enigma2's own screen. Writing its pixels into the framebuffer behind enigma2's back cannot
 * work reliably (enigma2 flips between three pages, copies the shown page into the next one before we can paint, and redraws under our pixels), so
 * enigma2 shows it itself: wpe_fb saves the visible part of the frame as a PNG in /tmp, the plugin loads it into a Pixmap widget of a skin screen.
 *
 * The PNG is written uncompressed (deflate "stored" blocks): encoding costs a copy and a CRC, and enigma2's PNG loader has nothing to inflate.
 * The pixels are straight-alpha ARGB (0xAARRGGBB), as wpe_fb keeps them in overlay mode.
 */
#ifndef WPE_FB_PNG_H
#define WPE_FB_PNG_H

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static inline uint32_t png_crc32(uint32_t crc, const uint8_t *p, size_t n)
{
    static uint32_t table[256];
    if (!table[1]) {
        for (uint32_t i = 0; i < 256; i++) {
            uint32_t c = i;
            for (int k = 0; k < 8; k++)
                c = c & 1 ? 0xedb88320u ^ (c >> 1) : c >> 1;
            table[i] = c;
        }
    }
    crc = ~crc;
    for (size_t i = 0; i < n; i++)
        crc = table[(crc ^ p[i]) & 255] ^ (crc >> 8);
    return ~crc;
}

static inline uint32_t png_adler32(const uint8_t *p, size_t n)
{
    uint32_t a = 1, b = 0;
    while (n) {
        size_t chunk = n < 5552 ? n : 5552; /* the most that cannot overflow 32 bits before the modulo */
        n -= chunk;
        while (chunk--) {
            a += *p++;
            b += a;
        }
        a %= 65521;
        b %= 65521;
    }
    return (b << 16) | a;
}

static inline void png_put32(uint8_t *p, uint32_t v)
{
    p[0] = v >> 24;
    p[1] = v >> 16;
    p[2] = v >> 8;
    p[3] = v;
}

/* Encode the w x h pixels at `argb` (row stride `stride` pixels) as an RGBA PNG. Returns a malloc'd buffer (the caller frees it), its size in *len; NULL if
 * out of memory or the size is not sensible. */
static inline uint8_t *png_encode_argb(const uint32_t *argb, size_t stride, int w, int h, size_t *len)
{
    if (w <= 0 || h <= 0 || w > 16384 || h > 16384)
        return NULL;
    const size_t rowbytes = 1 + (size_t)w * 4, raw_len = rowbytes * (size_t)h;
    const size_t blocks = (raw_len + 65534) / 65535, zlib_len = 2 + raw_len + blocks * 5 + 4;
    const size_t total = 8 + (12 + 13) + (12 + zlib_len) + 12;
    uint8_t *raw = malloc(raw_len), *out = malloc(total);
    if (!raw || !out) {
        free(raw);
        free(out);
        return NULL;
    }
    for (int y = 0; y < h; y++) {
        uint8_t *d = raw + rowbytes * (size_t)y;
        const uint32_t *s = argb + stride * (size_t)y;
        *d++ = 0; /* filter: none */
        for (int x = 0; x < w; x++, d += 4) {
            const uint32_t v = s[x];
            d[0] = (uint8_t)(v >> 16);
            d[1] = (uint8_t)(v >> 8);
            d[2] = (uint8_t)v;
            d[3] = (uint8_t)(v >> 24);
        }
    }

    uint8_t *o = out;
    static const uint8_t signature[8] = { 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n' };
    memcpy(o, signature, 8);
    o += 8;
    /* IHDR */
    png_put32(o, 13);
    memcpy(o + 4, "IHDR", 4);
    png_put32(o + 8, (uint32_t)w);
    png_put32(o + 12, (uint32_t)h);
    o[16] = 8; /* bit depth */
    o[17] = 6; /* colour type: RGBA */
    o[18] = o[19] = o[20] = 0; /* deflate, adaptive filtering, no interlace */
    png_put32(o + 21, png_crc32(0, o + 4, 17));
    o += 25;
    /* IDAT: a zlib stream of stored blocks */
    uint8_t *idat = o;
    png_put32(o, (uint32_t)zlib_len);
    memcpy(o + 4, "IDAT", 4);
    o += 8;
    *o++ = 0x78; /* zlib header: deflate, 32 K window, fastest */
    *o++ = 0x01;
    for (size_t pos = 0; pos < raw_len;) {
        const size_t n = raw_len - pos < 65535 ? raw_len - pos : 65535;
        *o++ = pos + n == raw_len; /* BFINAL, BTYPE 00 = stored */
        *o++ = (uint8_t)n;
        *o++ = (uint8_t)(n >> 8);
        *o++ = (uint8_t)~n;
        *o++ = (uint8_t)(~n >> 8);
        memcpy(o, raw + pos, n);
        o += n;
        pos += n;
    }
    png_put32(o, png_adler32(raw, raw_len));
    o += 4;
    png_put32(o, png_crc32(0, idat + 4, zlib_len + 4));
    o += 4;
    /* IEND */
    png_put32(o, 0);
    memcpy(o + 4, "IEND", 4);
    png_put32(o + 8, png_crc32(0, o + 4, 4));
    o += 12;
    free(raw);
    *len = (size_t)(o - out);
    return out;
}

#endif
