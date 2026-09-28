import time
import math
import M5
from M5 import *
import machine

# ---------------------------------------------------------------------------
# Audio frequency spectrum (FFT line chart) from the built-in microphone
#   Cardputer K132 / K132-V11
#   mic : SPM1423 PDM, DAT=G46, CLK=G43 (official schematic K132-V11)
#   amp : NS4168 I2S, BCLK=G41, LRCK=G43, SDATA=G42 - the amp shares G43 with
#         the mic clock, which is why the speaker is torn down before every
#         Mic.begin() and why a speaker-less boot is required.
#   panel: ST7789V2 240x135, landscape via rotation 1
# ---------------------------------------------------------------------------

RATE = 8000                 # 8 kHz: documented Mic sample rate -> 0..4 kHz
CAP_N = 512                 # samples per captured block (64 ms)
FFT_N = 256                 # 32 ms analysis window, 31.25 Hz per bin
CAP_OFF = CAP_N - FFT_N     # analyse the newest half of the block
DF = RATE / FFT_N
BIN_FIRST = 2               # skip DC and rumble below ~62 Hz
BIN_LAST = FFT_N // 2 - 1

BARS = 40
BAR_W = 6                   # 40 * 6 = 240 px, contiguous columns
SPECT_TOP = 16
BASE_Y = 94
SPECT_H = BASE_Y - SPECT_TOP
AXIS_Y = 96                 # frequency scale row (static)
SEP_Y = 112
INFO_Y = 116
METER_Y = 118
METER_X0 = 92
METER_X1 = 180
HDR_X0 = 116                # left edge of the repainted part of the header

F_LO = 60.0
F_HI = 4000.0
LOG_SPAN = math.log(F_HI / F_LO)
DB_RANGE = 55.0             # displayed dynamic range in dB
DB_TOP_MIN = -42.0          # never zoom in harder than this
PEAK_DECAY = 45.0           # peak-hold release, dB per second
GATE_DB = -78.0

MIC_DAT = 46
LIVE_DISTINCT = 16          # a constant driver value can never reach this
DEAD_FRAMES = 12            # dead blocks before the mic search is re-run

SCALE_HZ = (100, 500, 1000, 2000, 4000)

BG = 0x0A1018
HDR_BG = 0x14304A
SEP = 0x24485F
GRID = 0x1E3A4C
AXIS_FG = 0x4E7C93
TXT = 0xE8F4FF
DIM = 0x7F9DB0
BAR_LO = 0x18C26A
BAR_MID = 0xD9C22A
BAR_HI = 0xE0503A
LINE = 0x9BE8FF
PEAK_FG = 0xF2F7FF
OK_FG = 0x3ADC8C
WARN_FG = 0xE0A03A
ERR_FG = 0xE0503A
METER_BG = 0x16202C

LCD = None
F_SM = None
F_MD = None
LOCKED = -1
VARIANT_TXT = ""
HDR_KEY = None

BAR_NORM = [0.0] * BARS
BAR_PK = [0.0] * BARS
BAND = []
RAW = [0] * CAP_N
RE = [0.0] * FFT_N
IM = [0.0] * FFT_N
WIN = [0.0] * FFT_N
SEARCH_ROWS = []
SENTINEL = bytes([0x55]) * (CAP_N * 2)
BUF = bytearray(CAP_N * 2)
DEAD = [0]
LAST = [0]
FPS = [0]
FPS_N = [0]
FPS_T0 = [0]


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

def est_w(s, px):
    w = 0.0
    for ch in s:
        if ch == " ":
            w += 0.32
        elif "0" <= ch <= "9":
            w += 0.62
        elif "A" <= ch <= "Z":
            w += 0.80
        elif "a" <= ch <= "z":
            w += 0.58
        else:
            w += 0.40
    return int(w * px)


def tw(s, px):
    f = getattr(LCD, "textWidth", None)
    if f is not None:
        try:
            return int(f(s))
        except Exception:
            pass
    return est_w(s, px)


def font_of(*names):
    fonts = getattr(LCD, "FONTS", None)
    if fonts is None:
        return None
    for name in names:
        handle = getattr(fonts, name, None)
        if handle is not None:
            return handle
    return None


def set_font(px):
    f = F_SM if px <= 12 else F_MD
    if f is not None:
        LCD.setFont(f)


def put(s, x, y, fg, px):
    set_font(px)
    LCD.setTextColor(fg, BG)
    LCD.drawString(s, int(x), int(y))


def put_right(s, xr, y, fg, px):
    put(s, xr - tw(s, px), y, fg, px)


def put_center(s, xc, y, fg, px):
    w = tw(s, px)
    x = xc - w // 2
    if x + w > 239:                     # clamp the right edge first
        x = 239 - w
    if x < 0:
        x = 0
    put(s, x, y, fg, px)


def hz_label(hz):
    if hz < 1000:
        return "%d" % hz
    return "%dk" % (hz // 1000)


def fx_of(hz):
    if hz <= F_LO:
        return 0
    x = int(240.0 * math.log(hz / F_LO) / LOG_SPAN)
    if x > 239:
        x = 239
    return x


# ---------------------------------------------------------------------------
# FFT: iterative radix-2, complex, in place
# ---------------------------------------------------------------------------

def fft():
    n = FFT_N
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            RE[i], RE[j] = RE[j], RE[i]
            IM[i], IM[j] = IM[j], IM[i]
    ln = 2
    while ln <= n:
        ang = -6.283185307179586 / ln
        wr = math.cos(ang)
        wi = math.sin(ang)
        h = ln >> 1
        for i in range(0, n, ln):
            cr = 1.0
            ci = 0.0
            for k in range(i, i + h):
                k2 = k + h
                ar = RE[k]
                ai = IM[k]
                br = RE[k2]
                bi = IM[k2]
                tr = br * cr - bi * ci
                ti = br * ci + bi * cr
                RE[k] = ar + tr
                IM[k] = ai + ti
                RE[k2] = ar - tr
                IM[k2] = ai - ti
                ncr = cr * wr - ci * wi
                ci = cr * wi + ci * wr
                cr = ncr
        ln <<= 1


def build_tables():
    global BAND
    for i in range(FFT_N):
        WIN[i] = 0.5 - 0.5 * math.cos(6.283185307179586 * i / (FFT_N - 1))
    band = []
    ratio = math.pow(F_HI / F_LO, 1.0 / BARS)
    lo = F_LO
    for b in range(BARS):
        hi = lo * ratio
        k0 = int(lo / DF + 0.5)
        k1 = int(hi / DF + 0.5) - 1
        if k0 < BIN_FIRST:
            k0 = BIN_FIRST
        if k1 < k0:
            k1 = k0
        if k1 > BIN_LAST:
            k1 = BIN_LAST
        band.append((k0, k1))
        lo = hi
    BAND = band


# ---------------------------------------------------------------------------
# microphone handling
# ---------------------------------------------------------------------------

def speaker_release():
    try:
        Speaker.stop()
    except Exception:
        pass
    try:
        Speaker.end()
    except Exception:
        pass
    try:
        Speaker.begin()
        Speaker.setVolumePercentage(1)
        Speaker.end()
    except Exception:
        pass


def park_mic_pin():
    """Reset the pad state left behind by the I2S driver / amp on G46."""
    try:
        p = machine.Pin(MIC_DAT, machine.Pin.OUT)
        p.value(0)
        time.sleep_ms(20)
        p.value(1)
        time.sleep_ms(20)
        p.init(machine.Pin.IN)
        time.sleep_ms(20)
        return
    except Exception:
        pass
    try:
        p = machine.Pin(MIC_DAT, machine.Pin.IN, machine.Pin.PULL_DOWN)
        time.sleep_ms(20)
        p.init(machine.Pin.IN)
        time.sleep_ms(20)
    except Exception:
        pass


def mic_start(park, over_sampling):
    try:
        Mic.end()
    except Exception:
        pass
    speaker_release()
    if park:
        park_mic_pin()
    try:
        Mic.config(pin_data_in=MIC_DAT, sample_rate=RATE, stereo=False,
                   over_sampling=over_sampling, magnification=1,
                   noise_filter_level=0, use_adc=False)
    except Exception:
        try:
            Mic.config(sample_rate=RATE, over_sampling=over_sampling)
        except Exception:
            pass
    try:
        return Mic.begin()
    except Exception:
        return False


def capture():
    """Blocking capture into BUF. Returns elapsed ms, or -1 on failure."""
    BUF[:] = SENTINEL
    t0 = time.ticks_ms()
    try:
        Mic.record(BUF, RATE, False)
    except Exception:
        return -1
    nominal = CAP_N * 1000 // RATE
    limit = nominal + 2000
    while True:
        el = time.ticks_diff(time.ticks_ms(), t0)
        if Mic.isRecording() == 0 and el >= nominal:
            return el
        if el > limit:
            return el
        time.sleep_ms(2)


def decode():
    """Decode BUF into RAW. Returns (min, max, distinct, debris)."""
    mn = 32767
    mx = -32768
    for i in range(CAP_N):
        o = i * 2
        v = BUF[o] | (BUF[o + 1] << 8)
        if v >= 32768:
            v -= 65536
        RAW[i] = v
        if v < mn:
            mn = v
        if v > mx:
            mx = v
    seen = set()
    for i in range(FFT_N):
        seen.add(RAW[i])
    distinct = len(seen)
    f0 = RAW[0]
    if -30000 < f0 < 30000:
        return mn, mx, distinct, False
    for i in range(1, 8):
        if RAW[i] != f0:
            return mn, mx, distinct, False
    return mn, mx, distinct, True          # identical full-scale prefix


def is_live():
    mn, mx, distinct, debris = decode()
    if debris:
        return False, "DEBRIS"
    if distinct < LIVE_DISTINCT:
        return False, "CONST"
    return True, "LIVE"


def search_mic():
    """Try each mic configuration and confirm it with two consecutive live
    blocks. Returns the locked variant index, or -1."""
    global SEARCH_ROWS, VARIANT_TXT, HDR_KEY
    variants = ((1, False), (4, False), (1, True), (4, True))
    SEARCH_ROWS = []
    HDR_KEY = None
    draw_search("SEARCHING MIC")
    for idx in range(len(variants)):
        os_, park = variants[idx]
        name = "os%d%s" % (os_, "+park" if park else "")
        ok = mic_start(park, os_) is True
        note = "NO DATA"
        if ok:
            capture()                       # discard: DMA ring leftovers
            good = 0
            for _ in range(2):
                if capture() < 0:
                    note = "REC FAIL"
                    good = 0
                    break
                live, why = is_live()
                if live:
                    good += 1
                    note = "OK"
                else:
                    note = why
                    good = 0
                    break
            ok = good >= 2
        else:
            note = "BEGIN FAIL"
        if not ok:
            try:
                Mic.end()
            except Exception:
                pass
        SEARCH_ROWS.append((name, ok, note))
        draw_search("SEARCHING MIC")
        if ok:
            VARIANT_TXT = name
            return idx
    VARIANT_TXT = ""
    return -1


# ---------------------------------------------------------------------------
# spectrum maths
# ---------------------------------------------------------------------------

def analyse():
    """Fill BAR_NORM from one block. Returns (peak_hz, peak_db, rms_db)."""
    for i in range(FFT_N):
        RE[i] = RAW[CAP_OFF + i] * WIN[i]
        IM[i] = 0.0
    fft()

    peak_bin = BIN_FIRST
    peak_db = GATE_DB
    for k in range(BIN_FIRST, BIN_LAST + 1):
        r = RE[k]
        i2 = IM[k]
        mag = math.sqrt(r * r + i2 * i2)
        amp = mag * 4.0 / FFT_N
        if amp < 1.0:
            db = GATE_DB
        else:
            db = 20.0 * math.log(amp / 32768.0) / 2.302585092994046
        if db > peak_db:
            peak_db = db
            peak_bin = k
        RE[k] = db                      # buffer now holds dB, not reals

    top = peak_db
    if top < DB_TOP_MIN:
        top = DB_TOP_MIN
    bot = top - DB_RANGE

    for b in range(BARS):
        k0, k1 = BAND[b]
        best = GATE_DB
        for k in range(k0, k1 + 1):
            if RE[k] > best:
                best = RE[k]
        if best < bot:
            best = bot
        n = (best - bot) / (top - bot)
        if n < 0.0:
            n = 0.0
        elif n > 1.0:
            n = 1.0
        BAR_NORM[b] = n

    hz = peak_bin * DF
    if BIN_FIRST < peak_bin < BIN_LAST:
        a = RE[peak_bin - 1]
        c = RE[peak_bin + 1]
        den = a - 2.0 * RE[peak_bin] + c
        if den < -0.0001:
            off = 0.5 * (a - c) / den
            if off > 0.5:
                off = 0.5
            elif off < -0.5:
                off = -0.5
            hz = (peak_bin + off) * DF

    rms = 0.0
    for i in range(FFT_N):
        v = RAW[CAP_OFF + i]
        rms += v * v
    rms = math.sqrt(rms / FFT_N)
    if rms < 1.0:
        rms_db = GATE_DB
    else:
        rms_db = 20.0 * math.log(rms / 32768.0) / 2.302585092994046
    return hz, peak_db, rms_db


def bar_color(n):
    if n > 0.85:
        return BAR_HI
    if n > 0.6:
        return BAR_MID
    return BAR_LO


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------

def draw_static():
    LCD.fillScreen(BG)
    LCD.fillRect(0, 0, 240, SPECT_TOP, HDR_BG)
    put("MIC FFT", 4, 1, TXT, 12)
    put("%dk/%d" % (RATE // 1000, FFT_N), 70, 1, AXIS_FG, 12)
    LCD.drawLine(0, SEP_Y, 239, SEP_Y, SEP)
    for hz in SCALE_HZ:
        put_center(hz_label(hz), fx_of(hz), AXIS_Y, AXIS_FG, 12)


def draw_header(txt, fg):
    global HDR_KEY
    key = (txt, fg)
    if key == HDR_KEY:
        return
    HDR_KEY = key
    LCD.fillRect(HDR_X0, 0, 240 - HDR_X0, SPECT_TOP, HDR_BG)
    set_font(12)
    LCD.setTextColor(fg, HDR_BG)
    LCD.drawString(txt, int(236 - tw(txt, 12)), 1)


def draw_spectrum():
    LCD.fillRect(0, SPECT_TOP, 240, SPECT_H, BG)
    for hz in SCALE_HZ:
        x = fx_of(hz)
        LCD.drawLine(x, SPECT_TOP, x, BASE_Y - 1, GRID)
    LCD.drawLine(0, BASE_Y, 239, BASE_Y, SEP)

    prev_x = -1
    prev_y = -1
    for b in range(BARS):
        x = b * BAR_W
        n = BAR_NORM[b]
        h = int(n * SPECT_H)
        ytop = BASE_Y - h
        if h > 0:
            LCD.fillRect(x, ytop, BAR_W, h, bar_color(n))
        cx = x + BAR_W // 2
        if prev_x >= 0:
            LCD.drawLine(prev_x, prev_y, cx, ytop, LINE)
        prev_x = cx
        prev_y = ytop

    for b in range(BARS):
        n = BAR_PK[b]
        if n > 0.001:
            y = BASE_Y - int(n * SPECT_H)
            if y < SPECT_TOP:
                y = SPECT_TOP
            LCD.fillRect(b * BAR_W, y, BAR_W, 2, PEAK_FG)


def draw_info(hz, rms_db):
    LCD.fillRect(0, SEP_Y + 1, 240, 135 - SEP_Y - 1, BG)
    put("PK %4dHz" % int(hz), 4, INFO_Y, TXT, 14)

    LCD.fillRect(METER_X0, METER_Y, METER_X1 - METER_X0, 9, METER_BG)
    n = (rms_db - GATE_DB) / (0.0 - GATE_DB)
    if n < 0.0:
        n = 0.0
    elif n > 1.0:
        n = 1.0
    w = int(n * (METER_X1 - METER_X0))
    if w > 0:
        LCD.fillRect(METER_X0, METER_Y, w, 9, bar_color(n))
    LCD.drawRect(METER_X0 - 1, METER_Y - 1, METER_X1 - METER_X0 + 2, 11, SEP)
    put_right("%2df/s" % FPS[0], 236, METER_Y, DIM, 12)


def draw_search(note):
    LCD.fillScreen(BG)
    LCD.fillRect(0, 0, 240, SPECT_TOP, HDR_BG)
    put("MIC SELF CHECK", 4, 1, TXT, 12)
    put(note, 4, 26, DIM, 12)
    y = 46
    for name, ok, why in SEARCH_ROWS:
        put(name, 10, y, TXT, 12)
        put(why, 110, y, OK_FG if ok else ERR_FG, 12)
        y += 16
    put("SPM1423  DAT G46  CLK G43", 10, 116, AXIS_FG, 12)


def draw_no_mic():
    LCD.fillScreen(BG)
    LCD.fillRect(0, 0, 240, SPECT_TOP, HDR_BG)
    put("MIC NOT AVAILABLE", 4, 1, ERR_FG, 12)
    put("4 mic configurations tried,", 8, 34, TXT, 12)
    put("no live audio on G46.", 8, 52, TXT, 12)
    put("Nothing must own G43 (the", 8, 82, DIM, 12)
    put("amp clock). Searching again.", 8, 98, DIM, 12)
    put("retry...", 8, 118, WARN_FG, 12)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def setup():
    global LCD, F_SM, F_MD, LOCKED, VARIANT_TXT, HDR_KEY
    M5.begin()
    try:
        Widgets.setRotation(1)
    except Exception:
        try:
            M5.Lcd.setRotation(1)
        except Exception:
            pass
    LCD = M5.Lcd
    F_SM = font_of("Montserrat12", "Montserrat14", "DejaVu12")
    F_MD = font_of("Montserrat14", "Montserrat16", "DejaVu18", "Montserrat12")
    if F_MD is None:
        F_MD = F_SM

    build_tables()

    LOCKED = search_mic()
    HDR_KEY = None
    if LOCKED < 0:
        draw_no_mic()
    else:
        draw_static()
        draw_header("OK " + VARIANT_TXT, OK_FG)
        LAST[0] = time.ticks_ms()
        FPS_T0[0] = LAST[0]


def loop():
    global LOCKED, HDR_KEY
    M5.update()

    if LOCKED < 0:
        time.sleep_ms(900)
        LOCKED = search_mic()
        HDR_KEY = None
        if LOCKED < 0:
            draw_no_mic()
        else:
            draw_static()
            draw_header("OK " + VARIANT_TXT, OK_FG)
            LAST[0] = time.ticks_ms()
            FPS_T0[0] = LAST[0]
        return

    if capture() < 0:
        return
    live, why = is_live()
    if not live:
        DEAD[0] += 1
        if DEAD[0] >= DEAD_FRAMES:
            DEAD[0] = 0
            draw_search("MIC LOST - RESCAN")
            LOCKED = search_mic()
            HDR_KEY = None
            if LOCKED < 0:
                draw_no_mic()
            else:
                draw_static()
                draw_header("OK " + VARIANT_TXT, OK_FG)
        else:
            draw_header("NO DATA", WARN_FG)
        return
    DEAD[0] = 0

    hz, peak_db, rms_db = analyse()

    now = time.ticks_ms()
    dt = time.ticks_diff(now, LAST[0])
    if dt < 0:
        dt = 0
    LAST[0] = now
    drop = PEAK_DECAY * dt / 1000.0
    for b in range(BARS):
        n = BAR_NORM[b]
        if n >= BAR_PK[b]:
            BAR_PK[b] = n
        else:
            v = BAR_PK[b] - drop
            BAR_PK[b] = v if v > 0.001 else 0.0

    draw_spectrum()
    draw_info(hz, rms_db)
    if rms_db < -62.0:
        draw_header("QUIET " + VARIANT_TXT, WARN_FG)
    else:
        draw_header("OK " + VARIANT_TXT, OK_FG)

    FPS_N[0] += 1
    el = time.ticks_diff(now, FPS_T0[0])
    if el >= 1000:
        FPS[0] = FPS_N[0] * 1000 // el
        FPS_N[0] = 0
        FPS_T0[0] = now


if __name__ == "__main__":
    try:
        setup()
        while True:
            loop()
    except (Exception, KeyboardInterrupt) as e:
        try:
            from utility import print_error_msg

            print_error_msg(e)
        except ImportError:
            print("please update to latest firmware")
