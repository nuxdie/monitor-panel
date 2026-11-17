"""Monitor identity and DDC parsing shared by panel and ambient service."""
import re
import subprocess


def hexval(tok):
    return int(tok.lstrip("x"), 16)


def parse_vcp(out):
    """ddcutil getvcp --brief → ({feature: value}, {feature: max}); unsupported features are absent."""
    vals, maxes = {}, {}
    for line in out.splitlines():
        p = line.split()
        if len(p) < 4 or p[0] != "VCP":
            continue
        f, kind = p[1].upper(), p[2]
        if kind == "C" and len(p) >= 5:
            vals[f], maxes[f] = int(p[3]), int(p[4])
        elif kind == "SNC":
            vals[f] = hexval(p[3])
        elif kind == "CNC" and len(p) >= 7:
            maxes[f] = (hexval(p[3]) << 8) | hexval(p[4])
            vals[f] = (hexval(p[5]) << 8) | hexval(p[6])
    return vals, maxes


def parse_caps(text):
    """ddcutil capabilities → {feature: [(code, label), …]} ([] for continuous features)."""
    caps, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"\s+Feature: ([0-9A-Fa-f]{2}) \(", line)
        if m:
            cur = m.group(1).upper()
            caps[cur] = []
            continue
        if cur is None:
            continue
        m = re.match(r"\s+Values: ((?:[0-9a-fA-F]{2}\s+)+)\(interpretation unavailable\)", line)
        if m:
            caps[cur] += [(int(h, 16), f"Mode {h}") for h in m.group(1).split()]
            continue
        m = re.match(r"\s+([0-9a-fA-F]{2}): (.+)", line)
        if m:
            label = m.group(2).strip()
            if label == "Unrecognized value":
                label = f"Mode {m.group(1)}"
            caps[cur].append((int(m.group(1), 16), label))
    return caps


def parse_detect(out):
    """ddcutil detect → {edid key: {"bus", "model", "connector"}}."""
    found = {}
    for block in re.split(r"\n(?=\S)", out):
        if not block.startswith("Display"):
            continue           # "Invalid display" = an output without DDC
        def field(rx):
            m = re.search(rx, block)
            return m.group(1).strip() if m else None
        bus, mfg = field(r"/dev/i2c-(\d+)"), field(r"Mfg id:\s+(\w{3})")
        if not (bus and mfg):
            continue
        product = int(field(r"Product code:\s+(\d+)") or 0)
        serial = int(field(r"Binary serial number:\s+(\d+)") or 0)
        found[edid_key(mfg, product, serial)] = {
            "bus": int(bus), "model": field(r"Model:\s+(.*)") or "",
            "connector": field(r"DRM connector:\s+(\S+)") or ""}
    return found


def edid_key(mfg, product, serial):
    return f"{mfg}-{product:04x}-{serial:08x}"


def decode_edid(hexstr):
    """(key, monitor name) from a raw EDID hex dump, or (None, "")."""
    try:
        e = bytes.fromhex(hexstr[:256])
    except ValueError:
        return None, ""
    if len(e) < 128:
        return None, ""
    v = (e[8] << 8) | e[9]
    mfg = "".join(chr(((v >> s) & 31) + 64) for s in (10, 5, 0))
    product = e[10] | (e[11] << 8)
    serial = e[12] | (e[13] << 8) | (e[14] << 16) | (e[15] << 24)
    name = ""
    for i in (54, 72, 90, 108):
        d = e[i:i + 18]
        if d[:3] == b"\0\0\0" and d[3] == 0xFC:
            name = d[5:18].decode("ascii", "ignore").strip(" \n\0")
    return edid_key(mfg, product, serial), name


def read_outputs():
    """Active xrandr outputs: [{name, w, h, x, y, key, model}] (key None without an EDID)."""
    try:
        out = subprocess.run(["xrandr", "--verbose"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    outputs, cur, in_edid, edid = [], None, False, ""
    for line in out.splitlines() + ["END"]:
        if not line.startswith(("\t", " ")):
            if cur and edid:
                cur["key"], cur["model"] = decode_edid(edid)
            cur, in_edid, edid = None, False, ""
            m = re.match(r"^(\S+) connected(?: primary)? (\d+)x(\d+)\+(\d+)\+(\d+)", line)
            if m:
                cur = {"name": m.group(1), "w": int(m.group(2)), "h": int(m.group(3)),
                       "x": int(m.group(4)), "y": int(m.group(5)), "key": None, "model": "", "primary": " connected primary " in line, "output_id": None}
                outputs.append(cur)
            continue
        if cur is None:
            continue
        ident = re.match(r"^\s+Identifier:\s+(0x[0-9a-fA-F]+)", line)
        if ident:
            cur["output_id"] = int(ident.group(1), 16)
        if re.match(r"^\s+EDID:", line):
            in_edid = True
            continue
        if in_edid:
            m = re.match(r"^\s+([0-9a-fA-F]{32})\s*$", line)
            if m:
                edid += m.group(1)
            else:
                in_edid = False
    return outputs

