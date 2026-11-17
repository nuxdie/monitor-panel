"""Ambient monitor policy and read-only light sources (stdlib only)."""
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

CONFIG = Path.home() / '.config/monitor-panel/config.json'
STATE_DIR = Path.home() / '.local/state/monitor-panel'
STATUS = STATE_DIR / 'status.json'
OVERRIDES = STATE_DIR / 'overrides.json'
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
SOCKET = RUNTIME / 'monitor-ambient.sock'
# The owner's four anchor levels, darkest to brightest. They are anchors of one continuous
# curve, not steps: brightness glides between them as the light changes.
PHASES = ['Night', 'Evening', 'Dusk', 'Day']
LEVELS = {'Night': 5, 'Evening': 10, 'Dusk': 15, 'Day': 20}
# Used until config.json gives ambient.latitude / ambient.longitude (city-level is plenty).
DEFAULT_LAT, DEFAULT_LON = 52.37, 4.90


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def request(payload, timeout=2):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(str(SOCKET))
        s.sendall(json.dumps(payload).encode() + b'\n')
        data = b''
        while b'\n' not in data:
            block = s.recv(65536)
            if not block:
                raise RuntimeError('Automation connection closed')
            data += block
        result = json.loads(data.split(b'\n')[0])
        if result.get('error'):
            raise RuntimeError(result['error'])
        return result


def command(argv, timeout=12):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'command failed').strip()[:400])
    return result.stdout


def ddc(bus, *args):
    # The panel and service use the same lock, including reads.
    return command(['flock', '--timeout', '10', str(RUNTIME / f'monitor-ddc-{bus}.lock'),
                    'ddcutil', '--bus', str(bus), *map(str, args)], timeout=25)


def vcp_values(text):
    return {m[0].upper(): (int(m[1]), int(m[2])) for m in
            re.findall(r'^VCP\s+([0-9a-fA-F]{2})\s+C\s+(\d+)\s+(\d+)', text, re.M)}


def solar_elevation(timestamp=None, lat=DEFAULT_LAT, lon=DEFAULT_LON):
    """Low-precision solar position; no network or civil-time assumptions."""
    days = (timestamp if timestamp is not None else time.time()) / 86400 - 10957.5
    anomaly = math.radians((357.529 + .98560028 * days) % 360)
    longitude = math.radians((280.459 + .98564736 * days +
                             1.915 * math.sin(anomaly) + .020 * math.sin(2 * anomaly)) % 360)
    obliquity = math.radians(23.439 - .00000036 * days)
    ra = math.atan2(math.cos(obliquity) * math.sin(longitude), math.cos(longitude))
    dec = math.asin(math.sin(obliquity) * math.sin(longitude))
    hour = math.radians((280.1600 + 360.9856235 * days + lon) % 360) - ra
    phi = math.radians(lat)
    return math.degrees(math.asin(math.sin(phi) * math.sin(dec) +
                                  math.cos(phi) * math.cos(dec) * math.cos(hour)))


def interp(x, points):
    """Straight lines through (x, y) points, flat beyond the ends."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def clear_day_level(elevation, levels=LEVELS):
    """Brightness from the sun's height alone: night below −12°, evening at −6° (civil
    twilight), dusk at the horizon, day from 6° up, gliding in between."""
    n, e, d, day = (levels[p] for p in PHASES)
    return interp(elevation, [(-12, n), (-6, e), (-.833, d), (6, day)])


def light_level(sample, levels=LEVELS):
    """The brightness a light reading asks for, as a continuous number.
    Indoor lux: 2.5 lx night, 12 evening, 55 dusk, 200 day, interpolated on a log scale
    (the eye's own scale). Outdoor solar radiation (W/m², not room lux): 0 evening,
    30 dusk, 120 day, and below the horizon the sun's depth takes over down to night."""
    n, e, d, day = (levels[p] for p in PHASES)
    if 'lux' in sample:
        return interp(math.log10(max(sample['lux'], .1)),
                      [(math.log10(2.5), n), (math.log10(12), e), (math.log10(55), d), (math.log10(200), day)])
    if 'radiation' in sample:
        dark = interp(sample['elevation'], [(-12, n), (-.833, e)])
        return max(dark, interp(sample['radiation'], [(0, e), (30, d), (120, day)])) \
            if sample['elevation'] > -.833 else dark
    return clear_day_level(sample['elevation'], levels)


class LightSources:
    def __init__(self):
        self.sensor = None
        self.weather = None
        self.last_sensor = self.last_weather = -float('inf')
        self.sensor_error = self.weather_error = None

    def poll(self, config, now=None):
        now = time.time() if now is None else now
        # Optional lux sensor on another machine: ambient.lux_ssh = "user@host" and
        # ambient.lux_path = file holding the latest lux reading (default /dev/shm/lux).
        # An unreachable sensor costs a multi-second timeout; do not pay it every round.
        host = config.get('lux_ssh')
        if host and now - self.last_sensor >= (15 if self.sensor_error is None else 120):
            self.last_sensor = now
            # Fixed read-only command; remote age avoids clock skew between hosts.
            script = ('import json,time,os,sys; p=sys.argv[1]; '
                      'print(json.dumps({"lux":float(open(p).read()),'
                      '"age":time.time()-os.stat(p).st_mtime}))')
            import shlex
            try:
                value = json.loads(command(['ssh', '-oBatchMode=yes', '-oConnectTimeout=3',
                                            '-oStrictHostKeyChecking=yes', host,
                                            'python3 -c ' + shlex.quote(script) + ' ' +
                                            shlex.quote(config.get('lux_path', '/dev/shm/lux'))], timeout=5))
                lux, age = float(value['lux']), float(value['age'])
                if not math.isfinite(lux) or not 0 <= lux <= 200000 or not -5 <= age <= 90:
                    raise ValueError('Invalid or stale lux reading')
                self.sensor = {'lux': lux, 'at': now, 'age': max(0, age)}
                self.sensor_error = None
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as e:
                self.sensor_error = str(e)
        if now - self.last_weather >= (900 if self.weather_error is None else 120):
            self.last_weather = now
            try:
                params = urllib.parse.urlencode({
                    'latitude': config.get('latitude', DEFAULT_LAT),
                    'longitude': config.get('longitude', DEFAULT_LON),
                    'minutely_15': 'shortwave_radiation_instant', 'timeformat': 'unixtime',
                    'forecast_minutely_15': 2, 'past_minutely_15': 1})
                req = urllib.request.Request('https://api.open-meteo.com/v1/forecast?' + params,
                                             headers={'User-Agent': 'monitor-panel/1.0 personal-use'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.load(response)['minutely_15']
                samples = sorted((stamp, value) for stamp, value in
                                 zip(data['time'], data['shortwave_radiation_instant'])
                                 if value is not None)
                if not samples or samples[0][0] > now or any(
                        not math.isfinite(v) or not 0 <= v <= 1600 for _, v in samples):
                    raise ValueError('Invalid solar radiation')
                # Quarter-hour samples around now; choose() glides between them.
                self.weather = {'samples': samples, 'at': now}
                self.weather_error = None
            except (OSError, ValueError, KeyError, RuntimeError) as e:
                self.weather_error = str(e)

    def choose(self, config, previous=None, now=None):
        now = time.time() if now is None else now
        elevation = solar_elevation(now, config.get('latitude', DEFAULT_LAT),
                                    config.get('longitude', DEFAULT_LON))
        if self.sensor and now - self.sensor['at'] + self.sensor['age'] <= 90:
            return {'source': 'Light sensor', 'lux': self.sensor['lux'], 'elevation': elevation}
        samples = self.weather['samples'] if self.weather else []
        if samples and samples[0][0] <= now <= samples[-1][0] + 2700:
            return {'source': 'Daylight estimate', 'radiation': interp(now, samples),
                    'at': self.weather['at'], 'elevation': elevation}
        return {'source': 'Offline sun position', 'elevation': elevation}


def stepped(current, target, step=2):
    return min(target, current + step) if target > current else max(target, current - step)


def matched(level, points):
    """A brightness level on the main monitor's scale → the value that looks the same on
    another monitor, from pairs [level on main, value here] the owner judged equal by eye.
    One pair shifts the scale; several are joined by straight lines, and beyond the first
    and last pair the nearest line carries on (kept between a quarter and four times the
    main monitor's step so two close pairs cannot run away)."""
    if not points:
        return level
    pts = sorted(points)
    if len(pts) == 1:
        value = level + pts[0][1] - pts[0][0]
    else:
        i = max(1, min(len(pts) - 1, sum(level > l for l, _ in pts)))
        (l0, v0), (l1, v1) = pts[i - 1], pts[i]
        slope = (v1 - v0) / (l1 - l0)
        if not l0 <= level <= l1:
            slope = max(.25, min(4, slope))
        value = (v0 if level <= l0 else v1) + slope * (level - (l0 if level <= l0 else l1)) \
            if not l0 <= level <= l1 else v0 + slope * (level - l0)
    return max(0, min(100, round(value)))


def add_match(points, level, value):
    """points plus a new judged-equal pair. The new pair wins: older pairs at about the same
    level, or that would make brighter-on-main mean darker-here, are dropped."""
    kept = [[l, v] for l, v in points or []
            if abs(l - level) >= max(4, .35 * level) and (l - level) * (v - value) >= 0]
    return sorted(kept + [[level, value]])[-4:]


def targets(keys, main, level, overrides, match=None):
    match = match or {}
    result = {key: {'10': level if key == main else matched(level, match.get(key)),
                    '12': 20, 'mode': 'auto'} for key in keys}
    for key, entry in overrides.items():
        if key not in result:
            continue
        mode = entry['mode']
        if mode == 'manual':
            result[key].update(entry.get('values', {}))
            result[key]['mode'] = mode
        elif key == main and mode == 'movie':
            result[key].update({'10': 50, '12': 50, 'mode': mode})
        elif key == main and mode == 'game':
            result[key]['mode'] = mode
    return result
