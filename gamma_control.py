"""Reversible shadow lift on one XRandR output, preserving the existing colour ramp.

Redshift also owns gamma ramps and rewrites them every few seconds, so its adjustment
process is suspended during Game and resumed on exit. While it is suspended,
follow_redshift() keeps the evening warmth moving on every screen by applying the
temperature Redshift itself reports. Recovery is persisted before any signal/write and
also runs from systemd ExecStopPost after a crash.
"""
import ctypes as C
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from ambient import STATE_DIR, load, save

RECOVERY = STATE_DIR / 'gamma-recovery.json'


class Ramp(C.Structure):
    _fields_ = [('size', C.c_int), ('red', C.POINTER(C.c_ushort)),
                ('green', C.POINTER(C.c_ushort)), ('blue', C.POINTER(C.c_ushort))]


class OutputInfo(C.Structure):
    _fields_ = [('timestamp', C.c_ulong), ('crtc', C.c_ulong)]


class Resources(C.Structure):
    _fields_ = [('timestamp', C.c_ulong), ('config_timestamp', C.c_ulong),
                ('ncrtc', C.c_int), ('crtcs', C.POINTER(C.c_ulong))]


class RandR:
    def __init__(self):
        self.x = C.CDLL('libX11.so.6')
        self.r = C.CDLL('libXrandr.so.2')
        for lib, name, args, result in [
            (self.x, 'XOpenDisplay', [C.c_char_p], C.c_void_p),
            (self.x, 'XDefaultRootWindow', [C.c_void_p], C.c_ulong),
            (self.x, 'XCloseDisplay', [C.c_void_p], C.c_int),
            (self.x, 'XSync', [C.c_void_p, C.c_int], C.c_int),
            (self.r, 'XRRGetScreenResourcesCurrent', [C.c_void_p, C.c_ulong], C.c_void_p),
            (self.r, 'XRRFreeScreenResources', [C.c_void_p], None),
            (self.r, 'XRRGetOutputInfo', [C.c_void_p, C.c_void_p, C.c_ulong], C.POINTER(OutputInfo)),
            (self.r, 'XRRFreeOutputInfo', [C.POINTER(OutputInfo)], None),
            (self.r, 'XRRGetCrtcGammaSize', [C.c_void_p, C.c_ulong], C.c_int),
            (self.r, 'XRRGetCrtcGamma', [C.c_void_p, C.c_ulong], C.POINTER(Ramp)),
            (self.r, 'XRRAllocGamma', [C.c_int], C.POINTER(Ramp)),
            (self.r, 'XRRFreeGamma', [C.POINTER(Ramp)], None),
            (self.r, 'XRRSetCrtcGamma', [C.c_void_p, C.c_ulong, C.POINTER(Ramp)], None),
        ]:
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, result
        self.display = self.x.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError('Cannot open X display for shadow lift')

    def crtc(self, output):
        resources = self.r.XRRGetScreenResourcesCurrent(
            self.display, self.x.XDefaultRootWindow(self.display))
        if not resources:
            raise RuntimeError('Cannot read display resources')
        try:
            info = self.r.XRRGetOutputInfo(self.display, resources, output)
            if not info:
                raise RuntimeError('Main output unavailable')
            try:
                return info.contents.crtc
            finally:
                self.r.XRRFreeOutputInfo(info)
        finally:
            self.r.XRRFreeScreenResources(resources)

    def crtcs(self):
        """Every CRTC id, in the order Redshift numbers them (randr:crtc=N)."""
        resources = self.r.XRRGetScreenResourcesCurrent(
            self.display, self.x.XDefaultRootWindow(self.display))
        if not resources:
            raise RuntimeError('Cannot read display resources')
        try:
            r = C.cast(resources, C.POINTER(Resources)).contents
            return [r.crtcs[i] for i in range(r.ncrtc)]
        finally:
            self.r.XRRFreeScreenResources(resources)

    def read(self, crtc):
        p = self.r.XRRGetCrtcGamma(self.display, crtc)
        if not p or p.contents.size < 2:
            raise RuntimeError('No gamma ramp available')
        try:
            return [[getattr(p.contents, channel)[i] for i in range(p.contents.size)]
                    for channel in ('red', 'green', 'blue')]
        finally:
            self.r.XRRFreeGamma(p)

    def write(self, crtc, values):
        size = len(values[0])
        if self.r.XRRGetCrtcGammaSize(self.display, crtc) != size:
            raise RuntimeError('Gamma ramp size changed; select Auto and retry')
        p = self.r.XRRAllocGamma(size)
        if not p:
            raise RuntimeError('Cannot allocate gamma ramp')
        try:
            for channel, data in zip(('red', 'green', 'blue'), values):
                for i, value in enumerate(data):
                    getattr(p.contents, channel)[i] = value
            self.r.XRRSetCrtcGamma(self.display, crtc, p)
            self.x.XSync(self.display, 0)
        finally:
            self.r.XRRFreeGamma(p)

    def close(self):
        if self.display:
            self.x.XCloseDisplay(self.display)
            self.display = None


MAX_SHADOWS = 100


def curve(x, shadows):
    """Shadow lift for a 0..1 signal. shadows is 0..100: 0 leaves the picture alone, 100
    is the strongest lift. Only the dark end moves; from the midtones up the picture is
    practically untouched, black stays black and white stays white. The slope at black
    is finite (1 + 4·shadows/100), so noise in near-black is not blown up into banding
    the way a plain gamma curve does, and the curve stays strictly increasing."""
    return x + 4 * shadows / MAX_SHADOWS * x * (1 - x) ** 5


def lifted(base, shadows):
    """Resample the original ramp through curve(): endpoints and colour tint stay intact."""
    result = []
    for channel in base:
        n = len(channel)
        values = []
        for i in range(n):
            position = min(curve(i / (n - 1), shadows), 1) * (n - 1)
            lo = int(position)
            hi = min(lo + 1, n - 1)
            values.append(round(channel[lo] + (channel[hi] - channel[lo]) * (position - lo)))
        result.append(values)
    return result


def redshift_temperature():
    """The colour temperature Redshift wants right now (K), or None."""
    try:
        out = subprocess.run(['redshift', '-p'], capture_output=True, text=True, timeout=5,
                             env={**os.environ, 'LC_ALL': 'C'}).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'Color temperature:\s*(\d+)K', out)
    return int(m.group(1)) if m else None


def process_info(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()
        return {'pid': int(pid), 'start': fields[19], 'state': fields[0]}
    except (OSError, ValueError, IndexError):
        return None


def redshift_processes():
    result = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid == os.getuid() and (p / 'exe').resolve() == Path('/usr/bin/redshift'):
                info = process_info(p.name)
                if info and info['state'] not in ('T', 't', 'Z'):
                    result.append(info)
        except OSError:
            pass
    return result


def signal_owned(info, sig):
    current = process_info(info['pid'])
    if current and current['start'] == info['start']:
        try:
            os.kill(info['pid'], sig)
        except ProcessLookupError:
            pass


def recover():
    record = load(RECOVERY)
    if not record:
        return
    try:
        if record.get('base'):
            rr = RandR()
            try:
                if rr.crtc(record['output']) == record['crtc']:
                    rr.write(record['crtc'], record['base'])
            finally:
                rr.close()
    finally:
        for info in record.get('processes', []):
            signal_owned(info, signal.SIGCONT)
    RECOVERY.unlink(missing_ok=True)


class ShadowLift:
    def __init__(self):
        self.rr = None
        self.record = None

    def follow_redshift(self, other_crtcs):
        """Redshift is suspended during Game, which would freeze the warmth for a whole
        evening. Ask Redshift what temperature it wants now, apply that to the other
        screens with its own one-shot mode, and rebuild the main screen's base ramp from
        one of them. The main screen never drops its lift, so nothing flickers."""
        if not self.record or not self.record['processes'] or not other_crtcs:
            return None
        wanted = redshift_temperature()
        if wanted is None:
            return None
        if self.record.get('temperature') is None:
            # First look after entering Game: the ramps already show this temperature.
            # An untinted ramp while Redshift wants warmth means it is switched off in its
            # tray icon; then there is nothing to follow.
            peaks = [channel[-1] for channel in self.record['base']]
            self.record['temperature'] = wanted if (wanted >= 6400 or min(peaks) < max(peaks)) else 0
            return wanted
        if not self.record['temperature']:
            return None
        if wanted == self.record['temperature']:
            return wanted
        order = self.rr.crtcs()
        size = len(self.record['base'][0])
        donor = None
        for crtc in other_crtcs:
            if crtc not in order or self.rr.r.XRRGetCrtcGammaSize(self.rr.display, crtc) != size:
                continue
            # -P starts from a clean ramp instead of stacking on the current one.
            done = subprocess.run(['redshift', '-P', '-O', str(wanted), '-m',
                                   f'randr:crtc={order.index(crtc)}'],
                                  capture_output=True, text=True, timeout=10)
            if done.returncode == 0:
                donor = crtc
        if donor is None:
            return self.record['temperature']
        self.record['base'] = self.rr.read(donor)
        self.record['temperature'] = wanted
        save(RECOVERY, self.record)
        return wanted

    def apply(self, output, shadows):
        if self.record and self.record['output'] != output:
            self.restore()
        if not self.record:
            self.rr = RandR()
            self.record = {'output': output, 'crtc': self.rr.crtc(output),
                           'processes': redshift_processes()}
            if not self.record['crtc']:
                self.rr.close()
                self.rr, self.record = None, None
                raise RuntimeError('Main monitor disconnected')
            save(RECOVERY, self.record)
            for info in self.record['processes']:
                signal_owned(info, signal.SIGSTOP)
            for _ in range(20):
                if all(not process_info(p['pid']) or process_info(p['pid'])['state'] in ('T', 't')
                       for p in self.record['processes']):
                    break
                time.sleep(.01)
            self.record['base'] = self.rr.read(self.record['crtc'])
            save(RECOVERY, self.record)
        if self.rr.crtc(output) != self.record['crtc']:
            self.restore()
            raise RuntimeError('Display layout changed; select Game again')
        wanted = lifted(self.record['base'], shadows)
        if self.rr.read(self.record['crtc']) != wanted:
            self.rr.write(self.record['crtc'], wanted)
        actual = self.rr.read(self.record['crtc'])
        error = max(abs(a - b) for ca, cb in zip(actual, wanted) for a, b in zip(ca, cb))
        if error > 256:
            raise RuntimeError('Display driver did not retain the requested shadow lift')
        return {'shadows': shadows, 'max_readback_error': error,
                'redshift_suspended': bool(self.record['processes']),
                'temperature': self.record.get('temperature')}

    def restore(self):
        try:
            recover()
        finally:
            if self.rr:
                self.rr.close()
            self.rr, self.record = None, None
