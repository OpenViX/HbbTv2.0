/*
 * wpe_fb - WPE Platform API embedder that shows live web frames on a Linux framebuffer (HbbTV 2.0 experiment).
 *
 * It provides its own WPE platform (WPEDisplay / WPEToplevel / WPEView subclasses). The view's render_buffer()
 * receives every frame the web process produces, together with its damage rectangles; only those rectangles are
 * copied to a shadow canvas and, optionally, to the visible framebuffer page (the compositor's slow read-back
 * only ever produces the damaged regions, see the WebKit patches).
 *
 *   wpe_fb [options] <page.html | http(s)://url | file:///path>
 *     --size WxH        web view size (default 1280x720)
 *     --secs N          run time in seconds (default 10)
 *     --fb              draw to /dev/fb0's visible page (saves and restores the pixels it overwrites)
 *     --egl-sink        hand the frames to enigma2's own EGL renderer (gEGLDC, lib/gdi/egl/gweb_overlay.*) through shared memory
 *                       instead of drawing them: the view is rendered at its own size, gEGLDC scales it to the whole OSD canvas on the GPU and
 *                       blends it over the UI (premultiplied alpha, so a transparent page shows the live picture). No second Nexus window, no CPU
 *                       conversion/scaling, no upload by this process. --egl-screen WxH (default 1920x1080) is the OSD canvas size, used to place
 *                       the hardware video hole. Base path /tmp/e2_webov (.shm frame, .sock wake-up), WPE_EGL_PATH overrides.
 *     --pos X,Y         where the frame goes on the framebuffer (default: centred)
 *     --unpremul        convert premultiplied alpha to straight alpha when writing to the framebuffer
 *                       (the default with --fb: the Dreambox display blends with straight alpha)
 *     --premul          with --fb, write the premultiplied pixels as delivered (for experiments)
 *     --opaque          ignore the frame's alpha (write 0xff)
 *     --dump file.ppm   write the shadow canvas (RGB, alpha ignored) when done
 *     --scale F         show the view scaled by F (bilinear); --fullscreen (with --fb) scales it to fill the screen. HbbTV
 *                       applications are laid out at 1280x720 and shown scaled to the whole screen
 *     --dump-out f.ppm  with --scale: write the scaled output as it goes to the framebuffer
 *     --hidpi F         device scale factor: the page is laid out at --size but rendered F times larger (sharp text; 1.5 gives
 *                       1920x1080 for a 1280x720 page, so --fullscreen then needs no scaling at all). Costs read-back time.
 *     --fine-damage     turn off WebKit's UnifyDamagedRegions feature (several damage rects per frame)
 *     --transparent     transparent page background
 *     --orb DIR         host the ORB HbbTV engine: DIR holds hbbtv.js (polyfill) and ext/libORBWebExt.so; ctl "app <url>" launches an app
 *     --hwvideo         <video> plays on the hardware video plane (dvbvideosink hole-punch, needs the WebKit Dreambox quirks)
 *     --hwvideo-lazy    like --hwvideo, but the live service keeps playing until media needs the decoder: the web process
 *                       asks for it over a socket (see "decoder handover"), ctl "decoder-free" answers
 *     --evt PATH        FIFO on which events for the enigma2 plugin are written (with --hwvideo-lazy): "acquire"/"release" (decoder handover),
 *                       "loaded" (the first page has finished loading), "keyset <mask>" (the keys the application wants, see orb_host_key_set_mask),
 *                       "select audio|subtitle <pid>|default|none" (the application chose an audio track / subtitle),
 *                       "overlay ..." (with --overlay-png, see there)
 *     --trace           timestamped "T ..." lines on stderr for keys, frames (damage, blit time) and acks (see TRACE)
 *     --autoplay deny   media never starts by itself (measurements: no software decoding of previews)
 *     --overlay-png P   (with --fb, for the geometry) the frame is not written to the framebuffer: the bounding box of its visible pixels is saved as
 *                       P_<n>.png (at most ten a second) and the event "overlay <n> <x> <y> <w> <h> <file>" (screen coordinates; w = 0: nothing is
 *                       visible) tells the plugin, which shows it in a skin screen on top of enigma2's own screen. See wpe_fb_png.h
 *     --scrollbars      (with --orb) keep WebKit's scrollbars (they are hidden by default: a TV shows none)
 *     --fast-anim       (with --orb) CSS transitions/animations last 1 ms and smooth scrolling is off: fewer frames per step
 *     --persist DIR     keep the HTTP cache (<= 75 MB), cookies and local storage of the applications in DIR between runs
 *     --no-debug-js     do not inject DIR/debug.js (the page's console/media events in the log)
 *     --limit-repeat    pass on the remote's auto-repeat ("keyrepeat" commands) at most once per delivered frame (feel, not speed)
 *     --info            print the framebuffer parameters and exit
 *
 * Control FIFO (--ctl): channel <onid> <tsid> <sid> <name> (the service enigma2 is tuned to: "app" then starts a broadcast-related
 * application of it), service <onid> <tsid> <sid> + ait <pid> <sid> <hex> (the real AIT: the service was selected, one raw AIT section of it;
 * the engine then runs the AUTOSTART application, resolves "app dvb://current.ait/<org>.<app>"), ait-stop (the broadcast stopped),
 * components <V:pid:enc:crypt:aspect;A:pid:lang:enc:channels:active:ad;S:pid:lang:enc:active:hi> (its video, audio and
 * subtitle streams, see orb_host.h; sent again whenever they change), key <name>, keyrepeat <name> (auto-repeat; with --limit-repeat dropped while the page is still answering the previous key),
 * keydown <name>, keyup <name>, load <url>, app <url>, decoder-free, mark <text> (trace marker),
 * dump <file.ppm> (the canvas as it is now), quit.
 */
#define _GNU_SOURCE
#include "wpe_fb_png.h"
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <glib-unix.h>
#include <glib.h>
#include <linux/fb.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <wpe/webkit.h>

#include "orb_host.h"

/* --trace: one line per event on stderr, "T <monotonic microseconds> <what>": keys in/out, frames delivered by WebKit (damage, blit time),
 * acknowledgements, and marks put in by the harness (ctl "mark <text>"). tools/box/nav_analyze.py turns them into latencies. */
static gboolean trace;
static gboolean autoplay_deny; /* --autoplay deny */
static gboolean fast_anim; /* --fast-anim */
static gboolean tolerant; /* --tolerant: see on_load_failed_tolerant() / on_web_process_terminated() */
static gboolean show_scrollbars; /* --scrollbars */
static const char *persist_dir; /* --persist DIR: HTTP cache, cookies, local storage survive between runs */
static gboolean no_debug_js; /* --no-debug-js */
/* --limit-repeat: the remote's auto-repeat ("keyrepeat <key>") is passed on at most once per delivered frame, so a held key moves as
 * many steps as the screen can show. MEASURED (docs/phase1-results.txt): this does NOT shorten the time the page keeps moving after
 * the key is released and does not reduce the frames or pixels (overlapping step animations cost about as much as spaced ones); it only
 * makes a held key move fewer steps (3 instead of 10 in 1.3 s). So it is a matter of feel, off by default. Without the option a
 * "keyrepeat" is an ordinary "key". A key that produced no frame within REPEAT_HOLD_US (nothing changed, e.g. at the end of a list)
 * does not hold back the next repeat. */
#define REPEAT_HOLD_US 1200000
static gboolean limit_repeat; /* --limit-repeat */
static gboolean key_awaits_frame; /* a key was delivered to the application and no frame has arrived since */
static gint64 key_sent_us;
static int egl_key_fast_ms = 400; /* WPE_EGL_KEY_MS: frames this soon after a key press are the page's answer to it: shown at once (--egl-sink settling is for page loads) */
static gint64 key_pressed_us;
static guint64 repeats_forwarded, repeats_dropped;
#define TRACE(...) do { if (trace) { fprintf(stderr, "T %lld ", (long long)g_get_monotonic_time()); fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

/* ------------------------------------------------------------------------------------------------ output ---- */

typedef struct {
    int w, h; /* the web view */
    guint8 *canvas; /* BGRA, premultiplied, tightly packed, w x h */
    /* Output: the view scaled by `scale` (HbbTV applications are laid out at 1280x720 and shown scaled to the whole screen).
     * conv holds the output in framebuffer format (ow x oh) whenever there is a framebuffer or a scaled dump. */
    double scale;
    int ow, oh;
    int *xi0, *xi1, *yi0, *yi1; /* bilinear sampling: the two source columns/rows of every output column/row ... */
    guint16 *xwt, *ywt; /* ... and the weight (0..256) of the second one */

    gboolean fb_on, nxpl_on, unpremul, opaque;
    gboolean egl_on; /* --egl-sink: the canvas lives in the shared frame handed to gEGLDC */
    int unpremul_from_y; /* -1 = off */
    int fb_fd;
    guint8 *fb_map;
    size_t fb_len;
    struct fb_var_screeninfo vinfo;
    struct fb_fix_screeninfo finfo;
    int fb_x, fb_y;
    guint8 *fb_saved;
    int fb_saved_yoffset;
    int sx, sy, sw, sh; /* clipped region saved */

    /* guard mode: the display owner (enigma2) redraws and flips pages under us, which wipes our frame; keep a
     * converted copy of the frame in framebuffer format and repaint it whenever a flip or overwrite is seen */
    gboolean guard;
    /* --overlay-png: the frame goes to PNG files (see png_flush) instead of the framebuffer */
    char *png_prefix;
    guint png_timer;
    gint64 png_last_us;
    guint64 png_seq;
    gboolean png_empty; /* the last thing sent was "nothing visible" */
    guint8 *png_last; /* the PNG last written (malloc'd) and where it goes: an identical one is not sent again */
    size_t png_last_len;
    int png_last_box[4];
    long png_area; /* pixels of the last picture: the bigger, the longer until the next */
    guint32 *conv;
    int last_page;
    unsigned painted; /* bit mask of the pages we have painted */
    guint64 flips, repaints, overwrites;
    double max_repaint_ms;

    guint64 frames, pixels;
    double blit_ms;
    gint64 first_us, last_us;
} Out;

static Out out;
static GMainLoop *loop;
static WebKitWebView *view;

static void fb_read_var(void)
{
    if (ioctl(out.fb_fd, FBIOGET_VSCREENINFO, &out.vinfo) < 0)
        perror("FBIOGET_VSCREENINFO");
}

static void fb_print_info(void)
{
    fprintf(stderr, "fb: %ux%u virtual %ux%u, %u bpp, line_length %u, yoffset %u\n", out.vinfo.xres, out.vinfo.yres, out.vinfo.xres_virtual,
        out.vinfo.yres_virtual, out.vinfo.bits_per_pixel, out.finfo.line_length, out.vinfo.yoffset);
    fprintf(stderr, "fb: red %u/%u green %u/%u blue %u/%u alpha %u/%u (offset/length), smem_len %u\n", out.vinfo.red.offset, out.vinfo.red.length,
        out.vinfo.green.offset, out.vinfo.green.length, out.vinfo.blue.offset, out.vinfo.blue.length, out.vinfo.transp.offset,
        out.vinfo.transp.length, out.finfo.smem_len);
}

static gboolean fb_open(void)
{
    out.fb_fd = open("/dev/fb0", O_RDWR);
    if (out.fb_fd < 0) {
        perror("open /dev/fb0");
        return FALSE;
    }
    if (ioctl(out.fb_fd, FBIOGET_FSCREENINFO, &out.finfo) < 0) {
        perror("FBIOGET_FSCREENINFO");
        return FALSE;
    }
    fb_read_var();
    fb_print_info();
    if (out.vinfo.bits_per_pixel != 32) {
        fprintf(stderr, "fb: only 32 bpp is supported\n");
        return FALSE;
    }
    out.fb_len = out.finfo.smem_len;
    out.fb_map = mmap(NULL, out.fb_len, PROT_READ | PROT_WRITE, MAP_SHARED, out.fb_fd, 0);
    if (out.fb_map == MAP_FAILED) {
        perror("mmap fb0");
        out.fb_map = NULL;
        return FALSE;
    }
    return TRUE;
}

/* Convert n BGRA (premultiplied) pixels to the framebuffer's pixel layout. */
static void convert_pixels(guint32 *dst, const guint8 *src, int n)
{
    const struct fb_var_screeninfo *v = &out.vinfo;
    const gboolean native_bgra = v->red.offset == 16 && v->green.offset == 8 && v->blue.offset == 0 && v->transp.offset == 24;
    if (native_bgra && !out.unpremul && !out.opaque) {
        memcpy(dst, src, (size_t)n * 4);
        return;
    }
    /* The Dreambox display blends the OSD with STRAIGHT alpha (verified on screen), while WPE delivers
     * premultiplied pixels: colour = premultiplied * 255 / alpha, via a 16.16 reciprocal table. */
    static guint32 recip[256];
    if (!recip[1]) {
        for (int i = 1; i < 256; i++)
            recip[i] = (guint32)((255u * 65536u + i / 2) / i);
    }
    for (int i = 0; i < n; i++, src += 4) {
        unsigned b = src[0], g = src[1], r = src[2], a = src[3];
        if (out.unpremul && a && a < 255) {
            const guint32 k = recip[a];
            r = (r * k + 32768) >> 16; if (r > 255) r = 255;
            g = (g * k + 32768) >> 16; if (g > 255) g = 255;
            b = (b * k + 32768) >> 16; if (b > 255) b = 255;
        }
        if (out.opaque)
            a = 255;
        dst[i] = ((guint32)r << v->red.offset) | ((guint32)g << v->green.offset) | ((guint32)b << v->blue.offset) | ((guint32)a << v->transp.offset);
    }
}

static void fb_save_area(void)
{
    fb_read_var();
    out.fb_saved_yoffset = out.vinfo.yoffset;
    out.sx = MAX(0, out.fb_x);
    out.sy = MAX(0, out.fb_y);
    out.sw = MIN(out.fb_x + out.ow, (int)out.vinfo.xres) - out.sx;
    out.sh = MIN(out.fb_y + out.oh, (int)out.vinfo.yres) - out.sy;
    if (out.sw <= 0 || out.sh <= 0) {
        out.sw = out.sh = 0;
        return;
    }
    if (out.png_prefix)
        return; /* the framebuffer is not touched (unless the application grows too big, png_promote: then the area is cleared at the end, not restored) */
    out.fb_saved = g_malloc((size_t)out.sw * out.sh * 4);
    for (int row = 0; row < out.sh; row++) {
        const guint8 *src = out.fb_map + (size_t)(out.fb_saved_yoffset + out.sy + row) * out.finfo.line_length + (size_t)out.sx * 4;
        memcpy(out.fb_saved + (size_t)row * out.sw * 4, src, (size_t)out.sw * 4);
    }
    fprintf(stderr, "fb: saved %dx%d pixels at (%d,%d) of page yoffset %d\n", out.sw, out.sh, out.sx, out.sy, out.fb_saved_yoffset);
}

/* --overlay-png (see wpe_fb_png.h): when the frame has changed, the bounding box of its visible pixels is saved as <prefix>_<n>.png and the plugin is told
 * on the event FIFO: "overlay <n> <x> <y> <w> <h> <file>" (screen coordinates), or "overlay <n> 0 0 0 0 -" when nothing is visible. enigma2 decodes the file
 * on its main thread, so at most PNG_INTERVAL_MS between two. The last PNG_KEEP files stay (the plugin may be a little behind), older ones are deleted. */
#define PNG_INTERVAL_MS 100 /* plus one millisecond per PNG_PIXELS_PER_MS pixels of the last picture: enigma2 makes a texture of each */
#define PNG_PIXELS_PER_MS 2500
#define PNG_KEEP 4
#define PNG_BIG_PERCENT 40 /* a picture of more than this share of the screen is no banner: png_promote() */

static void png_promote(void);

static void evt_send(const char *line);

static void png_unlink(guint64 seq)
{
    char *path = g_strdup_printf("%s_%llu.png", out.png_prefix, (unsigned long long)seq);
    unlink(path);
    g_free(path);
}

static gboolean png_flush(gpointer data)
{
    out.png_timer = 0;
    out.png_last_us = g_get_monotonic_time();
    int x0 = out.ow, y0 = out.oh, x1 = -1, y1 = -1;
    for (int y = 0; y < out.oh; y++) {
        const guint32 *row = out.conv + (size_t)y * out.ow;
        int first = 0;
        while (first < out.ow && !(row[first] >> 24))
            first++;
        if (first == out.ow)
            continue;
        int last = out.ow - 1;
        while (!(row[last] >> 24))
            last--;
        x0 = MIN(x0, first);
        x1 = MAX(x1, last);
        y0 = MIN(y0, y);
        y1 = y;
    }
    char line[600];
    if (x1 < 0) {
        if (out.png_empty)
            return G_SOURCE_REMOVE; /* still nothing to see */
        out.png_empty = TRUE;
        out.png_area = 0;
        free(out.png_last);
        out.png_last = NULL;
        snprintf(line, sizeof line, "overlay %llu 0 0 0 0 -\n", (unsigned long long)++out.png_seq);
    } else {
        const int w = x1 - x0 + 1, h = y1 - y0 + 1;
        out.png_area = (long)w * h;
        if ((double)w * h * 100 > (double)out.ow * out.oh * PNG_BIG_PERCENT) { /* at once: enigma2 must not make a texture of it */
            png_promote();
            return G_SOURCE_REMOVE;
        }
        size_t len;
        guint8 *png = png_encode_argb(out.conv + (size_t)y0 * out.ow + x0, out.ow, w, h, &len);
        if (png && out.png_last && len == out.png_last_len && out.png_last_box[0] == x0 && out.png_last_box[1] == y0 && out.png_last_box[2] == w &&
            out.png_last_box[3] == h && !memcmp(png, out.png_last, len)) {
            free(png); /* the same picture in the same place: the frame changed somewhere nobody can see */
            return G_SOURCE_REMOVE;
        }
        char *path = g_strdup_printf("%s_%llu.png", out.png_prefix, (unsigned long long)(out.png_seq + 1));
        GError *error = NULL;
        if (!png || !g_file_set_contents(path, (const gchar *)png, (gssize)len, &error)) { /* written to a temporary file and renamed: never half a file */
            fprintf(stderr, "overlay: cannot write %s: %s\n", path, error ? error->message : "out of memory");
            g_clear_error(&error);
            free(png);
            g_free(path);
            return G_SOURCE_REMOVE;
        }
        free(out.png_last);
        out.png_last = png; /* kept for the comparison with the next */
        out.png_last_len = len;
        out.png_last_box[0] = x0;
        out.png_last_box[1] = y0;
        out.png_last_box[2] = w;
        out.png_last_box[3] = h;
        out.png_empty = FALSE;
        snprintf(line, sizeof line, "overlay %llu %d %d %d %d %s\n", (unsigned long long)++out.png_seq, out.fb_x + x0, out.fb_y + y0, w, h, path);
        g_free(path);
    }
    if (out.png_seq > PNG_KEEP)
        png_unlink(out.png_seq - PNG_KEEP);
    evt_send(line);
    return G_SOURCE_REMOVE;
}

/* The frame (out.conv) has changed. */
static void png_dirty(void)
{
    if (out.png_timer)
        return; /* a save is already due: it takes the newest state */
    const gint64 since = (g_get_monotonic_time() - out.png_last_us) / 1000, interval = PNG_INTERVAL_MS + out.png_area / PNG_PIXELS_PER_MS;
    out.png_timer = g_timeout_add(since >= interval ? 20 : (guint)(interval - since), png_flush, NULL); /* 20 ms: the rects of one frame come together */
}

static void png_cleanup(void)
{
    if (out.png_timer)
        g_source_remove(out.png_timer);
    out.png_timer = 0;
    for (guint64 seq = out.png_seq > PNG_KEEP ? out.png_seq - PNG_KEEP : 1; seq <= out.png_seq; seq++)
        png_unlink(seq);
}

/* Copy a rect of the converted frame (out.conv) to the framebuffer page starting at row `yoffset`. */
static void fb_write_rect(int yoffset, int rx, int ry, int rw, int rh)
{
    if (out.png_prefix) {
        png_dirty();
        return;
    }
    if (out.vinfo.yres)
        out.painted |= 1u << (yoffset / out.vinfo.yres);
    for (int row = 0; row < rh; row++) {
        int fy = out.fb_y + ry + row;
        int fx = out.fb_x + rx;
        int n = rw;
        if (fy < 0 || fy >= (int)out.vinfo.yres || fx >= (int)out.vinfo.xres)
            continue;
        if (fx < 0) { n += fx; fx = 0; }
        if (fx + n > (int)out.vinfo.xres)
            n = out.vinfo.xres - fx;
        if (n <= 0)
            continue;
        memcpy(out.fb_map + (size_t)(yoffset + fy) * out.finfo.line_length + (size_t)fx * 4, out.conv + (size_t)(ry + row) * out.ow + (fx - out.fb_x),
            (size_t)n * 4);
    }
}

/* How many of 96 sample points on the page starting at row `yoffset` differ from our frame. */
static int fb_mismatches(int yoffset)
{
    int bad = 0;
    for (int k = 0; k < 96; k++) {
        int x = (k * 211 + 17) % out.ow, y = (k * 97 + 5) % out.oh;
        int fx = out.fb_x + x, fy = out.fb_y + y;
        if (fx < 0 || fx >= (int)out.vinfo.xres || fy < 0 || fy >= (int)out.vinfo.yres)
            continue;
        const guint32 v = *(const guint32 *)(out.fb_map + (size_t)(yoffset + fy) * out.finfo.line_length + (size_t)fx * 4);
        if (v != out.conv[(size_t)y * out.ow + x])
            bad++;
    }
    return bad;
}

/* Repaint the whole frame on the currently visible page; returns the time it took in ms. */
static double fb_paint_all(void)
{
    gint64 t0 = g_get_monotonic_time();
    fb_read_var();
    fb_write_rect(out.vinfo.yoffset, 0, 0, out.ow, out.oh);
    return (g_get_monotonic_time() - t0) / 1000.0;
}

/* Guard: poll for page flips and overwrites by the display owner and repair the frame. */
static void guard_check(void)
{
    if (!out.fb_on || !out.guard || !out.frames)
        return; /* nothing painted yet */
    fb_read_var();
    const int page = out.vinfo.yoffset / out.vinfo.yres;
    const gboolean flipped = page != out.last_page;
    if (flipped) {
        out.flips++;
        out.last_page = page;
    }
    if (flipped || fb_mismatches(out.vinfo.yoffset) > 2) {
        double ms = fb_paint_all();
        out.repaints++;
        if (!flipped)
            out.overwrites++;
        if (ms > out.max_repaint_ms)
            out.max_repaint_ms = ms;
    }
}

static gboolean guard_tick(gpointer data)
{
    guard_check();
    return G_SOURCE_CONTINUE;
}

/* ------------------------------------------------------------- gEGLDC shared-memory sink (--egl-sink) ---- */

/* Must match gWebOverlayShm in enigma2 (lib/gdi/egl/gweb_overlay.h). */
typedef struct {
    guint32 magic, version, width, height, stride, generation, visible;
    gint32 dst_x, dst_y, dst_w, dst_h;
    guint32 alpha, lock, dirty;
    gint32 dirty_y0, dirty_y1;
    guint32 seq, pid;
    gint32 hole_x, hole_y, hole_w, hole_h; /* frame pixels; hole_w <= 0: no hole. gEGLDC leaves this rectangle out, so what is behind shows */
} WebOverlayShm;

#define WOV_MAGIC 0x31564f57u /* "WOV1" */
#define WOV_VERSION 1u
#define WOV_HEADER 4096u

static void plane_restore(void);
static gboolean plane_moved; /* the video plane was moved for a video/broadcast object and has to be put back */

static WebOverlayShm *egl_shm;
static size_t egl_len;
static char *egl_shm_path;
static ino_t egl_ino; /* inode of our frame file: a newer wpe_fb may have renamed its own over the path by the time we exit */
static struct sockaddr_un egl_peer;
static int egl_sock = -1;
static int egl_screen_w = 1920, egl_screen_h = 1080;
static gboolean egl_notify_pending;
static int egl_resync_ms = 0; /* WPE_EGL_RESYNC_MS, see resync_note(); off by default: it re-reads a released buffer, which can hold older pixels than the frame (a flash back after an animation) */
static int egl_dip_ms = 150; /* WPE_EGL_DIP_MS, see dip_copy() */
static void dip_alloc(void);
static void dip_free(void);
static gboolean egl_no_trim; /* WPE_EGL_NO_TRIM=1, see out_blit_rect() */
static int egl_hold_ms = 0; /* WPE_EGL_HOLD_MS, see held_take(); off by default: it delays solid areas, which shows as a late flash after an animation */
static gboolean egl_hold_clear; /* WPE_EGL_HOLD_CLEAR */
static int egl_settle_ms = 60, egl_max_delay_ms = 250; /* see egl_mark_dirty() */

/* The frame lives in a fresh file that is renamed over the old one: gEGLDC keeps the previous mapping valid (and notices the new inode),
 * and never finds a truncated file under its feet. The magic is written last. */
static gboolean egl_open(void)
{
    const char *base = g_getenv("WPE_EGL_PATH");
    if (!base || !*base)
        base = "/tmp/e2_webov";
    egl_shm_path = g_strdup_printf("%s.shm", base);
    char *tmp = g_strdup_printf("%s.shm.%d", base, (int)getpid());
    egl_len = WOV_HEADER + (size_t)out.w * out.h * 4;
    int fd = open(tmp, O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0 || ftruncate(fd, (off_t)egl_len) < 0) {
        fprintf(stderr, "egl: cannot create %s: %s\n", tmp, g_strerror(errno));
        if (fd >= 0)
            close(fd);
        g_free(tmp);
        return FALSE;
    }
    void *p = mmap(NULL, egl_len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) {
        fprintf(stderr, "egl: mmap: %s\n", g_strerror(errno));
        unlink(tmp);
        g_free(tmp);
        return FALSE;
    }
    egl_shm = p;
    if (g_getenv("WPE_EGL_SETTLE_MS"))
        egl_settle_ms = atoi(g_getenv("WPE_EGL_SETTLE_MS"));
    if (g_getenv("WPE_EGL_DIP_MS"))
        egl_dip_ms = atoi(g_getenv("WPE_EGL_DIP_MS"));
    egl_no_trim = g_getenv("WPE_EGL_NO_TRIM") && atoi(g_getenv("WPE_EGL_NO_TRIM")) == 1;
    if (g_getenv("WPE_EGL_HOLD_MS"))
        egl_hold_ms = atoi(g_getenv("WPE_EGL_HOLD_MS"));
    egl_hold_clear = g_getenv("WPE_EGL_HOLD_CLEAR") && atoi(g_getenv("WPE_EGL_HOLD_CLEAR")) == 1;
    if (g_getenv("WPE_EGL_RESYNC_MS"))
        egl_resync_ms = atoi(g_getenv("WPE_EGL_RESYNC_MS"));
    if (g_getenv("WPE_EGL_KEY_MS"))
        egl_key_fast_ms = atoi(g_getenv("WPE_EGL_KEY_MS"));
    if (g_getenv("WPE_EGL_MAX_DELAY_MS"))
        egl_max_delay_ms = MAX(1, atoi(g_getenv("WPE_EGL_MAX_DELAY_MS")));
    memset(egl_shm, 0, WOV_HEADER);
    dip_alloc();
    egl_shm->version = WOV_VERSION;
    egl_shm->width = out.w;
    egl_shm->height = out.h;
    egl_shm->stride = out.w * 4;
    egl_shm->generation = 1;
    egl_shm->alpha = 255;
    egl_shm->pid = (guint32)getpid();
    __atomic_store_n(&egl_shm->magic, WOV_MAGIC, __ATOMIC_RELEASE);
    if (rename(tmp, egl_shm_path) < 0) {
        fprintf(stderr, "egl: rename to %s: %s\n", egl_shm_path, g_strerror(errno));
        unlink(tmp);
        g_free(tmp);
        return FALSE;
    }
    g_free(tmp);
    {
        struct stat st;
        egl_ino = stat(egl_shm_path, &st) == 0 ? st.st_ino : 0;
    }
    out.canvas = (guint8 *)p + WOV_HEADER; /* WPE's frame is copied straight into the shared memory */

    egl_sock = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    memset(&egl_peer, 0, sizeof egl_peer);
    egl_peer.sun_family = AF_UNIX;
    snprintf(egl_peer.sun_path, sizeof egl_peer.sun_path, "%s.sock", base);
    fprintf(stderr, "egl: frame %dx%d in %s (%zu bytes), wake-up socket %s%s\n", out.w, out.h, egl_shm_path, egl_len, egl_peer.sun_path,
        access(egl_peer.sun_path, F_OK) ? " (not there: enigma2 is not running gEGLDC with the web layer - nothing will be shown)" : "");
    return TRUE;
}

static void egl_notify(void)
{
    egl_notify_pending = FALSE;
    if (egl_sock >= 0)
        sendto(egl_sock, "F", 1, MSG_DONTWAIT, (struct sockaddr *)&egl_peer, sizeof egl_peer); /* a full queue means enigma2 has a wake-up pending anyway */
}

static void egl_lock(void)
{
    while (__atomic_exchange_n(&egl_shm->lock, 1u, __ATOMIC_ACQUIRE))
        g_usleep(50);
}

static void egl_unlock(void)
{
    __atomic_store_n(&egl_shm->lock, 0u, __ATOMIC_RELEASE);
}

/* Rows [y0, y1) of the canvas were rewritten. They are not shown at once: WebKit paints in tiles, and the frames it delivers while a page is
 * still being painted (new tiles, images arriving) show blank parts. The rows are collected here and published to enigma2 only when no
 * further frame has come for WPE_EGL_SETTLE_MS (default 60) or the first of them is WPE_EGL_MAX_DELAY_MS (default 250) old - so the
 * settled picture is shown, not every step on the way. WPE_EGL_SETTLE_MS=0 publishes every frame immediately. */
static int pend_y0, pend_y1;
static gboolean pend_have;
static guint pend_timer;
static gint64 pend_first_us;

static void egl_mark_dirty(int y0, int y1)
{
    if (!pend_have || y0 < pend_y0)
        pend_y0 = y0;
    if (!pend_have || y1 > pend_y1)
        pend_y1 = y1;
    if (!pend_have)
        pend_first_us = g_get_monotonic_time();
    pend_have = TRUE;
}

/* the pixels were copied before the rows are marked, so a reader that sees the mark sees the pixels */
static void egl_publish(void)
{
    if (!pend_have)
        return;
    egl_lock();
    if (!egl_shm->dirty || pend_y0 < egl_shm->dirty_y0)
        egl_shm->dirty_y0 = pend_y0;
    if (!egl_shm->dirty || pend_y1 > egl_shm->dirty_y1)
        egl_shm->dirty_y1 = pend_y1;
    egl_shm->dirty = 1;
    egl_shm->seq++;
    egl_unlock();
    pend_have = FALSE;
    if (!egl_shm->visible)
        __atomic_store_n(&egl_shm->visible, 1u, __ATOMIC_RELEASE); /* from the first frame on */
    egl_notify();
}

static gboolean egl_publish_cb(gpointer data)
{
    (void)data;
    pend_timer = 0;
    egl_publish();
    return G_SOURCE_REMOVE;
}

/* end of one WebKit frame (all its damage rects are marked): publish now, or wait for the stream to settle */
static void egl_frame_done(void)
{
    if (!pend_have)
        return;
    if (egl_settle_ms <= 0 || (key_pressed_us && g_get_monotonic_time() - key_pressed_us < (gint64)egl_key_fast_ms * 1000)) {
        egl_publish();
        return;
    }
    if (pend_timer) {
        g_source_remove(pend_timer);
        pend_timer = 0;
    }
    const gint64 waited = (g_get_monotonic_time() - pend_first_us) / 1000;
    if (waited >= egl_max_delay_ms) {
        egl_publish();
        return;
    }
    const gint64 wait = MIN((gint64)egl_settle_ms, egl_max_delay_ms - waited);
    pend_timer = g_timeout_add((guint)wait, egl_publish_cb, NULL);
}

/* Cut a transparent hole into the layer (frame pixels), or remove it (w <= 0). The picture behind - the live service's video plane - shows
 * through it. gEGLDC draws the frame around the rectangle, so whatever the page painted there (an opaque black box) does not matter. */
static void egl_set_hole(int x, int y, int w, int h)
{
    if (!egl_shm)
        return;
    egl_lock();
    egl_shm->hole_x = x;
    egl_shm->hole_y = y;
    egl_shm->hole_w = w;
    egl_shm->hole_h = h;
    egl_shm->seq++;
    egl_unlock();
    egl_notify();
}

/* Unpainted tiles. WebKit paints tiles asynchronously and has no "frame complete" signal: while a slow page is still painting, the buffers it
 * delivers contain damage rects that are one solid colour - opaque black (0xFF000000) or fully transparent (0) - because the tile has not been
 * painted yet. Such a rect is not copied into the shared frame (enigma2 keeps showing what was there); a copy of it is held for
 * WPE_EGL_HOLD_MS (default 0 = off, try 400) and any later damage rect that touches it replaces it, since that is the real paint. Only if nothing
 * repaints the area in that time is the solid rect real content, and it is published after all. WPE_EGL_HOLD_MS=0 turns this off.
 * Small rects are never held (a button highlight, a caret). Transparent rects are held only with WPE_EGL_HOLD_CLEAR=1 (a page that clears
 * an area to show the picture behind it would otherwise lose that for the hold time). */
#define HOLD_MAX 48
#define HOLD_MIN_PIXELS 4096
typedef struct {
    int x, y, w, h;
    guint8 *px; /* w*h*4, tightly packed */
    gint64 due_us;
} HeldRect;
static HeldRect held[HOLD_MAX];
static int nheld;
static guint held_timer;

static gboolean held_flush_cb(gpointer data);

static void held_arm(void)
{
    if (held_timer) {
        g_source_remove(held_timer);
        held_timer = 0;
    }
    if (!nheld)
        return;
    gint64 first = held[0].due_us;
    for (int i = 1; i < nheld; i++)
        first = MIN(first, held[i].due_us);
    gint64 ms = (first - g_get_monotonic_time() + 999) / 1000;
    held_timer = g_timeout_add((guint)MAX(ms, 1), held_flush_cb, NULL);
}

static void held_drop(int i)
{
    g_free(held[i].px);
    held[i] = held[--nheld];
}

/* a damage rect (clipped, frame pixels) arrived: whatever it touches that is held was not the final paint */
static void held_supersede(int x, int y, int w, int h)
{
    for (int i = nheld - 1; i >= 0; i--)
        if (x < held[i].x + held[i].w && x + w > held[i].x && y < held[i].y + held[i].h && y + h > held[i].y)
            held_drop(i);
}

static gboolean held_flush_cb(gpointer data)
{
    (void)data;
    held_timer = 0;
    const gint64 now = g_get_monotonic_time();
    gboolean any = FALSE;
    for (int i = nheld - 1; i >= 0; i--) {
        if (held[i].due_us > now)
            continue;
        for (int row = 0; row < held[i].h; row++)
            memcpy(out.canvas + ((size_t)(held[i].y + row) * out.w + held[i].x) * 4, held[i].px + (size_t)row * held[i].w * 4, (size_t)held[i].w * 4);
        egl_mark_dirty(held[i].y, held[i].y + held[i].h);
        held_drop(i);
        any = TRUE;
    }
    held_arm();
    if (any)
        egl_publish();
    return G_SOURCE_REMOVE;
}

/* TRUE: the rect is an unpainted-looking solid block and was taken into the hold list instead of the frame */
static gboolean held_take(const guint8 *src, int stride, int rx, int ry, int rw, int rh)
{
    if (egl_hold_ms <= 0 || (gint64)rw * rh < HOLD_MIN_PIXELS || nheld >= HOLD_MAX)
        return FALSE;
    const guint32 first = *(const guint32 *)(src + (size_t)ry * stride + (size_t)rx * 4);
    /* diagnostics (WPE_EGL_DEBUG_RECTS=1): what do the big damage rects look like - solid black, solid transparent, solid other, or mixed? */
    static int dbg = -1, dbg_left = 300;
    if (dbg < 0)
        dbg = g_getenv("WPE_EGL_DEBUG_RECTS") && atoi(g_getenv("WPE_EGL_DEBUG_RECTS")) == 1;
    gboolean solid = TRUE;
    guint32 other = first;
    for (int row = 0; row < rh && solid; row++) {
        const guint32 *p = (const guint32 *)(src + (size_t)(ry + row) * stride + (size_t)rx * 4);
        for (int c = 0; c < rw; c++)
            if (p[c] != first) {
                solid = FALSE;
                other = p[c];
                break;
            }
    }
    if (dbg && dbg_left > 0 && (gint64)rw * rh >= HOLD_MIN_PIXELS && (solid || first == 0xFF000000u || first == 0)) {
        dbg_left--;
        fprintf(stderr, "egl: rect %d,%d %dx%d %s first=%08x%s\n", rx, ry, rw, rh, solid ? "SOLID" : "mixed", first, "");
        (void)other;
    }
    if (!solid || (first != 0xFF000000u && !(first == 0 && egl_hold_clear)))
        return FALSE;
    HeldRect *r = &held[nheld++];
    r->x = rx; r->y = ry; r->w = rw; r->h = rh;
    r->px = g_malloc((size_t)rw * rh * 4);
    for (int row = 0; row < rh; row++)
        memcpy(r->px + (size_t)row * rw * 4, src + (size_t)(ry + row) * stride + (size_t)rx * 4, (size_t)rw * 4);
    r->due_us = g_get_monotonic_time() + (gint64)egl_hold_ms * 1000;
    held_arm();
    return TRUE;
}

/* Resync. The shared frame is built from damage rects only, so anything WebKit paints without reporting it as damage (or reports before the
 * pixels are final) stays wrong in it for ever: a stuck black rectangle while the page redraws around it. When no frame has arrived for
 * WPE_EGL_RESYNC_MS (default 0 = off, try 500) the whole of the latest buffer is compared with the shared frame and every row that differs is
 * corrected and published. The count is logged ("egl: resync ..."): if it never fires while a black rectangle is stuck, the buffer itself
 * is black there too and this is not the cause. */
static GBytes *resync_bytes;
static int resync_stride, resync_w, resync_h;
static guint resync_timer;
static gboolean resync_cb(gpointer data)
{
    (void)data;
    resync_timer = 0;
    if (!resync_bytes || !egl_shm || nheld)
        return G_SOURCE_REMOVE;
    const guint8 *src = g_bytes_get_data(resync_bytes, NULL);
    if (resync_w != out.w || resync_h != out.h)
        return G_SOURCE_REMOVE;
    int rows = 0, y0 = -1, y1 = -1;
    /* which way does it differ? pixels that differ, and how many of them are opaque black (0xFF000000) or fully transparent (0) in the WebKit
     * buffer ("buf") and in the shared frame before the correction ("old"). old black >> buf black: the shared frame had lost a paint (damage
     * rects missed it) and the resync repaired it. buf black >> old black: WebKit's own buffer is black/empty there (a tile that is not painted
     * yet or was lost) and the resync copies that into the shared frame. */
    long diff_px = 0, buf_black = 0, buf_clear = 0, old_black = 0, old_clear = 0;
    for (int y = 0; y < out.h; y++) {
        guint8 *dst = out.canvas + (size_t)y * out.w * 4;
        if (!memcmp(dst, src + (size_t)y * resync_stride, (size_t)out.w * 4))
            continue;
        {
            const guint32 *d32 = (const guint32 *)dst, *s32 = (const guint32 *)(src + (size_t)y * resync_stride);
            for (int x = 0; x < out.w; x++) {
                if (d32[x] == s32[x])
                    continue;
                diff_px++;
                buf_black += s32[x] == 0xFF000000u;
                buf_clear += s32[x] == 0;
                old_black += d32[x] == 0xFF000000u;
                old_clear += d32[x] == 0;
            }
        }
        memcpy(dst, src + (size_t)y * resync_stride, (size_t)out.w * 4);
        if (y0 < 0)
            y0 = y;
        y1 = y + 1;
        rows++;
    }
    if (rows) {
        fprintf(stderr, "egl: resync corrected %d rows (%d..%d): %ld px differ, buf black %ld clear %ld, old black %ld clear %ld\n",
            rows, y0, y1, diff_px, buf_black, buf_clear, old_black, old_clear);
        egl_mark_dirty(y0, y1);
        egl_publish();
    }
    return G_SOURCE_REMOVE;
}

static void resync_note(GBytes *bytes, int stride, int w, int h)
{
    if (egl_resync_ms <= 0 || !egl_shm)
        return;
    if (resync_bytes)
        g_bytes_unref(resync_bytes);
    resync_bytes = g_bytes_ref(bytes);
    resync_stride = stride;
    resync_w = w;
    resync_h = h;
    if (resync_timer)
        g_source_remove(resync_timer);
    resync_timer = g_timeout_add((guint)egl_resync_ms, resync_cb, NULL);
}

/* Dips. When a fade or another animation ends, WebKit often delivers one frame in which the part is empty (fully transparent, or solid black)
 * before the final paint arrives a moment later: the part blinks. The frame is cut into 64x64 blocks; a block that would turn from content into
 * a solid transparent / solid black one is NOT written to the shared frame at once. It is written only if no later frame has painted it within
 * WPE_EGL_DIP_MS (default 150, 0 = off) - so a real clear (the fade-out is over) shows 150 ms late, a one-frame dip never shows. */
#define DIP_BLOCK 64
typedef struct {
    guint32 color;
    gint64 due_us;
    gboolean on;
} DipBlock;
static DipBlock *dip;
static guint8 *dip_now;
static int dip_bx, dip_by;
static guint dip_timer;

static void dip_free(void)
{
    if (dip_timer) {
        g_source_remove(dip_timer);
        dip_timer = 0;
    }
    g_clear_pointer(&dip, g_free);
    g_clear_pointer(&dip_now, g_free);
}

static void dip_alloc(void)
{
    dip_bx = (out.w + DIP_BLOCK - 1) / DIP_BLOCK;
    dip_by = (out.h + DIP_BLOCK - 1) / DIP_BLOCK;
    dip = g_new0(DipBlock, (gsize)dip_bx * dip_by);
    dip_now = g_new0(guint8, (gsize)dip_bx * dip_by);
}

static gboolean dip_flush_cb(gpointer data);

static void dip_arm(void)
{
    if (dip_timer) {
        g_source_remove(dip_timer);
        dip_timer = 0;
    }
    gint64 first = 0;
    for (int i = 0; i < dip_bx * dip_by; i++)
        if (dip[i].on && (!first || dip[i].due_us < first))
            first = dip[i].due_us;
    if (!first)
        return;
    gint64 ms = (first - g_get_monotonic_time() + 999) / 1000;
    dip_timer = g_timeout_add((guint)MAX(ms, 1), dip_flush_cb, NULL);
}

static gboolean dip_flush_cb(gpointer data)
{
    (void)data;
    dip_timer = 0;
    if (!dip || !egl_shm)
        return G_SOURCE_REMOVE;
    const gint64 now = g_get_monotonic_time();
    gboolean any = FALSE;
    for (int by = 0; by < dip_by; by++) {
        for (int bx = 0; bx < dip_bx; bx++) {
            DipBlock *b = &dip[by * dip_bx + bx];
            if (!b->on || b->due_us > now)
                continue;
            const int x = bx * DIP_BLOCK, y = by * DIP_BLOCK, w = MIN(DIP_BLOCK, out.w - x), h = MIN(DIP_BLOCK, out.h - y);
            for (int row = 0; row < h; row++) {
                guint32 *d = (guint32 *)(out.canvas + ((size_t)(y + row) * out.w + x) * 4);
                for (int i = 0; i < w; i++)
                    d[i] = b->color;
            }
            egl_mark_dirty(y, y + h);
            b->on = FALSE;
            any = TRUE;
        }
    }
    dip_arm();
    if (any)
        egl_publish();
    return G_SOURCE_REMOVE;
}

static gboolean dip_src_solid(const guint8 *src, int stride, int x, int y, int w, int h, guint32 *color)
{
    const guint32 first = *(const guint32 *)(src + (size_t)y * stride + (size_t)x * 4);
    if (first != 0 && first != 0xFF000000u)
        return FALSE;
    for (int row = 0; row < h; row++) {
        const guint32 *p = (const guint32 *)(src + (size_t)(y + row) * stride + (size_t)x * 4);
        for (int i = 0; i < w; i++)
            if (p[i] != first)
                return FALSE;
    }
    *color = first;
    return TRUE;
}

static gboolean dip_canvas_is(int x, int y, int w, int h, guint32 color)
{
    for (int row = 0; row < h; row++) {
        const guint32 *p = (const guint32 *)(out.canvas + ((size_t)(y + row) * out.w + x) * 4);
        for (int i = 0; i < w; i++)
            if (p[i] != color)
                return FALSE;
    }
    return TRUE;
}

/* copies one clipped damage rect into the shared frame, leaving out the blocks that look like a dip; TRUE when it did the copy */
static gboolean dip_copy(const guint8 *src, int stride, int rx, int ry, int rw, int rh)
{
    if (egl_dip_ms <= 0 || !dip)
        return FALSE;
    const int bxa = rx / DIP_BLOCK, bxb = (rx + rw - 1) / DIP_BLOCK, bya = ry / DIP_BLOCK, byb = (ry + rh - 1) / DIP_BLOCK;
    const gint64 now = g_get_monotonic_time();
    gboolean any_held = FALSE;
    for (int by = bya; by <= byb; by++) {
        for (int bx = bxa; bx <= bxb; bx++) {
            const int idx = by * dip_bx + bx;
            const int x = bx * DIP_BLOCK, y = by * DIP_BLOCK, w = MIN(DIP_BLOCK, out.w - x), h = MIN(DIP_BLOCK, out.h - y);
            guint32 color;
            dip_now[idx] = 0;
            if (x >= rx && y >= ry && x + w <= rx + rw && y + h <= ry + rh && dip_src_solid(src, stride, x, y, w, h, &color)
                && !dip_canvas_is(x, y, w, h, color)) {
                dip_now[idx] = 1;
                any_held = TRUE;
                if (!dip[idx].on || dip[idx].color != color) {
                    dip[idx].on = TRUE;
                    dip[idx].color = color;
                    dip[idx].due_us = now + (gint64)egl_dip_ms * 1000;
                }
            } else {
                dip[idx].on = FALSE; /* painted for real (or the part is not a dip): nothing pending any more */
            }
        }
    }
    for (int row = 0; row < rh; row++) {
        const int y = ry + row, by = y / DIP_BLOCK;
        int x = rx;
        while (x < rx + rw) {
            int bx = x / DIP_BLOCK, end = MIN(rx + rw, (bx + 1) * DIP_BLOCK);
            if (dip_now[by * dip_bx + bx]) {
                x = end;
                continue;
            }
            /* join the following blocks that are copied too */
            while (end < rx + rw && !dip_now[by * dip_bx + end / DIP_BLOCK])
                end = MIN(rx + rw, (end / DIP_BLOCK + 1) * DIP_BLOCK);
            memcpy(out.canvas + ((size_t)y * out.w + x) * 4, src + (size_t)y * stride + (size_t)x * 4, (size_t)(end - x) * 4);
            x = end;
        }
    }
    if (any_held)
        dip_arm();
    return TRUE;
}

static void egl_close(void)
{
    if (!egl_shm)
        return;
    if (resync_timer) {
        g_source_remove(resync_timer);
        resync_timer = 0;
    }
    g_clear_pointer(&resync_bytes, g_bytes_unref);
    dip_free();
    while (nheld)
        held_drop(nheld - 1);
    if (held_timer) {
        g_source_remove(held_timer);
        held_timer = 0;
    }
    if (plane_moved) {
        plane_restore();
        plane_moved = FALSE;
    }
    __atomic_store_n(&egl_shm->visible, 0u, __ATOMIC_RELEASE);
    egl_notify();
    {
        struct stat st;
        if (stat(egl_shm_path, &st) == 0 && st.st_ino == egl_ino)
            unlink(egl_shm_path); /* not a newer instance's file */
    }
    egl_notify(); /* enigma2 finds the file gone and drops its mapping */
    munmap(egl_shm, egl_len);
    egl_shm = NULL;
    out.canvas = NULL;
    if (egl_sock >= 0)
        close(egl_sock);
    egl_sock = -1;
    fprintf(stderr, "egl: layer withdrawn\n");
}

/* ------------------------------------------------------------------- NXPL/Nexus presentation backend ---- */

/* On a Nexus-middleware Broadcom box (GigaBlue), /dev/fb0 is not connected to the real display scanout at all: Nexus owns the physical display
 * exclusively through its own IPC/compositor (NXPL_RegisterNexusDisplayPlatform's own doc comment in default_nexus.h literally says "Register a
 * display for exclusive use"). Confirmed on real hardware 2026-09-23 with tools/vuplus-probe/fb_paint_probe.c (a raw fb0 write: invisible) and
 * tools/vuplus-probe/nxpl_window_probe.c (an NXPL native window + a real EGL window surface: visible) - see project memory
 * project_hbbtv2_wpe_orb_plan.md for the full trace. This backend reuses the exact sequence nxpl_window_probe.c already proved works: join Nexus,
 * register the EGL platform (patch 0017/r10's own fix, needed here too since this process also calls into EGL), create+show an NXPL native window,
 * get a real EGL window surface against it, and upload each already-composited frame (out.conv - the very same buffer the fb0 path would have
 * memcpy'd) as a GL texture drawn full-screen, presented with eglSwapBuffers. Nexus's own hardware compositor does the actual scanout from there.
 *
 * Pure runtime dlopen/dlsym, no vendor headers linked at build time, no link-time dependency on libnxclient.so/libnxpl.so/libEGL.so/libGLESv2.so -
 * nxpl_try_open() safely returns FALSE (falling back to the existing /dev/fb0 path) on any platform without them, e.g. the DM900. */

typedef void *EGLDisplayNX, *EGLConfigNX, *EGLContextNX, *EGLSurfaceNX, *EGLNativeWindowTypeNX;
typedef int EGLBoolean;

/* The library names to dlopen are not guessed here: the recipe reads the SONAME of the EGL/GLESv2 library the build's own headers belong to (the
 * staged libEGL/libGLESv2, "readelf -d") and passes it as WPE_FB_EGL_LIB / WPE_FB_GLES_LIB. The unversioned and vendor-driver names stay only as
 * fallbacks for boxes whose image ships the GPU driver under another name (GigaBlue: libv3ddriver.so only). */
#ifdef WPE_FB_EGL_LIB
#define NXPL_EGL_FIRST WPE_FB_EGL_LIB,
#else
#define NXPL_EGL_FIRST
#endif
#ifdef WPE_FB_GLES_LIB
#define NXPL_GLES_FIRST WPE_FB_GLES_LIB,
#else
#define NXPL_GLES_FIRST
#endif
static const char *const nxpl_egl_names[] = { NXPL_EGL_FIRST "libEGL.so", "libv3ddriver.so", NULL };
static const char *const nxpl_gles_names[] = { NXPL_GLES_FIRST "libGLESv2.so", "libv3ddriver.so", NULL };

static void *nxpl_dlopen_any(const char *const *names, int flags)
{
    for (int i = 0; names[i]; i++) {
        void *h = dlopen(names[i], flags);
        if (h)
            return h;
    }
    return NULL;
}

static gboolean nxpl_dirty;
/* Full 2D bounding box of everything touched since the last present(), not just a row range. r21/r22's own measurements on a real GigaBlue (project
 * memory project_hbbtv2_wpe_orb_plan.md, 2026-09-23) proved the upload cost is bandwidth-bound, not a fixed per-call cost (min 2.02 ms for a
 * 138240 px update vs. max 52.95 ms for a full 2073600 px one - scales with size) - so shrinking the uploaded byte count is the real lever. Plain
 * GLES2 has no GL_UNPACK_ROW_LENGTH, so a genuine x-sub-range out of out.conv's packed rows needs a CPU-side compaction copy into a scratch buffer
 * first (see nxpl_scratch below) - worth it because CPU memcpy bandwidth is far higher than the ~150-270 MB/s this GPU upload path measured at;
 * skipped when the touched width is already close to full-screen (compacting would just add CPU cost for no real saving). */
static int nxpl_dirty_x0, nxpl_dirty_y0, nxpl_dirty_x1, nxpl_dirty_y1;
static guint32 *nxpl_scratch;
static size_t nxpl_scratch_len;
/* Timing breakdown for nxpl_present(), split at the one place that can plausibly block on Nexus's own client/server IPC (eglSwapBuffers) versus
 * everything before it (a CPU-side glTexSubImage2D upload + a trivial 4-vertex glDrawArrays submission, both just queuing GL commands) - added to
 * answer a concrete question (GigaBlue "about half the speed of dreambox"): is the cost in this new presentation code, or somewhere this code
 * can't reach (the WebProcess's own Nexus-registered Skia GPU compositing, or a hardware clock difference)? See project memory
 * project_hbbtv2_wpe_orb_plan.md, 2026-09-23. */
static guint64 nxpl_present_count;
static double nxpl_upload_ms, nxpl_swap_ms;
/* r21's own result (real GigaBlue capture, 2026-09-23): eglSwapBuffers averaged 1.33 ms, upload+draw averaged 8.94 ms - Nexus IPC/compositor-sync
 * is NOT the bottleneck, the CPU-side texture upload is, and that session averaged uploading ~30% of the screen per frame (heavy scrolling). Two
 * different fixes apply depending on WHY the upload is slow: bandwidth-bound (scales with bytes moved -> reduce bytes, e.g. real 2D sub-rect
 * tracking instead of just row-range) vs. a large fixed per-call cost regardless of size (call-overhead-bound -> reduce how often we upload/swap
 * instead). Min/max upload+draw time (plus the smallest/largest dirty area seen) disambiguates: if even the smallest updates are still slow, that
 * points at fixed overhead; if only large updates are slow, that points at bandwidth. */
static double nxpl_upload_ms_min = -1, nxpl_upload_ms_max;
static long nxpl_upload_px_at_min, nxpl_upload_px_at_max;
/* r23's own result (real GigaBlue capture, 2026-09-23): after adding 2D sub-rect compaction, the average improved slightly (8.25 ms vs ~8.9 ms
 * before) but the MAX got dramatically worse per byte - 48.28 ms for only 67500 px vs r22's 52.95 ms for a full 2073600 px screen, i.e. ~30x
 * fewer bytes taking almost the same time. That is not bandwidth-bound behaviour any more - something about the compacted upload path is
 * pathologically slow for whatever SHAPE that region was (a narrow-but-tall region is the leading suspect: this GPU's tiled texture memory
 * layout may cross many internal tile boundaries on a many-row, few-column update even though the total byte count is small, unlike a wide
 * update that stays within fewer tile rows). Width/height (not just total px) recorded here so the actual problem shape is visible directly in
 * the next capture instead of guessing again. */
static int nxpl_upload_w_at_min, nxpl_upload_h_at_min, nxpl_upload_w_at_max, nxpl_upload_h_at_max;
static void *nxpl_platform_handle;
static void *nxpl_native_window;
static void (*nxpl_ShowNativeWindowEXT)(void *, int);
static void (*nxpl_DestroyNativeWindow)(void *);
static void (*nxpl_UnregisterNexusDisplayPlatform)(void *);
static EGLDisplayNX nxpl_egl_display;
static EGLSurfaceNX nxpl_egl_surface;
static EGLContextNX nxpl_egl_context;
static unsigned nxpl_texture, nxpl_program;
static EGLBoolean (*nxpl_eglSwapBuffers)(EGLDisplayNX, EGLSurfaceNX);
static void (*nxpl_glTexSubImage2D)(unsigned, int, int, int, int, int, unsigned, unsigned, const void *);
static void (*nxpl_glClear)(unsigned);
static void (*nxpl_glDrawArrays)(unsigned, int, int);
static void (*nxpl_glUseProgram)(unsigned);

static const char *nxpl_vs_src =
    "attribute vec2 pos; attribute vec2 texcoord; varying vec2 v_texcoord;"
    "void main() { v_texcoord = texcoord; gl_Position = vec4(pos, 0.0, 1.0); }";
static const char *nxpl_fs_src =
    "precision mediump float; varying vec2 v_texcoord; uniform sampler2D tex;"
    "void main() { gl_FragColor = texture2D(tex, v_texcoord); }";

/* Screen top-left (view row 0) at NDC (-1,+1): texcoord v=0 there, v=1 at the bottom - out.conv's row 0 is the top of the image. */
static const float nxpl_quad[] = {
    /*  x,     y,    u,   v */
    -1.0f,  1.0f, 0.0f, 0.0f,
    -1.0f, -1.0f, 0.0f, 1.0f,
     1.0f,  1.0f, 1.0f, 0.0f,
     1.0f, -1.0f, 1.0f, 1.0f,
};

static gboolean nxpl_try_open(void)
{
    void *egl = nxpl_dlopen_any(nxpl_egl_names, RTLD_NOW | RTLD_GLOBAL);
    void *gles = nxpl_dlopen_any(nxpl_gles_names, RTLD_NOW | RTLD_GLOBAL);
    if (!egl || !gles) {
        fprintf(stderr, "nxpl: no EGL/GLES library found - not a GigaBlue/Nexus box, falling back to /dev/fb0\n");
        return FALSE;
    }
    void *nxclient = dlopen("libnxclient.so", RTLD_NOW | RTLD_GLOBAL);
    if (!nxclient) {
        fprintf(stderr, "nxpl: dlopen(\"libnxclient.so\") failed (%s) - not a Nexus box, falling back to /dev/fb0\n", dlerror());
        return FALSE;
    }
    void (*GetDefaultJoinSettings)(void *) = dlsym(nxclient, "NxClient_GetDefaultJoinSettings");
    int (*Join)(const void *) = dlsym(nxclient, "NxClient_Join");
    if (!GetDefaultJoinSettings || !Join) {
        fprintf(stderr, "nxpl: NxClient_Join symbols not found - falling back to /dev/fb0\n");
        return FALSE;
    }
    unsigned char joinSettings[1024] = { 0 }; /* oversized zeroed buffer: GetDefaultJoinSettings only writes, Join only reads back - see
                                                * NexusEGLPlatformQuirk.h for why the real struct layout never needs to be known precisely */
    GetDefaultJoinSettings(joinSettings);
    int jrc = Join(joinSettings);
    fprintf(stderr, "nxpl: NxClient_Join() returned %d (0 = NEXUS_SUCCESS)\n", jrc);

    void *nxpl = dlopen("libnxpl.so", RTLD_NOW | RTLD_GLOBAL);
    if (!nxpl) {
        fprintf(stderr, "nxpl: dlopen(\"libnxpl.so\") failed: %s - falling back to /dev/fb0\n", dlerror());
        return FALSE;
    }
    void (*Register)(void **, void *) = dlsym(nxpl, "NXPL_RegisterNexusDisplayPlatform");
    nxpl_UnregisterNexusDisplayPlatform = dlsym(nxpl, "NXPL_UnregisterNexusDisplayPlatform");
    void (*GetDefaultWindowInfo)(void *) = dlsym(nxpl, "NXPL_GetDefaultNativeWindowInfoEXT");
    void *(*CreateWindow)(const void *) = dlsym(nxpl, "NXPL_CreateNativeWindowEXT");
    nxpl_DestroyNativeWindow = dlsym(nxpl, "NXPL_DestroyNativeWindow");
    nxpl_ShowNativeWindowEXT = (void (*)(void *, int))dlsym(nxpl, "NXPL_ShowNativeWindowEXT");
    if (!Register || !nxpl_UnregisterNexusDisplayPlatform || !GetDefaultWindowInfo || !CreateWindow ||
        !nxpl_DestroyNativeWindow || !nxpl_ShowNativeWindowEXT) {
        fprintf(stderr, "nxpl: a required NXPL symbol was not found - falling back to /dev/fb0\n");
        return FALSE;
    }
    Register(&nxpl_platform_handle, NULL);
    fprintf(stderr, "nxpl: NXPL_RegisterNexusDisplayPlatform() -> handle=%p\n", nxpl_platform_handle);
    if (!nxpl_platform_handle) {
        fprintf(stderr, "nxpl: register did not produce a handle - falling back to /dev/fb0\n");
        return FALSE;
    }

    /* Confirmed on a real GigaBlue 2026-09-23 (see the log in project memory): 1920x1080. No fb0 to query it from on this path - if a future
     * Nexus-based box needs a different resolution, this is the one place to make it configurable. */
    out.vinfo.xres = 1920;
    out.vinfo.yres = 1080;
    /* Matches this project's own precedent for "no real fb0" (see the !want_fb branch above): red.offset=0/green=8/blue=16/transp=24 makes
     * convert_pixels() emit genuine byte-order R,G,B,A - i.e. standard GL_RGBA/GL_UNSIGNED_BYTE texture data, no swizzle, no BGRA extension
     * dependency, and (since it's not the native_bgra fast path) always goes through the real per-pixel conversion, straight-alpha included. */
    out.vinfo.red.offset = 0; out.vinfo.green.offset = 8; out.vinfo.blue.offset = 16; out.vinfo.transp.offset = 24;
    out.vinfo.bits_per_pixel = 32;

    return TRUE;
}

/* Called once out.ow/out.oh/out.fb_x/out.fb_y are known (they need out.vinfo.xres/yres, which nxpl_try_open() just set). Creates the actual
 * native window/EGL surface at the real output size and compiles the textured-quad shader. On any failure, everything already registered by
 * nxpl_try_open() is torn down via nxpl_close() by the caller - there is no partial-nxpl state left running. */
static gboolean nxpl_finish_open(void)
{
    void *nxpl = dlopen("libnxpl.so", RTLD_NOW); /* already loaded by nxpl_try_open(); dlopen just bumps the refcount and returns the same handle */
    void (*GetDefaultWindowInfo_)(void *) = dlsym(nxpl, "NXPL_GetDefaultNativeWindowInfoEXT");
    void *(*CreateWindow)(const void *) = dlsym(nxpl, "NXPL_CreateNativeWindowEXT");
    unsigned char windowInfo[512] = { 0 }; /* oversized zeroed buffer, same reasoning as joinSettings above */
    GetDefaultWindowInfo_(windowInfo);
    guint32 *fields = (guint32 *)windowInfo; /* NXPL_NativeWindowInfoEXT: width,height,x,y (default_nexus.h) */
    fields[0] = (guint32)out.ow;
    fields[1] = (guint32)out.oh;
    fields[2] = (guint32)out.fb_x;
    fields[3] = (guint32)out.fb_y;
    nxpl_native_window = CreateWindow(windowInfo);
    fprintf(stderr, "nxpl: NXPL_CreateNativeWindowEXT(%dx%d @ %d,%d) -> %p\n", out.ow, out.oh, out.fb_x, out.fb_y, nxpl_native_window);
    if (!nxpl_native_window)
        return FALSE;
    nxpl_ShowNativeWindowEXT(nxpl_native_window, 1);

    void *egl = nxpl_dlopen_any(nxpl_egl_names, RTLD_NOW); /* already loaded by nxpl_try_open(): same handles back */
    void *gles = nxpl_dlopen_any(nxpl_gles_names, RTLD_NOW);
    void *(*eglGetDisplay_)(void *) = dlsym(egl, "eglGetDisplay");
    EGLBoolean (*eglInitialize_)(void *, int *, int *) = dlsym(egl, "eglInitialize");
    EGLBoolean (*eglBindAPI_)(unsigned) = dlsym(egl, "eglBindAPI");
    EGLBoolean (*eglChooseConfig_)(void *, const int *, void **, int, int *) = dlsym(egl, "eglChooseConfig");
    void *(*eglCreateWindowSurface_)(void *, void *, EGLNativeWindowTypeNX, const int *) = dlsym(egl, "eglCreateWindowSurface");
    void *(*eglCreateContext_)(void *, void *, void *, const int *) = dlsym(egl, "eglCreateContext");
    EGLBoolean (*eglMakeCurrent_)(void *, void *, void *, void *) = dlsym(egl, "eglMakeCurrent");
    nxpl_eglSwapBuffers = dlsym(egl, "eglSwapBuffers");
    if (!eglGetDisplay_ || !eglInitialize_ || !eglChooseConfig_ || !eglCreateWindowSurface_ || !eglCreateContext_ ||
        !eglMakeCurrent_ || !nxpl_eglSwapBuffers) {
        fprintf(stderr, "nxpl: a required EGL symbol was not found\n");
        return FALSE;
    }
    nxpl_egl_display = eglGetDisplay_(NULL);
    int maj = 0, min = 0;
    if (!nxpl_egl_display || !eglInitialize_(nxpl_egl_display, &maj, &min)) {
        fprintf(stderr, "nxpl: eglGetDisplay/eglInitialize failed\n");
        return FALSE;
    }
    fprintf(stderr, "nxpl: EGL %d.%d\n", maj, min);
    if (eglBindAPI_) eglBindAPI_(0x30A0 /* EGL_OPENGL_ES_API */);
    int cfgAttrs[] = { 0x3033 /* EGL_SURFACE_TYPE */, 4 /* EGL_WINDOW_BIT */, 0x3040 /* EGL_RENDERABLE_TYPE */, 4 /* EGL_OPENGL_ES2_BIT */,
        0x3024 /* EGL_RED_SIZE */, 8, 0x3023 /* EGL_GREEN_SIZE */, 8, 0x3022 /* EGL_BLUE_SIZE */, 8, 0x3021 /* EGL_ALPHA_SIZE */, 8,
        0x3038 /* EGL_NONE */ };
    void *config = NULL;
    int numConfigs = 0;
    if (!eglChooseConfig_(nxpl_egl_display, cfgAttrs, &config, 1, &numConfigs) || numConfigs < 1) {
        fprintf(stderr, "nxpl: eglChooseConfig found no EGL_WINDOW_BIT config\n");
        return FALSE;
    }
    nxpl_egl_surface = eglCreateWindowSurface_(nxpl_egl_display, config, (EGLNativeWindowTypeNX)nxpl_native_window, NULL);
    if (!nxpl_egl_surface) {
        fprintf(stderr, "nxpl: eglCreateWindowSurface failed\n");
        return FALSE;
    }
    int ctxAttrs[] = { 0x3098 /* EGL_CONTEXT_CLIENT_VERSION */, 2, 0x3038 /* EGL_NONE */ };
    nxpl_egl_context = eglCreateContext_(nxpl_egl_display, config, NULL, ctxAttrs);
    if (!nxpl_egl_context || !eglMakeCurrent_(nxpl_egl_display, nxpl_egl_surface, nxpl_egl_surface, nxpl_egl_context)) {
        fprintf(stderr, "nxpl: eglCreateContext/eglMakeCurrent failed\n");
        return FALSE;
    }

    unsigned (*glCreateShader_)(unsigned) = dlsym(gles, "glCreateShader");
    void (*glShaderSource_)(unsigned, int, const char **, const int *) = dlsym(gles, "glShaderSource");
    void (*glCompileShader_)(unsigned) = dlsym(gles, "glCompileShader");
    unsigned (*glCreateProgram_)(void) = dlsym(gles, "glCreateProgram");
    void (*glAttachShader_)(unsigned, unsigned) = dlsym(gles, "glAttachShader");
    void (*glBindAttribLocation_)(unsigned, unsigned, const char *) = dlsym(gles, "glBindAttribLocation");
    void (*glLinkProgram_)(unsigned) = dlsym(gles, "glLinkProgram");
    void (*glGenTextures_)(int, unsigned *) = dlsym(gles, "glGenTextures");
    void (*glBindTexture_)(unsigned, unsigned) = dlsym(gles, "glBindTexture");
    void (*glTexParameteri_)(unsigned, unsigned, int) = dlsym(gles, "glTexParameteri");
    void (*glTexImage2D_)(unsigned, int, int, int, int, int, unsigned, unsigned, const void *) = dlsym(gles, "glTexImage2D");
    void (*glEnableVertexAttribArray_)(unsigned) = dlsym(gles, "glEnableVertexAttribArray");
    void (*glVertexAttribPointer_)(unsigned, int, unsigned, unsigned char, int, const void *) = dlsym(gles, "glVertexAttribPointer");
    void (*glViewport_)(int, int, int, int) = dlsym(gles, "glViewport");
    void (*glDisable_)(unsigned) = dlsym(gles, "glDisable");
    nxpl_glTexSubImage2D = dlsym(gles, "glTexSubImage2D");
    nxpl_glClear = dlsym(gles, "glClear");
    nxpl_glDrawArrays = dlsym(gles, "glDrawArrays");
    nxpl_glUseProgram = dlsym(gles, "glUseProgram");
    if (!glCreateShader_ || !glShaderSource_ || !glCompileShader_ || !glCreateProgram_ || !glAttachShader_ ||
        !glBindAttribLocation_ || !glLinkProgram_ || !glGenTextures_ || !glBindTexture_ || !glTexParameteri_ || !glTexImage2D_ ||
        !glEnableVertexAttribArray_ || !glVertexAttribPointer_ || !glViewport_ || !glDisable_ ||
        !nxpl_glTexSubImage2D || !nxpl_glClear || !nxpl_glDrawArrays || !nxpl_glUseProgram) {
        fprintf(stderr, "nxpl: a required GLES symbol was not found\n");
        return FALSE;
    }

    unsigned vs = glCreateShader_(0x8B31 /* GL_VERTEX_SHADER */);
    glShaderSource_(vs, 1, &nxpl_vs_src, NULL);
    glCompileShader_(vs);
    unsigned fs = glCreateShader_(0x8B30 /* GL_FRAGMENT_SHADER */);
    glShaderSource_(fs, 1, &nxpl_fs_src, NULL);
    glCompileShader_(fs);
    nxpl_program = glCreateProgram_();
    glAttachShader_(nxpl_program, vs);
    glAttachShader_(nxpl_program, fs);
    glBindAttribLocation_(nxpl_program, 0, "pos");
    glBindAttribLocation_(nxpl_program, 1, "texcoord");
    glLinkProgram_(nxpl_program);
    nxpl_glUseProgram(nxpl_program);

    glGenTextures_(1, &nxpl_texture);
    glBindTexture_(0x0DE1 /* GL_TEXTURE_2D */, nxpl_texture);
    glTexParameteri_(0x0DE1, 0x2801 /* GL_TEXTURE_MIN_FILTER */, 0x2601 /* GL_LINEAR */);
    glTexParameteri_(0x0DE1, 0x2800 /* GL_TEXTURE_MAG_FILTER */, 0x2601);
    glTexParameteri_(0x0DE1, 0x2802 /* GL_TEXTURE_WRAP_S */, 0x812F /* GL_CLAMP_TO_EDGE */);
    glTexParameteri_(0x0DE1, 0x2803 /* GL_TEXTURE_WRAP_T */, 0x812F);
    glTexImage2D_(0x0DE1, 0, 0x1908 /* GL_RGBA */, out.ow, out.oh, 0, 0x1908, 0x1401 /* GL_UNSIGNED_BYTE */, out.conv);

    glEnableVertexAttribArray_(0);
    glVertexAttribPointer_(0, 2, 0x1406 /* GL_FLOAT */, 0, 4 * sizeof(float), nxpl_quad);
    glEnableVertexAttribArray_(1);
    glVertexAttribPointer_(1, 2, 0x1406, 0, 4 * sizeof(float), nxpl_quad + 2);
    glDisable_(0x0BE2 /* GL_BLEND */); /* first version: assume opaque HbbTV content, matching --opaque's own precedent for the fb0 path */
    glViewport_(0, 0, out.ow, out.oh);

    fprintf(stderr, "nxpl: ready - %dx%d NXPL window presented via a real EGL window surface\n", out.ow, out.oh);
    return TRUE;
}

/* out.conv just changed (a damaged rect was converted into it) - upload and present the whole frame. One upload+swap per WebKit frame (called
 * from view_render_buffer after all of a frame's damage rects are processed), not per rect - but only the union bounding box of everything
 * touched since the last present() is re-uploaded (see nxpl_dirty_x0/y0/x1/y1 above), not the whole out.ow x out.oh frame. */
static void nxpl_mark_dirty(int x0, int y0, int x1, int y1)
{
    if (!nxpl_dirty || x0 < nxpl_dirty_x0)
        nxpl_dirty_x0 = x0;
    if (!nxpl_dirty || y0 < nxpl_dirty_y0)
        nxpl_dirty_y0 = y0;
    if (!nxpl_dirty || x1 > nxpl_dirty_x1)
        nxpl_dirty_x1 = x1;
    if (!nxpl_dirty || y1 > nxpl_dirty_y1)
        nxpl_dirty_y1 = y1;
    nxpl_dirty = TRUE;
}

static void nxpl_present(void)
{
    if (!nxpl_dirty)
        return;
    nxpl_dirty = FALSE;
    int y0 = CLAMP(nxpl_dirty_y0, 0, out.oh), y1 = CLAMP(nxpl_dirty_y1, 0, out.oh);
    int x0 = CLAMP(nxpl_dirty_x0, 0, out.ow), x1 = CLAMP(nxpl_dirty_x1, 0, out.ow);
    if (y1 <= y0 || x1 <= x0)
        return;
    int w = x1 - x0, h = y1 - y0;
    int uploaded_w; /* the width actually passed to glTexSubImage2D - w in the compacted branch, out.ow in the full-width branch (was a metric
                      * bug before r24: upload_px used to always compute w*h even on the full-width path, understating what was actually sent) */
    gint64 t0 = g_get_monotonic_time();
    nxpl_glUseProgram(nxpl_program);
    if (w * 10 < out.ow * 9) {
        /* Narrower than 90% of the screen width: compact rows x0..x1 into a tightly-packed scratch buffer first (plain GLES2 has no
         * GL_UNPACK_ROW_LENGTH to sub-range a row directly out of out.conv's packed buffer) and upload only that - r22's own measurement (real
         * GigaBlue, 2026-09-23: min 2.02 ms for a 138240 px update vs. max 52.95 ms for a full 2073600 px one) proved this is bandwidth-bound,
         * and CPU memcpy bandwidth is far higher than the ~150-270 MB/s that GPU upload measured at, so paying for this copy to shrink the
         * upload is a real net win. Skipped above 90% width: compacting would just add CPU cost for negligible saving there.
         * r23's own result: this made the AVERAGE slightly better but the MAX dramatically worse per byte (48.28 ms for only 67500 px, vs r22's
         * 52.95 ms for a full 2073600 px screen - ~30x fewer bytes, almost the same time). Leading theory: a narrow-but-tall compacted update
         * crosses many more of this tiled GPU's internal texture tile boundaries per byte than a wide update does - width/height are now
         * recorded at min/max (below) to confirm this shape theory directly instead of guessing further. */
        uploaded_w = w;
        size_t need = (size_t)w * h;
        if (nxpl_scratch_len < need) {
            g_free(nxpl_scratch);
            nxpl_scratch = g_new(guint32, need);
            nxpl_scratch_len = need;
        }
        for (int row = 0; row < h; row++)
            memcpy(nxpl_scratch + (size_t)row * w, out.conv + (size_t)(y0 + row) * out.ow + x0, (size_t)w * 4);
        nxpl_glTexSubImage2D(0x0DE1 /* GL_TEXTURE_2D */, 0, x0, y0, w, h, 0x1908 /* GL_RGBA */, 0x1401 /* GL_UNSIGNED_BYTE */, nxpl_scratch);
    } else {
        uploaded_w = out.ow;
        nxpl_glTexSubImage2D(0x0DE1 /* GL_TEXTURE_2D */, 0, 0, y0, out.ow, h, 0x1908 /* GL_RGBA */, 0x1401 /* GL_UNSIGNED_BYTE */,
            out.conv + (size_t)y0 * out.ow);
    }
    /* no glClear: the quad below always covers the whole screen, so every pixel is overwritten regardless - a clear first is pure wasted GPU
     * time before every swap, and one more thing that can cost a frame's timing budget on this SoC */
    nxpl_glDrawArrays(5 /* GL_TRIANGLE_STRIP */, 0, 4);
    gint64 t1 = g_get_monotonic_time();
    nxpl_eglSwapBuffers(nxpl_egl_display, nxpl_egl_surface);
    gint64 t2 = g_get_monotonic_time();
    nxpl_present_count++;
    double upload_ms = (t1 - t0) / 1000.0;
    long upload_px = (long)uploaded_w * h; /* the ACTUAL pixel count uploaded (compacted or full-width) */
    nxpl_upload_ms += upload_ms;
    nxpl_swap_ms += (t2 - t1) / 1000.0;
    if (nxpl_upload_ms_min < 0 || upload_ms < nxpl_upload_ms_min) {
        nxpl_upload_ms_min = upload_ms; nxpl_upload_px_at_min = upload_px;
        nxpl_upload_w_at_min = uploaded_w; nxpl_upload_h_at_min = h;
    }
    if (upload_ms > nxpl_upload_ms_max) {
        nxpl_upload_ms_max = upload_ms; nxpl_upload_px_at_max = upload_px;
        nxpl_upload_w_at_max = uploaded_w; nxpl_upload_h_at_max = h;
    }
}

/* Tear down everything nxpl_try_open()/nxpl_finish_open() may have registered/created/shown, in the order Nexus expects (hide -> destroy ->
 * unregister) - called on normal exit and on any failure partway through nxpl_finish_open(), so nothing is ever left registered or visible. */
static void nxpl_close(void)
{
    if (nxpl_native_window && nxpl_ShowNativeWindowEXT)
        nxpl_ShowNativeWindowEXT(nxpl_native_window, 0);
    if (nxpl_native_window && nxpl_DestroyNativeWindow)
        nxpl_DestroyNativeWindow(nxpl_native_window);
    nxpl_native_window = NULL;
    if (nxpl_platform_handle && nxpl_UnregisterNexusDisplayPlatform)
        nxpl_UnregisterNexusDisplayPlatform(nxpl_platform_handle);
    nxpl_platform_handle = NULL;
    fprintf(stderr, "nxpl: released the Nexus display\n");
}

static void out_scale_rect(int rx, int ry, int rw, int rh);

/* The application has grown too big for pictures (a colour key opened its whole user interface): enigma2 makes a texture of every picture, at this size faster
 * than it frees them, until the GPU memory is gone - and enigma2 crashed in the driver. An application that fills the screen does not need enigma2's own screen
 * anyway: from here on the frame goes to the framebuffer, with the guard, like the application of the red button. The plugin is told ("overlay fullscreen")
 * and takes its picture away; what is on the screen is cleared when wpe_fb ends. */
static void png_promote(void)
{
    fprintf(stderr, "overlay: the application has grown too big for a picture: the frame goes to the framebuffer from now on\n");
    png_cleanup();
    g_free(out.png_prefix);
    out.png_prefix = NULL;
    free(out.png_last);
    out.png_last = NULL;
    fb_read_var(); /* the real colour layout again (the pictures were made in ARGB) */
    if (out.scale != 1.0) {
        out_scale_rect(0, 0, out.w, out.h); /* converts the whole frame and, with the framebuffer on, draws it */
    } else {
        for (int row = 0; row < out.h; row++)
            convert_pixels(out.conv + (size_t)row * out.w, out.canvas + (size_t)row * out.w * 4, out.w);
        fb_paint_all();
    }
    out.guard = TRUE;
    out.last_page = out.vinfo.yoffset / out.vinfo.yres;
    g_timeout_add(10, guard_tick, NULL);
    evt_send("overlay fullscreen\n");
}

/* Hand the screen back: on every page we painted and that still holds our frame, put back the pixels that were
 * there before (same page) or clear our area to transparent (other pages, where the old pixels are stale). */
static void fb_restore_area(void)
{
    if (out.egl_on) {
        egl_close();
        return;
    }
    if (out.nxpl_on) {
        nxpl_close();
        return;
    }
    if (!out.fb_on)
        return;
    if (out.png_prefix) {
        png_cleanup();
        return;
    }
    fb_read_var();
    const int pages = out.vinfo.yres_virtual / out.vinfo.yres;
    int restored = 0, cleared = 0;
    for (int p = 0; p < pages && p < 8; p++) {
        if (!(out.painted & (1u << p)))
            continue;
        const int yoff = p * out.vinfo.yres;
        if (fb_mismatches(yoff) > 8)
            continue; /* the display owner has already replaced our frame on this page */
        const gboolean same_page = out.fb_saved && yoff == out.fb_saved_yoffset;
        for (int row = 0; row < out.sh; row++) {
            guint8 *dst = out.fb_map + (size_t)(yoff + out.sy + row) * out.finfo.line_length + (size_t)out.sx * 4;
            if (same_page)
                memcpy(dst, out.fb_saved + (size_t)row * out.sw * 4, (size_t)out.sw * 4);
            else
                memset(dst, 0, (size_t)out.sw * 4);
        }
        same_page ? restored++ : cleared++;
    }
    fprintf(stderr, "fb: released the screen (%d page(s) restored to the original pixels, %d cleared to transparent)\n", restored, cleared);
    g_clear_pointer(&out.fb_saved, g_free);
}

/* Bilinear sampling tables for output size ow x oh from a view of w x h (pixel centres aligned). */
static void scale_setup(void)
{
    out.xi0 = g_new(int, out.ow); out.xi1 = g_new(int, out.ow); out.xwt = g_new(guint16, out.ow);
    out.yi0 = g_new(int, out.oh); out.yi1 = g_new(int, out.oh); out.ywt = g_new(guint16, out.oh);
    for (int d = 0; d < out.ow; d++) {
        double s = (d + 0.5) / out.scale - 0.5;
        int i0 = (int)floor(s);
        double f = s - i0;
        out.xi0[d] = CLAMP(i0, 0, out.w - 1);
        out.xi1[d] = CLAMP(i0 + 1, 0, out.w - 1);
        out.xwt[d] = (guint16)CLAMP((int)(f * 256.0 + 0.5), 0, 256);
    }
    for (int d = 0; d < out.oh; d++) {
        double s = (d + 0.5) / out.scale - 0.5;
        int i0 = (int)floor(s);
        double f = s - i0;
        out.yi0[d] = CLAMP(i0, 0, out.h - 1);
        out.yi1[d] = CLAMP(i0 + 1, 0, out.h - 1);
        out.ywt[d] = (guint16)CLAMP((int)(f * 256.0 + 0.5), 0, 256);
    }
}

/* a * (256 - w) + b * w, all four channels of two BGRA pixels at once (two 16-bit lanes per multiplication) */
static inline guint32 lerp_px(guint32 a, guint32 b, guint32 w)
{
    guint32 rb = (((a & 0x00ff00ffu) * (256 - w) + (b & 0x00ff00ffu) * w) >> 8) & 0x00ff00ffu;
    guint32 ag = (((a >> 8) & 0x00ff00ffu) * (256 - w) + ((b >> 8) & 0x00ff00ffu) * w) & 0xff00ff00u;
    return rb | ag;
}

/* The view rect (in view pixels) has changed in out.canvas: recompute the output pixels it touches, scaled bilinearly (on the
 * premultiplied pixels, which is the right thing to interpolate), converted to the framebuffer format, and put them on the page. */
static void out_scale_rect(int rx, int ry, int rw, int rh)
{
    int ox0 = MAX(0, (int)floor((rx - 1) * out.scale)), ox1 = MIN(out.ow, (int)ceil((rx + rw + 1) * out.scale));
    int oy0 = MAX(0, (int)floor((ry - 1) * out.scale)), oy1 = MIN(out.oh, (int)ceil((ry + rh + 1) * out.scale));
    if (ox1 <= ox0 || oy1 <= oy0)
        return;
    static guint32 *row;
    static int row_len;
    if (row_len < ox1 - ox0) {
        g_free(row);
        row_len = out.ow;
        row = g_new(guint32, row_len);
    }
    const guint32 *cv = (const guint32 *)out.canvas;
    for (int dy = oy0; dy < oy1; dy++) {
        const guint32 *r0 = cv + (size_t)out.yi0[dy] * out.w, *r1 = cv + (size_t)out.yi1[dy] * out.w;
        const guint32 wy = out.ywt[dy];
        for (int dx = ox0; dx < ox1; dx++) {
            const int a = out.xi0[dx], b = out.xi1[dx];
            const guint32 wx = out.xwt[dx];
            row[dx - ox0] = lerp_px(lerp_px(r0[a], r0[b], wx), lerp_px(r1[a], r1[b], wx), wy);
        }
        convert_pixels(out.conv + (size_t)dy * out.ow + ox0, (const guint8 *)row, ox1 - ox0);
    }
    if (out.fb_on) {
        fb_read_var();
        fb_write_rect(out.vinfo.yoffset, ox0, oy0, ox1 - ox0, oy1 - oy0);
    } else if (out.nxpl_on) {
        nxpl_mark_dirty(ox0, oy0, ox1, oy1);
    }
}

/* Copy one damaged rect of a frame to the shadow canvas and the framebuffer. */
static void out_blit_rect(const guint8 *src, int stride, int rx, int ry, int rw, int rh)
{
    if (rx < 0) { rw += rx; rx = 0; }
    if (ry < 0) { rh += ry; ry = 0; }
    if (rx + rw > out.w) rw = out.w - rx;
    if (ry + rh > out.h) rh = out.h - ry;
    if (rw <= 0 || rh <= 0 || !out.canvas) /* no canvas: the layer was withdrawn (egl_close) while WebKit still delivers frames */
        return;

    if (out.egl_on) {
        /* WebKit with CPU rendering reports the whole view as damaged on every frame. Only the rows that really differ from the frame
         * already shared are copied, marked and uploaded by enigma2 (its GPU heap is small, every full-frame texture update costs
         * 3.6 MB of GPU work); a frame identical to the last one is dropped. WPE_EGL_NO_TRIM=1 turns this off. */
        if (!egl_no_trim) {
            const size_t rowlen = (size_t)rw * 4;
            int top = 0, bottom = rh;
            while (top < bottom && !memcmp(out.canvas + ((size_t)(ry + top) * out.w + rx) * 4, src + (size_t)(ry + top) * stride + (size_t)rx * 4, rowlen))
                top++;
            if (top == bottom)
                return;
            while (bottom > top + 1 && !memcmp(out.canvas + ((size_t)(ry + bottom - 1) * out.w + rx) * 4, src + (size_t)(ry + bottom - 1) * stride + (size_t)rx * 4, rowlen))
                bottom--;
            ry += top;
            rh = bottom - top;
        }
        held_supersede(rx, ry, rw, rh);
        if (held_take(src, stride, rx, ry, rw, rh))
            return;
        if (dip_copy(src, stride, rx, ry, rw, rh)) {
            egl_mark_dirty(ry, ry + rh);
            return;
        }
    }
    for (int row = 0; row < rh; row++)
        memcpy(out.canvas + ((size_t)(ry + row) * out.w + rx) * 4, src + (size_t)(ry + row) * stride + (size_t)rx * 4, (size_t)rw * 4);

    if (out.egl_on) {
        egl_mark_dirty(ry, ry + rh);
        return;
    }
    if (out.scale != 1.0) {
        if (out.conv)
            out_scale_rect(rx, ry, rw, rh);
        return;
    }
    if (!out.fb_on && !out.nxpl_on)
        return;

    /* convert the damaged rows into the framebuffer format once, then copy them to the visible page (fb0) or mark the frame dirty for the
     * next nxpl_present() (NXPL/Nexus) */
    for (int row = 0; row < rh; row++) {
        /* --unpremul-from-y: rows from that view row on are converted to straight alpha (side-by-side alpha test) */
        if (out.unpremul_from_y >= 0)
            out.unpremul = (ry + row) >= out.unpremul_from_y;
        convert_pixels(out.conv + (size_t)(ry + row) * out.w + rx, src + (size_t)(ry + row) * stride + (size_t)rx * 4, rw);
    }
    if (out.fb_on) {
        fb_read_var(); /* the visible page can change under us when the owner flips; the guard notices and repaints */
        fb_write_rect(out.vinfo.yoffset, rx, ry, rw, rh);
    } else {
        nxpl_mark_dirty(rx, ry, rx + rw, ry + rh);
    }
}

/* ------------------------------------------------------------------------- the custom WPE platform ---- */

static GType display_type, toplevel_type, view_type;
static gpointer toplevel_parent, view_parent;
static WPEBuffer *pending_buffer;
static guint pending_source;
static gint64 last_complete_us;

static gboolean complete_frame(gpointer data)
{
    WPEView *v = WPE_VIEW(data);
    pending_source = 0;
    if (pending_buffer) {
        WPEBuffer *b = pending_buffer;
        pending_buffer = NULL;
        wpe_view_buffer_rendered(v, b);
        wpe_view_buffer_released(v, b); /* the pixels were copied already */
        g_object_unref(b);
    }
    last_complete_us = g_get_monotonic_time();
    TRACE("ack");
    return G_SOURCE_REMOVE;
}

static gboolean view_render_buffer(WPEView *v, WPEBuffer *buffer, const WPERectangle *rects, guint n_rects, GError **error)
{
    gint64 t0 = g_get_monotonic_time();
    const guint8 *px = NULL;
    int stride = 0;
    GBytes *bytes = NULL;
    const int w = wpe_buffer_get_width(buffer), h = wpe_buffer_get_height(buffer);

    if (WPE_IS_BUFFER_SHM(buffer)) {
        WPEBufferSHM *shm = WPE_BUFFER_SHM(buffer);
        bytes = g_bytes_ref(wpe_buffer_shm_get_data(shm));
        stride = wpe_buffer_shm_get_stride(shm);
    } else {
        bytes = wpe_buffer_import_to_pixels(buffer, error);
        stride = w * 4;
    }
    if (!bytes)
        return FALSE;
    px = g_bytes_get_data(bytes, NULL);

    gint64 pixels = 0;
    if (n_rects == 0) {
        out_blit_rect(px, stride, 0, 0, w, h);
        pixels = (gint64)w * h;
    } else {
        for (guint i = 0; i < n_rects; i++) {
            out_blit_rect(px, stride, rects[i].x, rects[i].y, rects[i].width, rects[i].height);
            pixels += (gint64)rects[i].width * rects[i].height;
        }
    }
    if (out.egl_on)
        resync_note(bytes, stride, w, h);
    g_bytes_unref(bytes);
    if (out.egl_on)
        egl_frame_done(); /* publish to enigma2 (now, or once the stream of frames settles), after all of this frame's damage rects were marked */
    if (out.nxpl_on)
        nxpl_present(); /* one upload+swap per frame, after all of this frame's damage rects were converted into out.conv above */

    gint64 t1 = g_get_monotonic_time();
    out.frames++;
    out.pixels += pixels;
    out.blit_ms += (t1 - t0) / 1000.0;
    if (!out.first_us)
        out.first_us = t1;
    out.last_us = t1;
    key_awaits_frame = FALSE; /* the page has answered (see REPEAT_HOLD_US) */
    if (trace) {
        char list[160] = "";
        for (guint i = 0; i < n_rects && i < 3; i++)
            snprintf(list + strlen(list), sizeof list - strlen(list), " r%u=%d,%d,%d,%d", i, rects[i].x, rects[i].y, rects[i].width, rects[i].height);
        TRACE("frame %llu rects=%u px=%lld blit_ms=%.2f%s", (unsigned long long)out.frames, n_rects, (long long)pixels, (t1 - t0) / 1000.0, list);
    }

    /* pace the frame loop at ~60 Hz like a vsync'd display */
    if (pending_buffer)
        g_object_unref(pending_buffer);
    pending_buffer = g_object_ref(buffer);
    if (!pending_source) {
        gint64 next = last_complete_us + G_USEC_PER_SEC / 60;
        guint delay_ms = next > t1 ? (guint)((next - t1) / 1000) : 0;
        pending_source = g_timeout_add(delay_ms, complete_frame, v);
    }
    return TRUE;
}

static void view_notify_toplevel(GObject *o, GParamSpec *pspec, gpointer data)
{
    WPEView *v = WPE_VIEW(o);
    WPEToplevel *top = wpe_view_get_toplevel(v);
    if (!top) {
        wpe_view_unmap(v);
        return;
    }
    int w, h;
    wpe_toplevel_get_size(top, &w, &h);
    if (w && h)
        wpe_view_resized(v, w, h);
    wpe_view_map(v);
}

static void view_constructed(GObject *o)
{
    G_OBJECT_CLASS(view_parent)->constructed(o);
    g_signal_connect(o, "notify::toplevel", G_CALLBACK(view_notify_toplevel), NULL);
}

static void view_class_init(gpointer klass, gpointer data)
{
    view_parent = g_type_class_peek_parent(klass);
    G_OBJECT_CLASS(klass)->constructed = view_constructed;
    WPE_VIEW_CLASS(klass)->render_buffer = view_render_buffer;
}

static void toplevel_constructed(GObject *o)
{
    G_OBJECT_CLASS(toplevel_parent)->constructed(o);
    wpe_toplevel_state_changed(WPE_TOPLEVEL(o), WPE_TOPLEVEL_STATE_ACTIVE);
}

static gboolean toplevel_foreach_view_cb(WPEToplevel *t, WPEView *v, gpointer data)
{
    int w, h;
    wpe_toplevel_get_size(t, &w, &h);
    wpe_view_resized(v, w, h);
    return FALSE;
}

static gboolean toplevel_resize(WPEToplevel *t, int w, int h)
{
    wpe_toplevel_resized(t, w, h);
    wpe_toplevel_foreach_view(t, toplevel_foreach_view_cb, NULL);
    return TRUE;
}

static gboolean toplevel_set_fullscreen(WPEToplevel *t, gboolean fs)
{
    WPEToplevelState s = wpe_toplevel_get_state(t);
    s = fs ? (WPEToplevelState)(s | WPE_TOPLEVEL_STATE_FULLSCREEN) : (WPEToplevelState)(s & ~WPE_TOPLEVEL_STATE_FULLSCREEN);
    wpe_toplevel_state_changed(t, s);
    return TRUE;
}

static void toplevel_class_init(gpointer klass, gpointer data)
{
    toplevel_parent = g_type_class_peek_parent(klass);
    G_OBJECT_CLASS(klass)->constructed = toplevel_constructed;
    WPE_TOPLEVEL_CLASS(klass)->resize = toplevel_resize;
    WPE_TOPLEVEL_CLASS(klass)->set_fullscreen = toplevel_set_fullscreen;
}

static gboolean display_connect(WPEDisplay *d, GError **error)
{
    return TRUE;
}

static WPEView *display_create_view(WPEDisplay *d)
{
    return WPE_VIEW(g_object_new(view_type, "display", d, NULL));
}

static WPEToplevel *display_create_toplevel(WPEDisplay *d, guint max_views)
{
    return WPE_TOPLEVEL(g_object_new(toplevel_type, "display", d, NULL));
}

static gpointer display_get_egl_display(WPEDisplay *d, GError **error)
{
    g_set_error_literal(error, WPE_EGL_ERROR, WPE_EGL_ERROR_NOT_AVAILABLE, "wpe_fb has no EGL display (frames come as shared memory)");
    return NULL;
}

static void display_class_init(gpointer klass, gpointer data)
{
    WPEDisplayClass *dc = WPE_DISPLAY_CLASS(klass);
    dc->connect = display_connect;
    dc->create_view = display_create_view;
    dc->create_toplevel = display_create_toplevel;
    dc->get_egl_display = display_get_egl_display;
}

static GType register_subclass(GType parent, const char *name, GClassInitFunc class_init)
{
    GTypeQuery q;
    g_type_query(parent, &q); /* the base instance structs are opaque: size the subclass from what GLib reports */
    return g_type_register_static_simple(parent, name, q.class_size, class_init, q.instance_size, NULL, 0);
}

/* ------------------------------------------------------------------------------- hardware video plane ---- */

/* With --hwvideo the web process moves the hardware video plane (/proc/stb/vmpeg/0/dst_*) to wherever a <video> sits.
 * enigma2 only rewrites those values when its own video widget changes, so whatever we leave behind stays: remember the
 * rectangle at start and put it back on exit. */
static const char *const plane_names[4] = { "left", "top", "width", "height" };
static char plane_saved[4][16];
static gboolean plane_have_saved;

static gboolean plane_read(char values[4][16])
{
    for (int i = 0; i < 4; i++) {
        char path[64];
        snprintf(path, sizeof path, "/proc/stb/vmpeg/0/dst_%s", plane_names[i]);
        FILE *f = fopen(path, "r");
        if (!f || !fgets(values[i], 16, f)) {
            if (f)
                fclose(f);
            return FALSE;
        }
        fclose(f);
        g_strstrip(values[i]);
    }
    return TRUE;
}

/* NOT USED: blanking the plane from here left the video output dark after enigma2 played the service again (the picture only
 * came back after another stop/play cycle of the service), so the frozen last frame of the live service is left alone: it is
 * replaced by the first video frame, and it is only visible where the page is transparent. What it was for: enigma2 leaves the last decoded frame on the video plane when it stops the live service (stopService() only closes the
 * decoder); wherever the page is transparent that stale picture would show. Blank the plane the way eTSMPEGDecoder::
 * blankPrimaryVideoDecoder() does: VIDEO_STOP with mode 1 on the (free) video device. */
#ifndef VIDEO_STOP
#define VIDEO_STOP _IO('o', 21)
#endif
static void plane_blank(void) __attribute__((unused));
static void plane_blank(void)
{
    if (getenv("WPE_FB_NO_BLANK")) { /* protocol tests: the decoder may still belong to the live service */
        fprintf(stderr, "hwvideo: blanking skipped (WPE_FB_NO_BLANK)\n");
        return;
    }
    int fd = open("/dev/dvb/adapter0/video0", O_RDWR | O_NONBLOCK);
    if (fd < 0) {
        fprintf(stderr, "hwvideo: cannot open the video decoder to blank it: %s\n", g_strerror(errno));
        return;
    }
    fprintf(stderr, "hwvideo: blanking the video plane (VIDEO_STOP %s)\n", ioctl(fd, VIDEO_STOP, 1) < 0 ? g_strerror(errno) : "ok");
    close(fd);
}

static void plane_save(void)
{
    plane_have_saved = plane_read(plane_saved);
    if (plane_have_saved)
        fprintf(stderr, "hwvideo: video plane at start: %s,%s %sx%s\n", plane_saved[0], plane_saved[1], plane_saved[2], plane_saved[3]);
}

static void plane_restore(void)
{
    char now[4][16];
    if (!plane_have_saved || !plane_read(now))
        return;
    gboolean same = TRUE;
    for (int i = 0; i < 4; i++)
        same = same && !strcmp(now[i], plane_saved[i]);
    if (same)
        return;
    for (int i = 0; i < 4; i++) {
        char path[64];
        snprintf(path, sizeof path, "/proc/stb/vmpeg/0/dst_%s", plane_names[i]);
        FILE *f = fopen(path, "w");
        if (f) {
            fprintf(f, "%s\n", plane_saved[i]);
            fclose(f);
        }
    }
    FILE *f = fopen("/proc/stb/vmpeg/0/dst_apply", "w");
    if (f) {
        fputs("1\n", f);
        fclose(f);
    }
    fprintf(stderr, "hwvideo: video plane restored to %s,%s %sx%s\n", plane_saved[0], plane_saved[1], plane_saved[2], plane_saved[3]);
}

/* ------------------------------------------------------------------------------------------- main ---- */

static void evt_send(const char *line);

static void on_load_changed(WebKitWebView *v, WebKitLoadEvent ev, gpointer data)
{
    static const char *names[] = { "STARTED", "REDIRECTED", "COMMITTED", "FINISHED" };
    static gboolean told;
    fprintf(stderr, "load-changed: %s\n", (unsigned)ev < 4 ? names[ev] : "?");
    if (ev == WEBKIT_LOAD_FINISHED && !told) {
        /* the first real page is there: the plugin can take its "loading" message away (event FIFO, only with --hwvideo-lazy --evt) */
        const char *uri = webkit_web_view_get_uri(v);
        if (uri && strncmp(uri, "about:", 6)) {
            told = TRUE;
            evt_send("loaded\n");
        }
    }
}

static void on_title_changed(GObject *obj, GParamSpec *pspec, gpointer data)
{
    const char *t = webkit_web_view_get_title(WEBKIT_WEB_VIEW(obj));
    if (t && (strncmp(t, "fps=", 4) == 0 || strncmp(t, "key=", 4) == 0 || strncmp(t, "vid=", 4) == 0))
        fprintf(stderr, "page reports %s\n", t);
}

/* ------------------------------------------------------------------------------- control channel ---- */

/* Remote-control key names (as sent by the enigma2 plugin) -> X keysym + evdev keycode. */
static const struct {
    const char *name;
    guint keyval, evdev;
    int hbbtv; /* JS/HbbTV key code (ETSI TS 102 796 table 11), used in --orb mode */
} key_table[] = {
    { "up", WPE_KEY_Up, 103, 38 }, { "down", WPE_KEY_Down, 108, 40 }, { "left", WPE_KEY_Left, 105, 37 }, { "right", WPE_KEY_Right, 106, 39 },
    { "ok", WPE_KEY_Return, 28, 13 }, { "back", WPE_KEY_BackSpace, 14, 461 }, { "exit", WPE_KEY_Escape, 1, 27 }, { "menu", WPE_KEY_Menu, 139, 0 },
    { "red", WPE_KEY_F1, 59, 403 }, { "green", WPE_KEY_F2, 60, 404 }, { "yellow", WPE_KEY_F3, 61, 405 }, { "blue", WPE_KEY_F4, 62, 406 },
    { "play", WPE_KEY_AudioPlay, 207, 415 }, { "pause", WPE_KEY_AudioPause, 201, 19 }, { "stop", WPE_KEY_AudioStop, 166, 413 },
    { "ff", WPE_KEY_AudioForward, 208, 417 }, { "rew", WPE_KEY_AudioRewind, 168, 412 }, { "rec", WPE_KEY_AudioRecord, 167, 416 },
    { "0", WPE_KEY_0, 11, 48 }, { "1", WPE_KEY_0 + 1, 2, 49 }, { "2", WPE_KEY_0 + 2, 3, 50 }, { "3", WPE_KEY_0 + 3, 4, 51 }, { "4", WPE_KEY_0 + 4, 5, 52 },
    { "5", WPE_KEY_0 + 5, 6, 53 }, { "6", WPE_KEY_0 + 6, 7, 54 }, { "7", WPE_KEY_0 + 7, 8, 55 }, { "8", WPE_KEY_0 + 8, 9, 56 }, { "9", WPE_KEY_0 + 9, 10, 57 },
};

static char *orb_socket; /* per process (/tmp/orb_bridge.<pid>.sock): two instances must never share a bridge */

static gboolean orb_mode;
static const char *orb_dir;

static gboolean send_key(const char *name, gboolean down, gboolean up)
{
    WPEView *wv = webkit_web_view_get_wpe_view(view);
    if (down)
        key_pressed_us = g_get_monotonic_time();
    for (guint i = 0; i < G_N_ELEMENTS(key_table); i++) {
        if (strcmp(key_table[i].name, name))
            continue;
        if (orb_mode) {
            /* the engine decides (key set of the running app) and dispatches the DOM event itself */
            if (!key_table[i].hbbtv)
                return FALSE;
            int consumed = 0;
            if (down)
                consumed |= orb_host_key(key_table[i].hbbtv, 1);
            if (up)
                consumed |= orb_host_key(key_table[i].hbbtv, 0);
            fprintf(stderr, "orb key %s (%d): %s\n", name, key_table[i].hbbtv, consumed ? "consumed by the application" : "not in the application's key set");
            if (consumed && down) {
                key_awaits_frame = TRUE;
                key_sent_us = g_get_monotonic_time();
            }
            return TRUE;
        }
        for (int phase = 0; phase < 2; phase++) {
            if ((phase == 0 && !down) || (phase == 1 && !up))
                continue;
            WPEEvent *ev = wpe_event_keyboard_new(phase == 0 ? WPE_EVENT_KEYBOARD_KEY_DOWN : WPE_EVENT_KEYBOARD_KEY_UP, wv, WPE_INPUT_SOURCE_KEYBOARD,
                (guint32)(g_get_monotonic_time() / 1000), (WPEModifiers)0, key_table[i].evdev + 8, key_table[i].keyval);
            wpe_view_event(wv, ev);
            wpe_event_unref(ev);
        }
        return TRUE;
    }
    return FALSE;
}

/* ------------------------------------------------------------------------------------ decoder handover ---- */

/* With --hwvideo-lazy the live service keeps playing (an HbbTV application runs on top of the live picture) until media really
 * needs the video/audio decoder. The web process (the Dreambox quirks in WebKit) asks for it over a UNIX socket before it creates
 * a dvb sink and keeps the connection open as a lease while any sink exists:
 *
 *   web process --"acquire <pid>"--> wpe_fb --"acquire" line on the event FIFO--> enigma2 plugin (stops the live service)
 *   plugin --"decoder-free" on the control FIFO--> wpe_fb (blanks the plane) --"ok"--> web process (creates the sinks)
 *   ... last sink destroyed, lease closed --> wpe_fb --"release" on the event FIFO--> plugin (plays the service again)
 *
 * If the plugin does not answer within 9 seconds the request is denied and the web process falls back to software sinks. */
typedef struct {
    int fd;
    guint source;
    gboolean requested, granted;
    char buf[64];
    size_t len;
} Lease;

static gboolean hw_lazy;
static char *handover_path, *evt_path;
static int handover_fd = -1, evt_fd = -1;
static GList *leases;
static gboolean decoder_ours, decoder_requested;
static guint decoder_timeout;

static void evt_send(const char *line)
{
    fprintf(stderr, "handover: event %s", line);
    if (evt_fd >= 0 && write(evt_fd, line, strlen(line)) < 0) {
        /* nobody reads the FIFO (the buffer is full): nothing to do about it */
    }
}

static void lease_reply(Lease *l, const char *text)
{
    if (write(l->fd, text, strlen(text)) < 0) {
        /* the client is gone: its end of the connection is noticed by the read watch */
    }
}

static void decoder_check_release(void)
{
    if (!decoder_ours)
        return;
    for (GList *i = leases; i; i = i->next) {
        if (((Lease *)i->data)->granted)
            return;
    }
    decoder_ours = FALSE;
    evt_send("release\n");
}

/* The client went away or is denied. (If called from its own watch callback, l->source has been zeroed first.) */
static void lease_drop(Lease *l)
{
    leases = g_list_remove(leases, l);
    if (l->source)
        g_source_remove(l->source);
    close(l->fd);
    g_free(l);
    decoder_check_release();
}

static gboolean decoder_timed_out(gpointer data)
{
    decoder_timeout = 0;
    if (!decoder_requested)
        return G_SOURCE_REMOVE;
    fprintf(stderr, "handover: the plugin did not free the decoder in time: denied\n");
    decoder_requested = FALSE;
    GList *copy = g_list_copy(leases);
    for (GList *i = copy; i; i = i->next) {
        Lease *l = i->data;
        if (!l->granted) {
            lease_reply(l, "no\n");
            lease_drop(l);
        }
    }
    g_list_free(copy);
    evt_send("release\n"); /* the plugin may have stopped the service by now */
    return G_SOURCE_REMOVE;
}

/* ctl "decoder-free": enigma2 has stopped the live service and let go of the decoder */
static void handover_decoder_free(void)
{
    if (!decoder_requested) {
        fprintf(stderr, "handover: decoder-free without a request: ignored\n");
        return;
    }
    decoder_requested = FALSE;
    if (decoder_timeout) {
        g_source_remove(decoder_timeout);
        decoder_timeout = 0;
    }
    decoder_ours = TRUE;
    for (GList *i = leases; i; i = i->next) {
        Lease *l = i->data;
        if (l->requested && !l->granted) {
            l->granted = TRUE;
            lease_reply(l, "ok\n");
        }
    }
    fprintf(stderr, "handover: decoder granted\n");
}

static gboolean lease_readable(gint fd, GIOCondition cond, gpointer data)
{
    Lease *l = data;
    ssize_t n = read(fd, l->buf + l->len, sizeof l->buf - l->len - 1);
    if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) {
        fprintf(stderr, "handover: lease closed%s\n", l->granted ? "" : " (it was not granted)");
        l->source = 0;
        lease_drop(l);
        return G_SOURCE_REMOVE;
    }
    if (n > 0) {
        l->len += (size_t)n;
        l->buf[l->len] = 0;
    }
    if (!l->requested && strstr(l->buf, "acquire")) {
        l->requested = TRUE;
        fprintf(stderr, "handover: web process asks for the decoder: %s", l->buf);
        if (decoder_ours) {
            l->granted = TRUE;
            lease_reply(l, "ok\n");
        } else if (!decoder_requested) {
            decoder_requested = TRUE;
            evt_send("acquire\n");
            decoder_timeout = g_timeout_add_seconds(9, decoder_timed_out, NULL);
        }
    }
    return G_SOURCE_CONTINUE;
}

static gboolean handover_accept(gint fd, GIOCondition cond, gpointer data)
{
    int client = accept4(fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (client < 0)
        return G_SOURCE_CONTINUE;
    Lease *l = g_new0(Lease, 1);
    l->fd = client;
    l->source = g_unix_fd_add(client, G_IO_IN | G_IO_HUP | G_IO_ERR, lease_readable, l);
    leases = g_list_prepend(leases, l);
    return G_SOURCE_CONTINUE;
}

static void handover_start(const char *evt)
{
    handover_path = g_strdup_printf("/tmp/wpe_dream.%d.sock", (int)getpid());
    unlink(handover_path);
    handover_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    struct sockaddr_un address = { .sun_family = AF_UNIX };
    g_strlcpy(address.sun_path, handover_path, sizeof address.sun_path);
    if (handover_fd < 0 || bind(handover_fd, (struct sockaddr *)&address, sizeof address) < 0 || listen(handover_fd, 4) < 0) {
        perror("decoder handover socket");
        return;
    }
    chmod(handover_path, 0600);
    g_unix_fd_add(handover_fd, G_IO_IN, handover_accept, NULL);
    g_setenv("WPE_DREAM_HANDOVER", handover_path, TRUE); /* the web process reads it: set before it is spawned */
    if (evt) {
        evt_path = g_strdup(evt);
        unlink(evt_path);
        if (mkfifo(evt_path, 0600) < 0)
            perror("mkfifo (event channel)");
        else
            evt_fd = open(evt_path, O_RDWR | O_NONBLOCK); /* O_RDWR: writing never blocks or fails for lack of a reader */
    }
    fprintf(stderr, "handover: waiting for decoder requests on %s, events on %s\n", handover_path, evt ? evt : "(no event channel)");
}

static void handover_stop(void)
{
    if (decoder_ours)
        evt_send("release\n");
    for (GList *i = leases; i; i = i->next) {
        Lease *l = i->data;
        close(l->fd);
    }
    g_list_free_full(leases, g_free);
    leases = NULL;
    if (handover_path) {
        unlink(handover_path);
    }
    if (evt_path)
        unlink(evt_path);
}

/* ---------------------------------------------------------------------------------------- ORB host ---- */

/* The engine calls these from its own threads; everything touching the view is hopped onto the main loop. */
static gboolean idle_eval_js(gpointer script)
{
    webkit_web_view_evaluate_javascript(view, script, -1, NULL, NULL, NULL, NULL, NULL);
    return G_SOURCE_REMOVE;
}

static gboolean idle_load_url(gpointer url)
{
    fprintf(stderr, "orb: loading application page %s\n", (char *)url);
    webkit_web_view_load_uri(view, url);
    return G_SOURCE_REMOVE;
}

static gboolean idle_quit(gpointer data)
{
    g_main_loop_quit(loop);
    return G_SOURCE_REMOVE;
}

static void orb_cb_eval_js(const char *script, void *user)
{
    g_main_context_invoke_full(NULL, G_PRIORITY_HIGH_IDLE, idle_eval_js, g_strdup(script), g_free);
}

static void orb_cb_load_url(const char *url, void *user)
{
    g_main_context_invoke_full(NULL, G_PRIORITY_HIGH_IDLE, idle_load_url, g_strdup(url), g_free);
}

static void orb_cb_set_visible(int visible, void *user)
{
    fprintf(stderr, "orb: application %s\n", visible ? "shown" : "hidden");
}

static void orb_cb_exit_pressed(void *user)
{
    fprintf(stderr, "orb: EXIT pressed -> leaving\n");
    g_main_context_invoke(NULL, idle_quit, NULL);
}

/* The application selected an audio track / subtitle (Broadcast.overrideComponentSelection / restoreComponentSelection): enigma2 does the
 * switching, so tell the plugin on the event FIFO (the one of the decoder handover, only there with --hwvideo-lazy --evt):
 *   select audio <pid> | default | none        select subtitle <pid> | default | none
 * Called from any thread; one write() of a short line is atomic. */
static void orb_cb_select_component(int type, const char *what, void *user)
{
    const char *kind = type == 1 ? "audio" : type == 2 ? "subtitle" : NULL;
    if (!kind || !what || !*what || strlen(what) > 16)
        return;
    for (const char *c = what; *c; c++)
        if (!g_ascii_isalnum(*c)) /* pid, "default", "none": nothing else goes to the plugin */
            return;
    if (evt_fd < 0) {
        fprintf(stderr, "orb: select %s %s: no event channel to the plugin (--evt), ignored\n", kind, what);
        return;
    }
    char line[64];
    snprintf(line, sizeof line, "select %s %s\n", kind, what);
    evt_send(line);
}

/* The keys the application has asked for (Manager.setKeyValue: a mask of red, green, yellow, blue, navigation, VCR, ... keys) are what the plugin
 * has to take from enigma2 when the application runs on top of the live picture: tell it when the mask changes ("keyset <mask>", event FIFO). */
static gboolean keyset_tick(gpointer data)
{
    static unsigned last = 0xffffffffu;
    const unsigned mask = orb_host_key_set_mask();
    if (mask != last) {
        char line[32];
        last = mask;
        snprintf(line, sizeof line, "keyset %u\n", mask);
        evt_send(line);
    }
    return G_SOURCE_CONTINUE;
}

/* ----------------------------------------------------------------------- video/broadcast object -> PiG ---- */

/* HbbTV shows the live picture where the application puts its video/broadcast object (a "picture in graphics"). Everything on the box
 * is already in place for it: the live service plays on the video plane behind the layer. What was missing is (1) a transparent hole in
 * the layer where the object is, and (2) the video plane sized to that rectangle. The rectangle is read from the page itself by a small
 * script (the polyfill keeps the <object> element) and arrives here as "x,y,w,h" in the 1280x720 coordinates of the page. */

static void plane_write(int left, int top, int width, int height)
{
    const int v[4] = { left, top, width, height };
    for (int i = 0; i < 4; i++) {
        char path[64];
        snprintf(path, sizeof path, "/proc/stb/vmpeg/0/dst_%s", plane_names[i]);
        FILE *f = fopen(path, "w");
        if (f) {
            fprintf(f, "%08x\n", v[i]); /* like enigma2's eVideoWidget::writeProc() */
            fclose(f);
        }
    }
    FILE *f = fopen("/proc/stb/vmpeg/0/dst_apply", "w");
    if (f) {
        fputs("1\n", f);
        fclose(f);
    }
}

static void apply_video_rect(int x, int y, int w, int h)
{
    if (g_getenv("WPE_EGL_NO_HOLE") && atoi(g_getenv("WPE_EGL_NO_HOLE")) == 1)
        return; /* diagnostic: never cut a hole / move the video plane */
    static int last[4] = { -1, -1, -1, -1 };
    if (last[0] == x && last[1] == y && last[2] == w && last[3] == h)
        return;
    last[0] = x; last[1] = y; last[2] = w; last[3] = h;
    if (!out.egl_on) {
        fprintf(stderr, "video/broadcast %d,%d %dx%d: only supported with --egl-sink, ignored\n", x, y, w, h);
        return;
    }
    const gboolean full = w <= 0 || h <= 0 || (x <= 0 && y <= 0 && w >= 1280 && h >= 720);
    if (full) {
        egl_set_hole(0, 0, 0, 0);
        if (plane_moved) {
            plane_restore();
            plane_moved = FALSE;
        }
        fprintf(stderr, "video/broadcast: whole screen (no hole)\n");
        return;
    }
    if (!plane_have_saved)
        plane_save();
    /* the application is laid out at 1280x720; the driver's output space is 720x576 (same mapping as enigma2's eVideoWidget) */
    plane_write(x * 720 / 1280, y * 576 / 720, w * 720 / 1280, h * 576 / 720);
    plane_moved = TRUE;
    const double sc = (double)out.w / 1280.0; /* frame pixels per page pixel (hidpi) */
    egl_set_hole((int)(x * sc), (int)(y * sc), (int)(w * sc + 0.5), (int)(h * sc + 0.5));
    fprintf(stderr, "video/broadcast: picture in graphics at %d,%d %dx%d\n", x, y, w, h);
}

/* An application animates its video/broadcast object (grows, shrinks, slides in): every step would cut a different hole and move the video plane,
 * which shows as blinking. The rectangle is applied only once it has stayed the same for a while, and how long depends on what is on screen:
 *  - no picture in graphics yet (whole screen) and a rectangle appears: WPE_EGL_PIG_FIRST_MS (default 2000). A loading animation (ZDF's)
 *    parks the object at several places, each for a few hundred ms, before the application settles;
 *  - a picture in graphics is showing and it moves or goes away: WPE_EGL_PIG_SETTLE_MS (default 300). Waiting longer would leave the video at
 *    the old place while the page behind it has already changed. */
static int pig_pending[4];
static guint pig_timer;
static int pig_settle_ms = -1, pig_first_ms = -1;

static int pig_env_ms(const char *name, int def)
{
    return g_getenv(name) ? MAX(0, atoi(g_getenv(name))) : def;
}

static gboolean pig_apply_cb(gpointer data)
{
    (void)data;
    pig_timer = 0;
    apply_video_rect(pig_pending[0], pig_pending[1], pig_pending[2], pig_pending[3]);
    return G_SOURCE_REMOVE;
}

static gboolean video_rect_idle(gpointer data)
{
    int x = 0, y = 0, w = 0, h = 0;
    if (sscanf((const char *)data, "%d,%d,%d,%d", &x, &y, &w, &h) == 4) {
        if (pig_settle_ms < 0) {
            pig_settle_ms = pig_env_ms("WPE_EGL_PIG_SETTLE_MS", 300);
            pig_first_ms = pig_env_ms("WPE_EGL_PIG_FIRST_MS", 2000);
        }
        const gboolean to_full = w <= 0 || h <= 0 || (x <= 0 && y <= 0 && w >= 1280 && h >= 720);
        const int ms = (plane_moved || to_full) ? pig_settle_ms : pig_first_ms;
        if (ms == 0) {
            apply_video_rect(x, y, w, h);
        } else {
            pig_pending[0] = x; pig_pending[1] = y; pig_pending[2] = w; pig_pending[3] = h;
            if (pig_timer)
                g_source_remove(pig_timer);
            pig_timer = g_timeout_add((guint)ms, pig_apply_cb, NULL);
        }
    }
    g_free(data);
    return G_SOURCE_REMOVE;
}

/* the script's postMessage("x,y,w,h"): any thread is possible, so hop to the main loop */
static void on_video_rect_message(WebKitUserContentManager *ucm, JSCValue *value, gpointer user)
{
    (void)ucm; (void)user;
    if (!jsc_value_is_string(value))
        return;
    char *text = jsc_value_to_string(value);
    if (text && strlen(text) < 48)
        g_idle_add(video_rect_idle, g_strdup(text));
    g_free(text);
}

/* Runs right after the ORB polyfill. The polyfill wraps the innerHTML/outerHTML setters and insertAdjacentHTML (objectManager's
 * addHTMLManipulationIntercept) around the native ones, and the native ones throw SyntaxError ("The string did not match the expected pattern")
 * when the markup is not well-formed XML - which an application's own script can hit, e.g. ZDF's start application building a teaser from data.
 * The error stops the application. An HTML parser never rejects such markup, so on that error the markup is parsed as HTML and the nodes are
 * put in place by hand. */
static const char html_fix_script[] =
    "(function(){\n"
    "  function nodesOf(html){\n"
    "    var d = new DOMParser().parseFromString('<!DOCTYPE html><html><body>' + html + '</body></html>', 'text/html');\n"
    "    return Array.prototype.slice.call(d.body.childNodes).map(function(n){ return document.importNode(n, true); });\n"
    "  }\n"
    "  function isSyntax(e){ return e && e.name === 'SyntaxError'; }\n"
    "  function wrapSetter(name){\n"
    "    var d = Object.getOwnPropertyDescriptor(HTMLElement.prototype, name);\n"
    "    if (!d || !d.set) return;\n"
    "    Object.defineProperty(HTMLElement.prototype, name, { configurable: true, enumerable: d.enumerable, get: d.get, set: function(v){\n"
    "      try { d.set.call(this, v); } catch (e) {\n"
    "        if (!isSyntax(e)) throw e;\n"
    "        var nodes = nodesOf(String(v)), i;\n"
    "        if (name === 'innerHTML') {\n"
    "          while (this.firstChild) this.removeChild(this.firstChild);\n"
    "          for (i = 0; i < nodes.length; i++) this.appendChild(nodes[i]);\n"
    "        } else if (this.parentNode) {\n"
    "          for (i = 0; i < nodes.length; i++) this.parentNode.insertBefore(nodes[i], this);\n"
    "          this.parentNode.removeChild(this);\n"
    "        }\n"
    "      }\n"
    "    }});\n"
    "  }\n"
    "  wrapSetter('innerHTML');\n"
    "  wrapSetter('outerHTML');\n"
    "  var insert = HTMLElement.prototype.insertAdjacentHTML;\n"
    "  if (typeof insert === 'function') {\n"
    "    HTMLElement.prototype.insertAdjacentHTML = function(position, text){\n"
    "      try { return insert.call(this, position, text); } catch (e) {\n"
    "        if (!isSyntax(e)) throw e;\n"
    "        var nodes = nodesOf(String(text)), i, ref;\n"
    "        switch (String(position).toLowerCase()) {\n"
    "        case 'beforebegin': if (this.parentNode) for (i = 0; i < nodes.length; i++) this.parentNode.insertBefore(nodes[i], this); break;\n"
    "        case 'afterbegin': ref = this.firstChild; for (i = 0; i < nodes.length; i++) this.insertBefore(nodes[i], ref); break;\n"
    "        case 'beforeend': for (i = 0; i < nodes.length; i++) this.appendChild(nodes[i]); break;\n"
    "        case 'afterend': if (this.parentNode) { ref = this.nextSibling; for (i = 0; i < nodes.length; i++) this.parentNode.insertBefore(nodes[i], ref); } break;\n"
    "        default: throw e;\n"
    "        }\n"
    "      }\n"
    "    };\n"
    "  }\n"
    "})();\n";

/* The page's video/broadcast object decides where the live picture goes (picture in graphics). Only one that is really on screen counts:
 * not sized 0, not display:none / visibility:hidden, not made invisible by an ancestor (opacity 0), not clipped away by an ancestor with
 * overflow hidden, not outside the page. Any other object (the application may have several, one hidden) is skipped; none = whole screen. */
static const char video_rect_script[] =
    "(function(){\n"
    "  if (window.top !== window || !window.webkit || !window.webkit.messageHandlers || !window.webkit.messageHandlers.e2video) return;\n"
    "  var last = '';\n"
    "  function wpeVisibleRect(o) {\n"
    "    var b = o.getBoundingClientRect();\n"
    "    if (!(b.width > 1 && b.height > 1)) return null;\n"
    "    var cs = getComputedStyle(o);\n"
    "    if (cs.visibility === 'hidden' || cs.display === 'none') return null;\n"
    "    var l = b.left, t = b.top, r = b.right, bt = b.bottom, opacity = parseFloat(cs.opacity), pos = cs.position;\n"
    "    var clipping = pos !== 'absolute';\n"
    "    if (pos === 'fixed') clipping = false;\n"
    "    var e = o.parentElement, p, ps;\n"
    "    while (e && e !== document.body && e !== document.documentElement) {\n"
    "      ps = getComputedStyle(e);\n"
    "      if (ps.display === 'none') return null;\n"
    "      opacity *= parseFloat(ps.opacity);\n"
    "      if (!clipping && ps.position !== 'static' && pos === 'absolute') clipping = true;\n"
    "      if (clipping && (ps.overflowX !== 'visible' || ps.overflowY !== 'visible')) {\n"
    "        p = e.getBoundingClientRect();\n"
    "        if (p.width > 1 && p.height > 1) {\n"
    "          l = Math.max(l, p.left); t = Math.max(t, p.top); r = Math.min(r, p.right); bt = Math.min(bt, p.bottom);\n"
    "        }\n"
    "      }\n"
    "      e = e.parentElement;\n"
    "    }\n"
    "    if (opacity < 0.05) return null;\n"
    "    l = Math.max(l, 0); t = Math.max(t, 0);\n"
    "    r = Math.min(r, window.innerWidth || 1280); bt = Math.min(bt, window.innerHeight || 720);\n"
    "    if (r - l <= 1 || bt - t <= 1) return null;\n"
    "    return b;\n"
    "  }\n"
    "  function tick() {\n"
    "    var r = [0, 0, 0, 0], list = document.querySelectorAll('object[type=\"video/broadcast\"]'), i, b;\n"
    "    for (i = 0; i < list.length; i++) {\n"
    "      b = wpeVisibleRect(list[i]);\n"
    "      if (b) { r = [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; break; }\n"
    "    }\n"
    "    var s = r.join(',');\n"
    "    if (s !== last) { last = s; window.webkit.messageHandlers.e2video.postMessage(s); }\n"
    "  }\n"
    "  setInterval(tick, 200);\n"
    "})();\n";

/* orb://player: the page the polyfill's media element extension puts in an iframe to play audio/video (see ORB's
 * ORBWPEWebExtensionHelper::HandleORBURISchemeRequest). It is playerpage.html with the token of the page and two scripts
 * (iframe.js: the player logic that talks to the polyfill in the parent through postMessage, dash.all.min.js) inlined. */
static char *orb_read_asset(const char *name)
{
    char *path = g_build_filename(orb_dir, name, NULL), *contents = NULL;
    if (!g_file_get_contents(path, &contents, NULL, NULL))
        fprintf(stderr, "orb: cannot read %s\n", path);
    g_free(path);
    return contents ? contents : g_strdup("");
}

static void on_orb_scheme_request(WebKitURISchemeRequest *request, gpointer data)
{
    const char *uri = webkit_uri_scheme_request_get_uri(request);
    if (!g_str_has_prefix(uri, "orb://player")) {
        GError *err = g_error_new(g_quark_from_string("orb"), 404, "unknown orb URI %s", uri);
        webkit_uri_scheme_request_finish_error(request, err);
        g_error_free(err);
        return;
    }
    static char *page, *iframe_js, *dash_js;
    if (!page) {
        page = orb_read_asset("playerpage.html");
        iframe_js = orb_read_asset("iframe.js");
        dash_js = orb_read_asset("dash.all.min.js");
    }
    char *token = orb_host_create_token(uri);
    const char *head_end = strstr(page, "</head>");
    gsize head_len = head_end ? (gsize)(head_end - page) : strlen(page);
    GString *html = g_string_new(NULL);
    g_string_append_len(html, page, head_len);
    g_string_append_printf(html, "\n<script type=\"text/javascript\">\ndocument.token = %s;\n</script>\n", token && *token ? token : "{}");
    g_string_append_printf(html, "<script type=\"text/javascript\">\n%s\n</script>\n<script type=\"text/javascript\">\n%s\n</script>\n", iframe_js, dash_js);
    g_string_append(html, head_end ? head_end : "");
    free(token);
    gsize len = html->len;
    GInputStream *stream = g_memory_input_stream_new_from_data(g_string_free(html, FALSE), len, g_free);
    webkit_uri_scheme_request_finish(request, stream, len, "text/html");
    g_object_unref(stream);
    fprintf(stderr, "orb: served %s (%lu bytes)\n", uri, (unsigned long)len);
}

static gpointer orb_launch_thread(gpointer url)
{
    orb_host_launch_broadcast(url); /* a broadcast-related application of the channel the box is tuned to, if it told us (ctl "channel") */
    g_free(url);
    return NULL;
}

static gboolean on_load_failed(WebKitWebView *v, WebKitLoadEvent ev, const char *uri, GError *err, gpointer data)
{
    fprintf(stderr, "load-failed: %s: %s\n", uri, err ? err->message : "?");
    if (orb_mode)
        orb_host_load_failed(uri, err ? err->message : "");
    return FALSE; /* not handled: WebKit shows its error page */
}

/* --tolerant: WebKit's own error page is not drawn over the TV picture; the failure is only reported (above) */
static gboolean on_load_failed_tolerant(WebKitWebView *v, WebKitLoadEvent ev, const char *uri, GError *err, gpointer data)
{
    on_load_failed(v, ev, uri, err, data);
    return TRUE;
}

static char *ctl_path;
static int ctl_fd = -1, ctl_dummy_fd = -1;
static char ctl_buf[8192]; /* an AIT section as hex is up to ~2100 characters, a components line a few hundred */
static size_t ctl_len;

/* Write the current shadow canvas (what WebKit last delivered) as a PPM, alpha ignored. */
static void dump_canvas(const char *path)
{
    FILE *f = fopen(path, "wb");
    if (!f) {
        fprintf(stderr, "ctl: cannot write %s\n", path);
        return;
    }
    fprintf(f, "P6\n%d %d\n255\n", out.w, out.h);
    for (int i = 0; i < out.w * out.h; i++) {
        const guint8 *p = out.canvas + (size_t)i * 4;
        fputc(p[2], f); fputc(p[1], f); fputc(p[0], f);
    }
    fclose(f);
    fprintf(stderr, "ctl: wrote %s (%dx%d)\n", path, out.w, out.h);
}

static void ctl_handle_line(char *line)
{
    g_strstrip(line);
    if (!*line)
        return;
    if (!strncmp(line, "key ", 4)) {
        TRACE("key-in %s", line + 4);
        gboolean sent = send_key(line + 4, TRUE, TRUE);
        TRACE("key-done %s", line + 4);
        fprintf(stderr, "ctl: %s -> %s\n", line, sent ? "sent" : "unknown key");
    } else if (!strncmp(line, "keyrepeat ", 10)) {
        if (limit_repeat && key_awaits_frame && g_get_monotonic_time() - key_sent_us < REPEAT_HOLD_US) {
            repeats_dropped++;
            TRACE("key-drop %s", line + 10);
        } else {
            repeats_forwarded++;
            TRACE("key-in %s", line + 10);
            send_key(line + 10, TRUE, TRUE);
            TRACE("key-done %s", line + 10);
        }
    } else if (!strncmp(line, "channel ", 8)) {
        /* channel <onid> <tsid> <sid> <name>: the service enigma2 is tuned to; the next "app" is launched as its broadcast-related application */
        int onid, tsid, sid, used = 0;
        if (sscanf(line + 8, "%d %d %d %n", &onid, &tsid, &sid, &used) >= 3 && orb_mode)
            orb_host_set_channel(onid, tsid, sid, line + 8 + used);
        else
            fprintf(stderr, "ctl: channel needs --orb and \"<onid> <tsid> <sid> <name>\"\n");
    } else if (!strncmp(line, "service ", 8)) {
        /* service <onid> <tsid> <sid>: the service was selected (the engine waits for its AIT, see orb_host_service_selected) */
        int onid, tsid, sid;
        if (sscanf(line + 8, "%d %d %d", &onid, &tsid, &sid) == 3 && orb_mode)
            orb_host_service_selected(onid, tsid, sid);
        else
            fprintf(stderr, "ctl: service needs --orb and \"<onid> <tsid> <sid>\"\n");
    } else if (!strncmp(line, "ait ", 4)) {
        /* ait <pid> <sid> <hex>: one raw AIT section of the selected service */
        int pid, sid, used = 0;
        if (orb_mode && sscanf(line + 4, "%d %d %n", &pid, &sid, &used) >= 2 && used) {
            const char *hex = line + 4 + used;
            size_t digits = strlen(hex), length = digits / 2;
            guint8 *data = length ? g_malloc(length) : NULL;
            gboolean ok = data != NULL && digits % 2 == 0;
            for (size_t i = 0; ok && i < length; i++) {
                int hi = g_ascii_xdigit_value(hex[2 * i]), lo = g_ascii_xdigit_value(hex[2 * i + 1]);
                if (hi < 0 || lo < 0)
                    ok = FALSE;
                else
                    data[i] = (guint8)(hi << 4 | lo);
            }
            if (ok)
                orb_host_ait_section(pid, sid, data, (unsigned int)length);
            else
                fprintf(stderr, "ctl: ait: bad hex data\n");
            g_free(data);
        } else
            fprintf(stderr, "ctl: ait needs --orb and \"<pid> <sid> <hex>\"\n");
    } else if (!strcmp(line, "ait-stop")) {
        if (orb_mode)
            orb_host_broadcast_stopped();
    } else if (!strncmp(line, "components", 10) && (line[10] == ' ' || line[10] == '\0')) {
        /* components <V:...;A:...;S:...>: the streams of the tuned service as enigma2 reports them (see orb_host.h); "components" alone = none */
        if (orb_mode)
            orb_host_set_components(line[10] ? line + 11 : "");
        else
            fprintf(stderr, "ctl: components needs --orb\n");
    } else if (!strncmp(line, "mark ", 5)) {
        TRACE("mark %s", line + 5);
    } else if (!strncmp(line, "dump ", 5)) {
        dump_canvas(line + 5);
    } else if (!strncmp(line, "keydown ", 8)) {
        send_key(line + 8, TRUE, FALSE);
    } else if (!strncmp(line, "keyup ", 6)) {
        send_key(line + 6, FALSE, TRUE);
    } else if (!strncmp(line, "load ", 5)) {
        fprintf(stderr, "ctl: load %s\n", line + 5);
        webkit_web_view_load_uri(view, line + 5);
    } else if (!strncmp(line, "app ", 4)) {
        fprintf(stderr, "ctl: launch application %s\n", line + 4);
        if (orb_mode)
            g_thread_unref(g_thread_new("orb-launch", orb_launch_thread, g_strdup(line + 4))); /* the engine fetches the URL: no blocking the loop */
        else
            fprintf(stderr, "ctl: app needs --orb\n");
    } else if (!strcmp(line, "decoder-free")) {
        handover_decoder_free();
    } else if (!strcmp(line, "quit")) {
        fprintf(stderr, "ctl: quit\n");
        g_main_loop_quit(loop);
    } else
        fprintf(stderr, "ctl: unknown command '%s'\n", line);
}

/* --trace: a 50 ms timer that reports when the main loop did not get to it in time (something blocked the loop for that long). */
static gint64 watchdog_last_us;
static gboolean loop_watchdog(gpointer data)
{
    gint64 now = g_get_monotonic_time();
    if (watchdog_last_us && now - watchdog_last_us > 150000)
        TRACE("loop-late %.0f ms", (now - watchdog_last_us - 50000) / 1000.0);
    watchdog_last_us = now;
    return G_SOURCE_CONTINUE;
}

static gboolean ctl_readable(GIOChannel *source, GIOCondition cond, gpointer data)
{
    ssize_t n = read(ctl_fd, ctl_buf + ctl_len, sizeof ctl_buf - ctl_len - 1);
    if (n <= 0)
        return G_SOURCE_CONTINUE;
    ctl_len += (size_t)n;
    ctl_buf[ctl_len] = 0;
    char *start = ctl_buf, *nl;
    while ((nl = strchr(start, '\n'))) {
        *nl = 0;
        ctl_handle_line(start);
        start = nl + 1;
    }
    ctl_len = strlen(start);
    memmove(ctl_buf, start, ctl_len + 1);
    if (ctl_len >= sizeof ctl_buf - 1)
        ctl_len = 0; /* runaway line: drop it */
    return G_SOURCE_CONTINUE;
}

static void ctl_open(const char *path)
{
    ctl_path = g_strdup(path);
    unlink(path);
    if (mkfifo(path, 0600) < 0) {
        perror("mkfifo");
        return;
    }
    ctl_fd = open(path, O_RDONLY | O_NONBLOCK);
    ctl_dummy_fd = open(path, O_WRONLY | O_NONBLOCK); /* keeps the FIFO open so reads don't hit EOF between writers */
    GIOChannel *ch = g_io_channel_unix_new(ctl_fd);
    g_io_add_watch(ch, G_IO_IN, ctl_readable, NULL);
    fprintf(stderr, "control channel ready: %s\n", path);
}

static gboolean on_web_process_terminated(WebKitWebView *v, WebKitWebProcessTerminationReason r, gpointer data)
{
    fprintf(stderr, "WEB PROCESS TERMINATED (reason %d)\n", (int)r);
    if (tolerant) {
        /* --tolerant: a crashed page is loaded again instead of ending the whole browser; not more than 3 times in a minute, then give up */
        static gint64 stamps[3];
        const gint64 now = g_get_monotonic_time();
        if (now - stamps[0] > 60 * G_USEC_PER_SEC) {
            stamps[0] = stamps[1];
            stamps[1] = stamps[2];
            stamps[2] = now;
            fprintf(stderr, "--tolerant: reloading the page\n");
            webkit_web_view_reload(v);
            return TRUE;
        }
    }
    g_main_loop_quit(loop);
    return TRUE;
}

static gboolean on_quit(gpointer data)
{
    g_main_loop_quit(loop);
    return G_SOURCE_REMOVE;
}

static void set_fine_damage(WebKitSettings *settings)
{
    WebKitFeatureList *all = webkit_settings_get_all_features();
    gboolean found = FALSE;
    for (gsize i = 0; i < webkit_feature_list_get_length(all); i++) {
        WebKitFeature *f = webkit_feature_list_get(all, i);
        if (!g_strcmp0(webkit_feature_get_identifier(f), "UnifyDamagedRegions")) {
            webkit_settings_set_feature_enabled(settings, f, FALSE);
            found = TRUE;
        }
    }
    webkit_feature_list_unref(all);
    fprintf(stderr, "fine-grained damage: %s\n", found ? "UnifyDamagedRegions turned off" : "feature not found");
}

int main(int argc, char **argv)
{
    int W = 1280, H = 720, secs = 10;
    const char *page = NULL, *dump = NULL, *dump_out = NULL;
    gboolean fullscreen = FALSE;
    double hidpi = 0;
    out.scale = 1.0;
    gboolean want_fb = FALSE, fine = FALSE, transparent = FALSE, info = FALSE, hwvideo = FALSE;
    int px = -1, py = -1;
    int probe_x[8], probe_y[8], nprobes = 0;
    gboolean force_premul = FALSE, no_guard = FALSE;
    const char *ctl = NULL, *evt_arg = NULL;
    out.unpremul_from_y = -1; /* off unless --unpremul-from-y is given */

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--size") && i + 1 < argc) sscanf(argv[++i], "%dx%d", &W, &H);
        else if (!strcmp(argv[i], "--secs") && i + 1 < argc) secs = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--fb")) want_fb = TRUE;
        else if (!strcmp(argv[i], "--pos") && i + 1 < argc) sscanf(argv[++i], "%d,%d", &px, &py);
        else if (!strcmp(argv[i], "--unpremul")) out.unpremul = TRUE;
        else if (!strcmp(argv[i], "--premul")) force_premul = TRUE;
        else if (!strcmp(argv[i], "--no-guard")) no_guard = TRUE;
        else if (!strcmp(argv[i], "--overlay-png") && i + 1 < argc) out.png_prefix = g_strdup(argv[++i]);
        else if (!strcmp(argv[i], "--ctl") && i + 1 < argc) ctl = argv[++i];
        else if (!strcmp(argv[i], "--opaque")) out.opaque = TRUE;
        else if (!strcmp(argv[i], "--unpremul-from-y") && i + 1 < argc) out.unpremul_from_y = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--dump") && i + 1 < argc) dump = argv[++i];
        else if (!strcmp(argv[i], "--dump-out") && i + 1 < argc) dump_out = argv[++i];
        else if (!strcmp(argv[i], "--scale") && i + 1 < argc) out.scale = atof(argv[++i]);
        else if (!strcmp(argv[i], "--fullscreen")) fullscreen = TRUE;
        else if (!strcmp(argv[i], "--egl-sink")) out.egl_on = TRUE;
        else if (!strcmp(argv[i], "--egl-screen") && i + 1 < argc) sscanf(argv[++i], "%dx%d", &egl_screen_w, &egl_screen_h);
        else if (!strcmp(argv[i], "--hidpi") && i + 1 < argc) hidpi = atof(argv[++i]);
        else if (!strcmp(argv[i], "--fine-damage")) fine = TRUE;
        else if (!strcmp(argv[i], "--transparent")) transparent = TRUE;
        else if (!strcmp(argv[i], "--probe") && i + 1 < argc && nprobes < 8) { sscanf(argv[++i], "%d,%d", &probe_x[nprobes], &probe_y[nprobes]); nprobes++; }
        else if (!strcmp(argv[i], "--info")) info = TRUE;
        else if (!strcmp(argv[i], "--orb") && i + 1 < argc) { orb_dir = argv[++i]; orb_mode = TRUE; }
        else if (!strcmp(argv[i], "--hwvideo")) hwvideo = TRUE;
        else if (!strcmp(argv[i], "--hwvideo-lazy")) { hwvideo = TRUE; hw_lazy = TRUE; }
        else if (!strcmp(argv[i], "--evt") && i + 1 < argc) evt_arg = argv[++i];
        else if (!strcmp(argv[i], "--trace")) trace = TRUE;
        else if (!strcmp(argv[i], "--fast-anim")) fast_anim = TRUE;
        else if (!strcmp(argv[i], "--tolerant")) tolerant = TRUE;
        else if (!strcmp(argv[i], "--scrollbars")) show_scrollbars = TRUE;
        else if (!strcmp(argv[i], "--persist") && i + 1 < argc) persist_dir = argv[++i];
        else if (!strcmp(argv[i], "--no-debug-js")) no_debug_js = TRUE;
        else if (!strcmp(argv[i], "--limit-repeat")) limit_repeat = TRUE;
        else if (!strcmp(argv[i], "--autoplay") && i + 1 < argc) autoplay_deny = !strcmp(argv[++i], "deny");
        else if (argv[i][0] != '-') page = argv[i];
        else { fprintf(stderr, "unknown option %s\n", argv[i]); return 2; }
    }

    if (info) {
        if (!fb_open())
            return 1;
        return 0;
    }
    if (!page && !orb_mode) {
        fprintf(stderr, "usage: wpe_fb [--fb] [--secs N] [--size WxH] [--pos X,Y] [--fine-damage] [--transparent] [--dump f.ppm] <page>\n");
        return 2;
    }

    out.w = W;
    out.h = H;
    if (hidpi > 1.0) {
        out.w = (int)(W * hidpi + 0.5); /* buffers of a view with scale F are F times the logical size */
        out.h = (int)(H * hidpi + 0.5);
    }
    if (out.egl_on) {
        /* gEGLDC does the scaling on the GPU: the view is delivered 1:1 whatever --scale/--fb were given */
        if (want_fb || out.scale != 1.0)
            fprintf(stderr, "--egl-sink: --fb/--scale/--fullscreen ignored (the frame goes to enigma2 unscaled)\n");
        want_fb = FALSE;
        fullscreen = FALSE;
        out.scale = 1.0;
        if (!egl_open())
            return 1;
    } else
        out.canvas = g_malloc0((size_t)out.w * out.h * 4);
    if (!want_fb && fullscreen)
        fprintf(stderr, "--fullscreen needs --fb: ignored\n");
    if (!want_fb) {
        /* no framebuffer: the layout of the pixels an output dump is written in */
        out.vinfo.red.offset = 16; out.vinfo.green.offset = 8; out.vinfo.blue.offset = 0; out.vinfo.transp.offset = 24;
        out.vinfo.bits_per_pixel = 32;
    }

    if (want_fb) {
        /* straight alpha is what this display expects (verified on a DM900, and it's what nxpl_try_open()'s own RGBA pixel layout also expects
         * via convert_pixels()): convert unless told otherwise */
        if (!force_premul)
            out.unpremul = TRUE;
        /* On a Nexus-middleware box (GigaBlue), /dev/fb0 is not connected to the real display scanout at all - confirmed on real hardware
         * 2026-09-23 (see the "NXPL/Nexus presentation backend" section above and project memory project_hbbtv2_wpe_orb_plan.md). Try that path
         * first: nxpl_try_open() is a pure runtime probe (dlopen/dlsym only) that safely returns FALSE, with nothing left registered, on any box
         * without Nexus - the DM900 falls straight through to the existing /dev/fb0 path, completely unchanged. --overlay-png always uses the
         * /dev/fb0-based PNG-dump path (a different embedding style entirely, for the enigma2 skin-overlay plugin mode) regardless of platform. */
        if (!out.png_prefix && nxpl_try_open())
            out.nxpl_on = TRUE;
        else if (fb_open())
            out.fb_on = TRUE;
        else
            return 1;
        if (fullscreen) /* the whole screen, in the view's aspect ratio */
            out.scale = MIN((double)out.vinfo.xres / out.w, (double)out.vinfo.yres / out.h);
        out.ow = (int)(out.w * out.scale + 0.5);
        out.oh = (int)(out.h * out.scale + 0.5);
        out.fb_x = px >= 0 ? px : ((int)out.vinfo.xres - out.ow) / 2;
        out.fb_y = py >= 0 ? py : ((int)out.vinfo.yres - out.oh) / 2;
        if (out.fb_on)
            fb_save_area();
        out.conv = g_malloc0((size_t)out.ow * out.oh * 4);
        if (out.nxpl_on && !nxpl_finish_open()) {
            fprintf(stderr, "nxpl: window/EGL setup failed after a successful register - aborting (see the messages above)\n");
            nxpl_close();
            return 1;
        }
        if (out.png_prefix) {
            /* the pixels are made straight-alpha ARGB whatever the framebuffer's own format, and the framebuffer is left alone */
            out.vinfo.red.offset = 16; out.vinfo.green.offset = 8; out.vinfo.blue.offset = 0; out.vinfo.transp.offset = 24;
            out.unpremul = TRUE;
            no_guard = TRUE;
            fprintf(stderr, "overlay: the frame goes to %s_<n>.png, not to the framebuffer\n", out.png_prefix);
        }
        /* the guard (repairing pages enigma2 flips/overwrites under us) is a /dev/fb0-only concern: an nxpl window is Nexus's own, exclusively
         * registered surface - nothing else should be drawing into it */
        out.guard = out.fb_on && !no_guard;
        out.last_page = out.fb_on ? out.vinfo.yoffset / out.vinfo.yres : 0;
        if (out.nxpl_on)
            fprintf(stderr, "nxpl: frame goes to an NXPL Nexus window at (%d,%d), %dx%d (scale %.3f)\n", out.fb_x, out.fb_y, out.ow, out.oh, out.scale);
        else
            fprintf(stderr, "fb: frame goes to (%d,%d), %dx%d (scale %.3f)\n", out.fb_x, out.fb_y, out.ow, out.oh, out.scale);
    } else {
        out.ow = (int)(out.w * out.scale + 0.5);
        out.oh = (int)(out.h * out.scale + 0.5);
        if (out.scale != 1.0 && dump_out) /* only a scaled dump needs the converted output */
            out.conv = g_malloc0((size_t)out.ow * out.oh * 4);
    }
    if (out.scale != 1.0)
        scale_setup();

    if (hwvideo) {
        plane_save();
        /* Media plays through the Dreambox GStreamer quirks (dvbvideosink/dvbaudiosink); the page is punched transparent
         * where a <video> sits and the web process positions the hardware video plane from the element's rectangle. It
         * has to know where this view is on the screen. Set before any web process is spawned. */
        char origin[32], screen[32];
        char scale_text[16];
        snprintf(scale_text, sizeof scale_text, "%.6f", out.egl_on ? (double)egl_screen_w / out.w : out.scale);
        g_setenv("WPE_DREAM_VIDEO_SCALE", scale_text, TRUE); /* a <video> rectangle is in view pixels, the screen is `scale` times larger */
        snprintf(origin, sizeof origin, "%d,%d", (out.fb_on || out.nxpl_on) ? out.fb_x : 0, (out.fb_on || out.nxpl_on) ? out.fb_y : 0);
        snprintf(screen, sizeof screen, "%dx%d", (out.fb_on || out.nxpl_on) ? (int)out.vinfo.xres : egl_screen_w, (out.fb_on || out.nxpl_on) ? (int)out.vinfo.yres : egl_screen_h);
        g_setenv("WEBKIT_GST_QUIRKS", "dreambox", TRUE);
        g_setenv("WEBKIT_GST_HOLE_PUNCH_QUIRK", "dreambox", TRUE);
        g_setenv("WPE_DREAM_VIDEO_ORIGIN", origin, TRUE);
        g_setenv("WPE_DREAM_VIDEO_SCREEN", screen, TRUE);
        fprintf(stderr, "hwvideo: video plane origin %s on a %s screen\n", origin, screen);
        if (hw_lazy)
            handover_start(evt_arg);
    }

    display_type = register_subclass(WPE_TYPE_DISPLAY, "WPEDisplayFb", display_class_init);
    toplevel_type = register_subclass(WPE_TYPE_TOPLEVEL, "WPEToplevelFb", toplevel_class_init);
    view_type = register_subclass(WPE_TYPE_VIEW, "WPEViewFb", view_class_init);

    /* WebKit picks its display with wpe_display_get_default(), which instantiates the highest-priority
     * implementation registered on the "wpe-platform-display" extension point (the built-in headless one has -100). */
    GError *error = NULL;
    GIOExtensionPoint *ext_point = g_io_extension_point_register(WPE_DISPLAY_EXTENSION_POINT_NAME);
    g_io_extension_point_set_required_type(ext_point, WPE_TYPE_DISPLAY);
    g_io_extension_point_implement(WPE_DISPLAY_EXTENSION_POINT_NAME, display_type, "fb", 1000);
    WPEDisplay *display = wpe_display_get_default();
    if (!display) {
        fprintf(stderr, "no WPE display available\n");
        return 3;
    }
    wpe_display_set_primary(display);

    WebKitNetworkSession *session;
    if (persist_dir) {
        /* what a TV does: the HTTP cache, cookies and local storage of the applications survive between runs (the cache is a
         * "document browser" cache: at most 75 MB; the default "web browser" model would take up to 1 GB of the disk) */
        char *data_dir = g_build_filename(persist_dir, "data", NULL), *cache_dir = g_build_filename(persist_dir, "cache", NULL);
        if (g_mkdir_with_parents(data_dir, 0700) == 0 && g_mkdir_with_parents(cache_dir, 0700) == 0) {
            webkit_web_context_set_cache_model(webkit_web_context_get_default(), WEBKIT_CACHE_MODEL_DOCUMENT_BROWSER);
            session = webkit_network_session_new(data_dir, cache_dir);
            /* cookies are kept in memory unless a file is named */
            char *cookie_file = g_build_filename(data_dir, "cookies.sqlite", NULL);
            webkit_cookie_manager_set_persistent_storage(webkit_network_session_get_cookie_manager(session), cookie_file, WEBKIT_COOKIE_PERSISTENT_STORAGE_SQLITE);
            g_free(cookie_file);
            fprintf(stderr, "persistent session: data %s, cache %s\n", data_dir, cache_dir);
        } else {
            fprintf(stderr, "cannot create %s: the session is not persistent\n", persist_dir);
            session = webkit_network_session_new_ephemeral();
        }
        g_free(data_dir);
        g_free(cache_dir);
    } else
        session = webkit_network_session_new_ephemeral();
    if (tolerant) {
        /* A set-top box's certificate store is old and its clock is often wrong: an HTTPS application whose certificate "fails" would not load at all */
        webkit_network_session_set_tls_errors_policy(session, WEBKIT_TLS_ERRORS_POLICY_IGNORE);
        fprintf(stderr, "--tolerant: TLS certificate errors are ignored\n");
    }
    /* A TV has no pointer and no "user gesture": HbbTV applications start audio/video by themselves, with sound. WebKit's
     * default autoplay policy is ALLOW_WITHOUT_SOUND, which rejects play() of any clip that has an audio track
     * (NotAllowedError). (The media-playback-requires-user-gesture setting is off by default and is not what blocks it.) */
    /* --autoplay deny (measurements): no media starts by itself, so that a preview does not software-decode on the box's two cores */
    WebKitWebsitePolicies *policies = webkit_website_policies_new_with_policies("autoplay", autoplay_deny ? WEBKIT_AUTOPLAY_DENY : WEBKIT_AUTOPLAY_ALLOW, NULL);
    if (orb_mode) {
        /* web process extension (bridge to the engine) + the ORB polyfill as a document-start user script */
        char *ext_dir = g_build_filename(orb_dir, "ext", NULL);
        WebKitWebContext *wctx = webkit_web_context_get_default();
        webkit_web_context_set_web_process_extensions_directory(wctx, ext_dir);
        g_free(ext_dir);
        webkit_security_manager_register_uri_scheme_as_cors_enabled(webkit_web_context_get_security_manager(wctx), "orb");
        webkit_web_context_register_uri_scheme(wctx, "orb", on_orb_scheme_request, NULL, NULL);

        orb_socket = g_strdup_printf("/tmp/orb_bridge.%d.sock", (int)getpid());
        webkit_web_context_set_web_process_extensions_initialization_user_data(wctx, g_variant_new_string(orb_socket));

        WebKitUserContentManager *ucm = webkit_user_content_manager_new();
        {
            /* optional: DIR/debug.js forwards the page's console, errors and media events to this process's log; it goes
             * first so that it also sees what the polyfill and the application do at load time */
            char *debug_path = g_build_filename(orb_dir, "debug.js", NULL), *debug_source = NULL;
            if (!no_debug_js && g_file_get_contents(debug_path, &debug_source, NULL, NULL)) {
                /* all frames: the polyfill's player runs in an orb://player iframe (frames without wpeBridge skip it) */
                WebKitUserScript *debug_script = webkit_user_script_new(debug_source, WEBKIT_USER_CONTENT_INJECT_ALL_FRAMES, WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, NULL, NULL);
                webkit_user_content_manager_add_script(ucm, debug_script);
                webkit_user_script_unref(debug_script);
                g_free(debug_source);
            }
            g_free(debug_path);
        }
        if (!show_scrollbars) {
            /* A TV shows no scrollbars: applications are operated with the remote and scroll themselves. One that overflows the view for
             * a moment while it starts (the ZDF main application) would flash WebKit's default ones. */
            static const char css[] = "html, body, * { scrollbar-width: none !important; } ::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }";
            WebKitUserStyleSheet *sheet = webkit_user_style_sheet_new(css, WEBKIT_USER_CONTENT_INJECT_ALL_FRAMES, WEBKIT_USER_STYLE_LEVEL_USER, NULL, NULL);
            webkit_user_content_manager_add_style_sheet(ucm, sheet);
            webkit_user_style_sheet_unref(sheet);
        }
        if (fast_anim) {
            /* every animation/transition step is a frame that has to be read back from the GPU (slow): make them (nearly) instant.
             * 1 ms, not "none", so that transitionend/animationend still fire and applications waiting for them do not hang */
            static const char css[] = "*, *::before, *::after { transition-duration: 1ms !important; transition-delay: 0s !important; "
                                      "animation-duration: 1ms !important; animation-delay: 0s !important; scroll-behavior: auto !important; }";
            WebKitUserStyleSheet *sheet = webkit_user_style_sheet_new(css, WEBKIT_USER_CONTENT_INJECT_ALL_FRAMES, WEBKIT_USER_STYLE_LEVEL_USER, NULL, NULL);
            webkit_user_content_manager_add_style_sheet(ucm, sheet);
            webkit_user_style_sheet_unref(sheet);
        }
        if (out.egl_on) {
            /* the live picture belongs into the application's video/broadcast object (see apply_video_rect) */
            webkit_user_content_manager_register_script_message_handler(ucm, "e2video", NULL);
            g_signal_connect(ucm, "script-message-received::e2video", G_CALLBACK(on_video_rect_message), NULL);
            WebKitUserScript *vr = webkit_user_script_new(video_rect_script, WEBKIT_USER_CONTENT_INJECT_TOP_FRAME, WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, NULL, NULL);
            webkit_user_content_manager_add_script(ucm, vr);
            webkit_user_script_unref(vr);
        }
        char *js_path = g_build_filename(orb_dir, "hbbtv.js", NULL), *source = NULL;
        if (!g_file_get_contents(js_path, &source, NULL, &error)) {
            fprintf(stderr, "cannot read the polyfill %s: %s\n", js_path, error->message);
            return 1;
        }
        WebKitUserScript *script = webkit_user_script_new(source, WEBKIT_USER_CONTENT_INJECT_TOP_FRAME, WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, NULL, NULL);
        webkit_user_content_manager_add_script(ucm, script);
        webkit_user_script_unref(script);
        {
            WebKitUserScript *fix = webkit_user_script_new(html_fix_script, WEBKIT_USER_CONTENT_INJECT_TOP_FRAME, WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, NULL, NULL);
            webkit_user_content_manager_add_script(ucm, fix); /* after the polyfill: wraps what it set up */
            webkit_user_script_unref(fix);
        }
        g_free(source);
        g_free(js_path);
        view = g_object_new(WEBKIT_TYPE_WEB_VIEW, "network-session", session, "user-content-manager", ucm, "website-policies", policies, NULL);
    } else
        view = g_object_new(WEBKIT_TYPE_WEB_VIEW, "network-session", session, "website-policies", policies, NULL);
    g_signal_connect(view, "load-failed", G_CALLBACK(tolerant ? on_load_failed_tolerant : on_load_failed), NULL);
    if (fine) { /* replaces the view's settings object: everything else has to be applied to the result, after this */
        WebKitSettings *settings = webkit_settings_new();
        set_fine_damage(settings);
        webkit_web_view_set_settings(view, settings);
        g_object_unref(settings);
    }
    {
        /* A TV has no pointer and no "user gesture": HbbTV applications start audio/video by themselves (with sound), which
         * WebKit's default autoplay policy would refuse (play() rejected with NotAllowedError). */
        WebKitSettings *media_settings = webkit_web_view_get_settings(view);
        webkit_settings_set_media_playback_requires_user_gesture(media_settings, FALSE);
        webkit_settings_set_media_playback_allows_inline(media_settings, TRUE);
        if (orb_mode)
            webkit_settings_set_enable_write_console_messages_to_stdout(media_settings, TRUE);
        if (fast_anim)
            webkit_settings_set_enable_smooth_scrolling(media_settings, FALSE);
        /* WPE_HW_ACCEL=never: composite the page in software and deliver plain shared-memory buffers. The 256 MB GPU heap of the box
         * is shared with enigma2; with GPU compositing WebKit allocates a texture per tile and layer, and on heavy animated pages
         * the heap runs out (dmesg: BMMA_Alloc failed / nexus_memory: Out of memory) - tiles are then lost = black rectangles. */
        const char *accel = g_getenv("WPE_HW_ACCEL");
        if (accel && !g_strcmp0(accel, "never")) {
            if (g_object_class_find_property(G_OBJECT_GET_CLASS(media_settings), "hardware-acceleration-policy")) {
                g_object_set(media_settings, "hardware-acceleration-policy", 2 /* WEBKIT_HARDWARE_ACCELERATION_POLICY_NEVER */, NULL);
                fprintf(stderr, "hardware acceleration: never (software compositing, no GPU memory for page layers)\n");
            } else
                fprintf(stderr, "hardware acceleration: this WebKit has no such setting, WPE_HW_ACCEL ignored\n");
        }
    }
    if (transparent) {
        WebKitColor color = { 0, 0, 0, 0 };
        webkit_web_view_set_background_color(view, &color);
    }
    g_signal_connect(view, "load-changed", G_CALLBACK(on_load_changed), NULL);
    g_signal_connect(view, "notify::title", G_CALLBACK(on_title_changed), NULL);
    g_signal_connect(view, "web-process-terminated", G_CALLBACK(on_web_process_terminated), NULL);

    WPEView *wv = webkit_web_view_get_wpe_view(view);
    {
        WPEDisplay *used = webkit_web_view_get_display(view);
        fprintf(stderr, "platform in use: display %s (ours: %s, primary: %s), view %s\n", used ? G_OBJECT_TYPE_NAME(used) : "(none)",
            G_OBJECT_TYPE_NAME(display), wpe_display_get_primary() ? G_OBJECT_TYPE_NAME(wpe_display_get_primary()) : "(none)",
            wv ? G_OBJECT_TYPE_NAME(wv) : "(none)");
    }
    WPEToplevel *top = wv ? wpe_view_get_toplevel(wv) : NULL;
    if (top)
        wpe_toplevel_resize(top, W, H);
    if (top && hidpi > 1.0) {
        wpe_toplevel_scale_changed(top, hidpi);
        fprintf(stderr, "hidpi: view %dx%d at scale %.2f renders %dx%d\n", W, H, hidpi, out.w, out.h);
    }
    if (wv) {
        wpe_view_set_visible(wv, TRUE);
        wpe_view_map(wv);
        wpe_view_focus_in(wv); /* the page must have focus to receive key events */
    }

    loop = g_main_loop_new(NULL, FALSE);
    g_unix_signal_add(SIGINT, on_quit, NULL);
    g_unix_signal_add(SIGTERM, on_quit, NULL);
    if (secs > 0)
        g_timeout_add_seconds(secs, on_quit, NULL);
    if (ctl)
        ctl_open(ctl);
    if (out.fb_on && out.guard)
        g_timeout_add(10, guard_tick, NULL);
    if (trace)
        g_timeout_add(50, loop_watchdog, NULL);

    if (orb_mode) {
        OrbHostCallbacks cb = { orb_cb_eval_js, orb_cb_load_url, orb_cb_set_visible, orb_cb_exit_pressed, NULL };
        int rc = orb_host_start(orb_socket, &cb);
        if (rc) {
            fprintf(stderr, "orb_host_start failed (%d)\n", rc);
            fb_restore_area();
            return 4;
        }
        orb_host_set_component_handler(orb_cb_select_component, NULL);
        g_timeout_add(300, keyset_tick, NULL);
        {
            /* HbbTV servers sniff the user agent for the "HbbTV/x.y.z (...)" token the engine provides */
            WebKitSettings *settings = webkit_web_view_get_settings(view);
            char *ua = g_strdup_printf("%s %s", webkit_settings_get_user_agent(settings), orb_host_user_agent());
            webkit_settings_set_user_agent(settings, ua);
            fprintf(stderr, "orb: user agent: %s\n", ua);
            g_free(ua);
        }
        if (page) /* a start application given on the command line */
            g_thread_unref(g_thread_new("orb-launch", orb_launch_thread, g_strdup(page)));
        else
            webkit_web_view_load_html(view, "<html><body></body></html>", "about:blank");
    } else if (g_str_has_prefix(page, "http://") || g_str_has_prefix(page, "https://") || g_str_has_prefix(page, "file://"))
        webkit_web_view_load_uri(view, page);
    else {
        char *html = NULL;
        if (!g_file_get_contents(page, &html, NULL, &error)) {
            fprintf(stderr, "cannot read %s: %s\n", page, error->message);
            fb_restore_area();
            return 1;
        }
        webkit_web_view_load_html(view, html, "about:blank");
        g_free(html);
    }

    g_main_loop_run(loop);

    if (out.fb_on && !out.png_prefix) {
        /* Verify the blit: read the framebuffer region back and compare it with the shadow canvas. */
        fb_read_var();
        const int page = out.vinfo.yoffset;
        long differ = 0, compared = 0, first_x = -1, first_y = -1;
        for (int row = 0; row < out.h; row++) {
            int fy = out.fb_y + row;
            if (fy < 0 || fy >= (int)out.vinfo.yres)
                continue;
            for (int col = 0; col < out.w; col++) {
                int fx = out.fb_x + col;
                if (fx < 0 || fx >= (int)out.vinfo.xres)
                    continue;
                const guint32 fbpx = *(const guint32 *)(out.fb_map + (size_t)(page + fy) * out.finfo.line_length + (size_t)fx * 4);
                const guint32 cvpx = out.conv[(size_t)row * out.w + col];
                compared++;
                if (fbpx != cvpx) {
                    if (!differ) { first_x = col; first_y = row; }
                    differ++;
                }
            }
        }
        fprintf(stderr, "== fb check: visible page yoffset %d (was %d); %ld of %ld pixels differ from the shadow canvas%s\n", page,
            out.fb_saved_yoffset, differ, compared, differ ? "" : "  -> the frame is on the framebuffer");
        if (differ)
            fprintf(stderr, "   first difference at frame pixel (%ld,%ld)\n", first_x, first_y);
    }

    if (out.fb_on && !out.png_prefix)
        fprintf(stderr, "== guard: %s; %llu page flips seen, %llu repaints (%llu without a flip = overwritten in place), slowest repaint %.1f ms\n",
            out.guard ? "on" : "off", (unsigned long long)out.flips, (unsigned long long)out.repaints, (unsigned long long)out.overwrites, out.max_repaint_ms);

    if (repeats_forwarded || repeats_dropped)
        fprintf(stderr, "== keys: %llu auto-repeats forwarded, %llu dropped while the page was still answering the previous key\n",
            (unsigned long long)repeats_forwarded, (unsigned long long)repeats_dropped);
    if (orb_mode)
        orb_host_stop();
    if (hw_lazy)
        handover_stop();
    plane_restore();
    fb_restore_area();
    if (ctl_path)
        unlink(ctl_path);

    double secs_run = out.frames > 1 ? (out.last_us - out.first_us) / 1e6 : 0;
    fprintf(stderr, "== sink: %llu frames delivered, %.1f fps, %.0f px/frame average (%.1f%% of the view), %.2f ms per frame copy\n",
        (unsigned long long)out.frames, secs_run > 0 ? (out.frames - 1) / secs_run : 0.0, out.frames ? (double)out.pixels / out.frames : 0.0,
        out.frames ? 100.0 * out.pixels / out.frames / ((double)out.w * out.h) : 0.0, out.frames ? out.blit_ms / out.frames : 0.0);
    if (out.nxpl_on && nxpl_present_count)
        fprintf(stderr, "== nxpl: %llu presents, %.2f ms avg upload+draw (CPU-side GL submission), %.2f ms avg eglSwapBuffers\n",
            (unsigned long long)nxpl_present_count, nxpl_upload_ms / nxpl_present_count, nxpl_swap_ms / nxpl_present_count);
    if (out.nxpl_on && nxpl_present_count)
        fprintf(stderr, "== nxpl: upload+draw min %.2f ms (%ld px, %dx%d), max %.2f ms (%ld px, %dx%d)\n",
            nxpl_upload_ms_min, nxpl_upload_px_at_min, nxpl_upload_w_at_min, nxpl_upload_h_at_min,
            nxpl_upload_ms_max, nxpl_upload_px_at_max, nxpl_upload_w_at_max, nxpl_upload_h_at_max);

    {
        /* alpha statistics of the final canvas (premultiplied BGRA as delivered by WPE) */
        long a0 = 0, amid = 0, a255 = 0;
        for (long i = 0; i < (long)out.w * out.h; i++) {
            guint8 a = out.canvas[i * 4 + 3];
            if (a == 0) a0++; else if (a == 255) a255++; else amid++;
        }
        fprintf(stderr, "== alpha: %ld px fully transparent (%.1f%%), %ld partial (%.1f%%), %ld opaque (%.1f%%)\n", a0, 100.0 * a0 / ((double)W * H), amid,
            100.0 * amid / ((double)W * H), a255, 100.0 * a255 / ((double)W * H));
        for (int i = 0; i < nprobes; i++) {
            const guint8 *p = out.canvas + ((size_t)probe_y[i] * W + probe_x[i]) * 4;
            fprintf(stderr, "   probe (%d,%d): B=%u G=%u R=%u A=%u\n", probe_x[i], probe_y[i], p[0], p[1], p[2], p[3]);
        }
    }

    if (dump_out && out.conv) { /* the scaled output as it goes to the framebuffer (straight alpha, alpha ignored) */
        FILE *f = fopen(dump_out, "wb");
        if (f) {
            fprintf(f, "P6\n%d %d\n255\n", out.ow, out.oh);
            for (long i = 0; i < (long)out.ow * out.oh; i++) {
                guint32 px = out.conv[i];
                fputc((px >> out.vinfo.red.offset) & 0xff, f); fputc((px >> out.vinfo.green.offset) & 0xff, f); fputc((px >> out.vinfo.blue.offset) & 0xff, f);
            }
            fclose(f);
            fprintf(stderr, "wrote %s (%dx%d)\n", dump_out, out.ow, out.oh);
        }
    }
    if (dump) {
        FILE *f = fopen(dump, "wb");
        if (f) {
            fprintf(f, "P6\n%d %d\n255\n", out.w, out.h);
            for (int i = 0; i < out.w * out.h; i++) {
                const guint8 *p = out.canvas + (size_t)i * 4;
                fputc(p[2], f); fputc(p[1], f); fputc(p[0], f);
            }
            fclose(f);
            fprintf(stderr, "wrote %s\n", dump);
        }
    }
    return 0;
}
