"""Measurement analysis and filter fitting for the CamillaDSP calibrator.

Everything here is pure numpy/scipy and works on arrays, so it can be tested
without speakers or a microphone. lib/calibrate drives PipeWire and calls in.

The pipeline:

  excitation()        exponential sine sweeps per channel, with silences
  locate()            find where the excitation starts in the recording
  analyze()           transfer function per sweep, frequency-dependent window,
                      noise floor and SNR from the silence before the sweeps
  combine()           average repeats and positions, normalize the level
  fit_peq()/fit_fir() cut-first correction toward a target, cross-validated
                      against the individual repeats, inside a safety budget
  preset_yaml()       a CamillaDSP preset with one filter step per channel
"""

import math
import re

import numpy as np
from scipy import optimize, signal

FS = 48000
F_LOW = 20.0
F_HIGH = 20000.0
GRID = np.geomspace(F_LOW, F_HIGH, 480)

SWEEP_SECONDS = 3.0
GAP_SECONDS = 1.0
LEAD_SECONDS = 1.0
FADE_SECONDS = 0.05

# Safety budget, in dB.
MAX_CUT_PER_FILTER = -12.0
MAX_BOOST_PER_FILTER = 6.0
MAX_TOTAL_CUT = -15.0
MAX_TOTAL_BOOST = 6.0
PREAMP_MARGIN = 0.5

# How much a remaining dip counts against a remaining peak of the same depth.
# A resonance is audible and worth removing; a dip is often a cancellation at
# the microphone that moves with the listener's head.
DIP_WEIGHT = 0.5


class MeasurementError(Exception):
    """A measurement that must not be used, with a message for the user."""


# ---------- Excitation ----------

def sweep(seconds=SWEEP_SECONDS, f1=F_LOW / 2, f2=F_HIGH * 1.1, fs=FS):
    """Exponential sine sweep with short raised-cosine fades.

    It runs a little past the analysis band on both ends, so the fades fall
    outside the frequencies that are measured.
    """
    t = np.arange(int(seconds * fs)) / fs
    rate = math.log(f2 / f1)
    x = np.sin(2 * np.pi * f1 * seconds / rate * (np.exp(t * rate / seconds) - 1))
    fade = int(FADE_SECONDS * fs)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, fade))
    x[:fade] *= ramp
    x[-fade:] *= ramp[::-1]
    return x


def excitation(channels=2, repeats=3, level_db=-20.0, fs=FS):
    """The whole playback: lead-in silence, then each channel in turn, repeated.

    Returns the stereo signal and a schedule of (channel, repeat, start) with
    start in samples from the beginning of the playback.
    """
    s = sweep(fs=fs) * 10 ** (level_db / 20)
    lead, gap = int(LEAD_SECONDS * fs), int(GAP_SECONDS * fs)
    order = [(ch, r) for r in range(repeats) for ch in range(channels)]
    total = lead + len(order) * (len(s) + gap)
    out = np.zeros((total, channels), dtype=np.float32)
    schedule = []
    pos = lead
    for ch, r in order:
        out[pos:pos + len(s), ch] = s
        schedule.append((ch, r, pos))
        pos += len(s) + gap
    return out, schedule


def probe(level_db=-35.0, fs=FS):
    """A short, quiet mid-band sweep on both channels to set the level."""
    s = sweep(seconds=1.0, f1=200, f2=4000, fs=fs) * 10 ** (level_db / 20)
    lead = int(0.5 * fs)
    out = np.zeros((lead + len(s) + lead, 2), dtype=np.float32)
    out[lead:lead + len(s)] = s[:, None]
    return out, lead


def probe_level(recording, played, lead, fs=FS):
    """How loud the probe arrived, and how loud the room is without it.

    Returns (signal peak dBFS, noise rms dBFS) at the microphone.
    """
    rec = np.asarray(recording, dtype=np.float64)
    if rec.ndim > 1:
        rec = rec.mean(axis=1)
    offset = locate(rec, played.mean(axis=1))
    noise = rec[offset + lead // 5:offset + lead - lead // 5]
    seg = rec[offset + lead:offset + lead + int(1.0 * fs)]
    if len(seg) == 0:
        raise MeasurementError("The microphone recording ended before the probe.")
    peak = 20 * np.log10(np.max(np.abs(seg)) + 1e-12)
    floor = 20 * np.log10(np.sqrt(np.mean(noise ** 2)) + 1e-12) if len(noise) else -120.0
    return peak, floor


def choose_level(probe_db, probe_peak_db, noise_db, goal_peak_db=-12.0, ceiling_db=-6.0):
    """Sweep level that brings the microphone to goal_peak_db, never above ceiling.

    Assumes the chain is linear, which holds well below clipping; the main
    measurement checks for clipping again anyway.
    """
    if probe_peak_db - noise_db < 6:
        raise MeasurementError(
            "The microphone barely heard the test sound. Turn the speaker volume up, "
            "check the right microphone is selected, and that it is not muted.")
    return float(min(ceiling_db, probe_db + goal_peak_db - probe_peak_db))


# ---------- Analysis ----------

def locate(recording, reference):
    """Sample offset of reference inside recording, by cross-correlation."""
    corr = signal.correlate(recording, reference, mode="valid", method="fft")
    if len(corr) == 0:
        raise MeasurementError("The recording is shorter than the test sound.")
    peak = int(np.argmax(np.abs(corr)))
    energy = np.sqrt(np.sum(reference ** 2) * np.sum(recording[peak:peak + len(reference)] ** 2)) + 1e-20
    if abs(corr[peak]) / energy < 0.05:
        raise MeasurementError(
            "The microphone did not pick up the test sweeps. Check the selected "
            "microphone and output, and that neither is muted.")
    return peak


def transfer(segment, excitation_segment, fs=FS):
    """Complex transfer function by regularized spectral division."""
    n = 1 << int(math.ceil(math.log2(len(segment) + len(excitation_segment))))
    x = np.fft.rfft(excitation_segment, n)
    y = np.fft.rfft(segment, n)
    f = np.fft.rfftfreq(n, 1 / fs)
    power = np.abs(x) ** 2
    band = (f >= F_LOW) & (f <= F_HIGH)
    eps = np.where(band, 1e-6, 1.0) * power[band].max()
    return y * np.conj(x) / (power + eps), n


def windowed_response(ir, peak, fs=FS, cycles=15.0, min_gate=0.003, max_gate=0.3, delays=False, delay_cycles=4.0):
    """Magnitude on GRID through a gate of `cycles` periods at each frequency.

    Long gates at low frequencies, short ones at high frequencies: the direct
    sound and the nearest reflections stay in, later room reflections stay out.
    Each gate opens a quarter of its length before the peak (1 to 20 ms), so
    bass that a timing correction moved ahead of the peak is not cut off.
    Returns dB on GRID; with delays, also the group delay in ms relative to
    the peak, power-averaged over a sixth of an octave, through gates of only
    `delay_cycles` periods: the timing of the speaker, not of the room's
    later reflections, which differs from one spot to the next.
    """
    gates = np.geomspace(min_gate, max_gate, 14)
    curves, lags = [], []
    for gate in gates:
        length = int(gate * fs)
        pre = int(min(max(gate / 4, 0.001), 0.02) * fs)
        start = max(0, peak - pre)
        seg = ir[start:peak + length].copy()
        rise = min(pre, peak - start)
        if rise > 0:
            seg[:rise] *= 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, rise))
        fall = max(1, length // 3)
        seg[-fall:] *= 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, fall))
        n = max(1 << int(math.ceil(math.log2(len(seg) * 4))), 1 << 14)
        spec = np.fft.rfft(seg, n)
        freqs = np.fft.rfftfreq(n, 1 / fs)
        curves.append(20 * np.log10(np.interp(GRID, freqs, np.abs(spec)) + 1e-12))
        if delays:
            lags.append(_group_delay(spec * np.exp(2j * np.pi * freqs * (peak - start) / fs), freqs))
    def blend(rows, cycles):
        rows = np.array(rows)
        wanted = np.clip(cycles / GRID, min_gate, max_gate)
        pos = np.interp(np.log(wanted), np.log(gates), np.arange(len(gates)))
        lo = np.floor(pos).astype(int)
        hi = np.minimum(lo + 1, len(gates) - 1)
        frac = pos - lo
        idx = np.arange(len(GRID))
        return rows[lo, idx] * (1 - frac) + rows[hi, idx] * frac

    if delays:
        return blend(curves, cycles), blend(lags, delay_cycles)
    return blend(curves, cycles)


def _group_delay(spec, freqs, fraction=6.0):
    """Group delay in ms on GRID from a complex spectrum, each point the
    power-weighted mean over a `fraction` of an octave, so the steep phase
    jumps at notches, where there is little energy, count for little."""
    phase = np.unwrap(np.angle(spec))
    gd = -np.gradient(phase, 2 * np.pi * freqs) * 1000
    power = np.abs(spec) ** 2
    cw = np.concatenate([[0.0], np.cumsum(power)])
    cg = np.concatenate([[0.0], np.cumsum(power * gd)])
    lo = np.searchsorted(freqs, GRID * 2 ** (-0.5 / fraction))
    hi = np.maximum(np.searchsorted(freqs, GRID * 2 ** (0.5 / fraction)), lo + 1)
    return (cg[hi] - cg[lo]) / np.maximum(cw[hi] - cw[lo], 1e-30)


def smooth(db, fraction=6.0):
    """Fractional-octave smoothing of a dB curve on GRID, averaging power."""
    power = 10 ** (db / 10)
    logf = np.log2(GRID)
    half = 0.5 / fraction
    out = np.empty_like(power)
    for i, lf in enumerate(logf):
        sel = np.abs(logf - lf) <= half
        out[i] = power[sel].mean()
    return 10 * np.log10(out)


def analyze(recording, played, schedule, fs=FS, cycles=15.0, max_gate=0.3):
    """Turn one recording of excitation() into per-sweep responses.

    Returns a dict with, per channel, the list of repeat curves (dB on GRID),
    their group delays (ms on GRID, relative to each arrival), the arrival time of each (seconds, relative to the playback start), the
    noise floor curve, and a list of warnings. Raises MeasurementError when the
    recording cannot be trusted.
    """
    rec = np.asarray(recording, dtype=np.float64)
    if rec.ndim > 1:
        rec = rec.mean(axis=1)
    peak = np.max(np.abs(rec))
    if peak > 0.98:
        raise MeasurementError(
            "The microphone was overloaded. Turn the speaker volume or the microphone "
            "gain down a little and measure again.")

    offset = locate(rec, played.mean(axis=1))
    sweep_len = int(SWEEP_SECONDS * fs)
    tail = int(min(GAP_SECONDS * 0.9, max_gate + 0.1) * fs)
    channels = played.shape[1]
    result = {"channels": [], "warnings": [], "peak_dbfs": float(20 * np.log10(peak + 1e-12))}
    per_channel = [{"repeats": [], "delays": [], "arrivals": []} for _ in range(channels)]

    lead = int(LEAD_SECONDS * fs)
    quiet = rec[offset + int(0.1 * fs):offset + lead - int(0.1 * fs)]

    # Start each segment a little early: the sweep that set the offset may have
    # come from the other, later speaker, and an early arrival must not wrap round.
    margin = int(0.02 * fs)
    for ch, _repeat, start in schedule:
        a = offset + start - margin
        if a < 0:
            raise MeasurementError("The recording started too late; measure again.")
        segment = rec[a:a + sweep_len + tail]
        if len(segment) < sweep_len + tail:
            raise MeasurementError("The recording stopped before the last sweep.")
        exc = played[start:start + sweep_len, ch].astype(np.float64)
        h, n = transfer(segment, exc, fs)
        ir = np.fft.irfft(h, n)[:sweep_len + tail]
        arrival = int(np.argmax(np.abs(ir[:margin + int(0.1 * fs)])))
        db, delay = windowed_response(ir, arrival, fs, cycles, max_gate=max_gate, delays=True)
        per_channel[ch]["repeats"].append(db)
        per_channel[ch]["delays"].append(np.round(delay, 3))
        per_channel[ch]["arrivals"].append((arrival - margin) / fs)
        per_channel[ch].setdefault("exc", exc)

    for ch in range(channels):
        exc = per_channel[ch].pop("exc")
        # The silence before the sweeps, put through the same division, is the
        # noise floor in the same units as the response.
        noise_seg = np.resize(quiet, sweep_len + tail) if len(quiet) else np.zeros(sweep_len + tail)
        h, n = transfer(noise_seg, exc, fs)
        noise_ir = np.fft.irfft(h, n)[:sweep_len + tail]
        noise = windowed_response(noise_ir, int(0.002 * fs), fs, cycles, max_gate=max_gate)
        reps = np.array(per_channel[ch]["repeats"])
        mean = smooth(10 * np.log10(np.mean(10 ** (reps / 10), axis=0)), 6)
        snr = mean - smooth(noise, 3)
        mid = (GRID >= 200) & (GRID <= 5000)
        if np.median(snr[mid]) < 15:
            raise MeasurementError(
                "The room is too loud compared to the sweeps, or the volume is too low. "
                "Make it quiet, turn the volume up a little, and measure again.")
        spread = np.std(np.array([smooth(r, 6) for r in reps]), axis=0) if len(reps) > 1 else np.zeros_like(mean)
        if np.median(spread[mid]) > 2.0:
            result["warnings"].append(
                f"Channel {ch + 1}: the repeats disagree by {np.median(spread[mid]):.1f} dB. "
                "Something moved or made noise during the measurement.")
        result["channels"].append({
            "repeats": [r.tolist() for r in reps],
            "delays": [d.tolist() for d in per_channel[ch]["delays"]],
            "arrivals": per_channel[ch]["arrivals"],
            "noise": noise.tolist(),
            "snr": snr.tolist(),
        })
    return result


# ---------- Microphone calibration ----------

def read_mic_calibration(path):
    """Parse a microphone calibration file.

    Reads miniDSP's UMIK files ("Sens Factor =-1.23dB, SERNO: 7012345" then
    "frequency dB" lines) and the REW-style .txt/.cal/.frd files most
    measurement microphones ship with: comment or header lines, then a
    frequency and a dB column, optionally a phase column, separated by spaces,
    tabs, commas or semicolons. Returns freq, db, sensitivity and serial;
    raises ValueError for anything that is not a calibration curve.
    """
    freqs, dbs = [], []
    sensitivity, serial = None, ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            text = line.strip().strip('"')
            if not text:
                continue
            m = re.search(r"sens(?:itivity)?\s*factor\s*=\s*(-?\d+(?:\.\d+)?)", text, re.I)
            if m:
                sensitivity = float(m.group(1))
            m = re.search(r"SERNO\s*:?\s*([A-Za-z0-9-]+)", text, re.I)
            if m:
                serial = m.group(1)
            parts = [p for p in re.split(r"[\s,;]+", text) if p]
            if len(parts) < 2:
                continue
            try:
                freq, db = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            if freq > 0 and math.isfinite(db):
                freqs.append(freq)
                dbs.append(db)
    if len(freqs) < 10:
        raise ValueError("no calibration curve in the file")
    order = np.argsort(freqs)
    freqs, dbs = np.asarray(freqs)[order], np.asarray(dbs)[order]
    if freqs[0] > 200 or freqs[-1] < 5000:
        raise ValueError("the curve does not cover the audio band")
    if np.max(np.abs(dbs)) > 30:
        raise ValueError("the deviations are implausibly large for a calibration file")
    return {"freq": freqs, "db": dbs, "sensitivity": sensitivity, "serial": serial}


def mic_calibration_curve(cal):
    """The microphone's own deviation in dB on GRID, held flat past the file's ends."""
    return np.interp(np.log(GRID), np.log(cal["freq"]), cal["db"])


def apply_mic_calibration(result, curve):
    """Remove the microphone's response from an analyze() result, in place.

    What the microphone over-reads is subtracted, so the curve describes the
    sound at the microphone's position rather than the microphone. The noise
    floor moves with it, which leaves the SNR unchanged.
    """
    for c in result["channels"]:
        c["repeats"] = [(np.asarray(r) - curve).tolist() for r in c["repeats"]]
        c["noise"] = (np.asarray(c["noise"]) - curve).tolist()
    return result


# ---------- Combining ----------

def combine(measurements):
    """Average every measurement (one per microphone position) per channel.

    Positions are averaged as power, which is what a listener moving their head
    hears; repeats within a position are kept separately for cross-validation.
    Returns per channel: mean (dB), repeats (each repeat averaged across the
    positions), spread (dB), snr, and the mean arrival time.
    """
    channels = len(measurements[0]["channels"])
    out = []
    for ch in range(channels):
        reps = [np.array(m["channels"][ch]["repeats"]) for m in measurements]
        count = min(len(r) for r in reps)
        # Repeat k across all positions is one complete, independent estimate.
        by_repeat = np.array([10 * np.log10(np.mean([10 ** (r[k] / 10) for r in reps], axis=0)) for k in range(count)])
        mean = 10 * np.log10(np.mean(10 ** (by_repeat / 10), axis=0))
        positions = np.array([10 * np.log10(np.mean(10 ** (r / 10), axis=0)) for r in reps])
        spread = np.std(np.vstack([by_repeat, positions]), axis=0) if len(reps) + count > 2 else np.zeros_like(mean)
        snr = np.min(np.array([m["channels"][ch]["snr"] for m in measurements]), axis=0)
        arrival = float(np.mean([np.mean(m["channels"][ch]["arrivals"]) for m in measurements]))
        out.append({"mean": mean, "repeats": by_repeat, "spread": spread, "snr": snr, "arrival": arrival})
        # Timing, from measurements made since it was kept: the group delay
        # weighted by how loud each sweep was there, and how far the
        # positions and repeats disagree on it.
        if all("delays" in m["channels"][ch] for m in measurements):
            delays = [np.array(m["channels"][ch]["delays"])[:count] for m in measurements]
            power = [10 ** (r[:count] / 10) for r in reps]

            def weighted(d, p):
                return np.sum(d * p, axis=0) / np.maximum(np.sum(p, axis=0), 1e-30)

            per_position = [weighted(d, p) for d, p in zip(delays, power)]
            per_repeat = [weighted(np.array([d[k] for d in delays]), np.array([p[k] for p in power])) for k in range(count)]
            out[-1]["delay"] = weighted(np.array(per_position), np.array([p.sum(axis=0) for p in power]))
            out[-1]["delay_spread"] = (np.std(np.vstack([per_repeat, per_position]), axis=0)
                                       if len(reps) + count > 2 else np.zeros_like(mean))

    # One reference level for all channels, so channel balance survives.
    ref_band = (GRID >= 300) & (GRID <= 3000)
    ref = np.mean([c["mean"][ref_band].mean() for c in out])
    for c in out:
        c["mean"] = c["mean"] - ref
        c["repeats"] = c["repeats"] - ref
    return out


# ---------- Targets ----------

def target_curve(name):
    """Target response in dB on GRID, 0 dB around 1 kHz.

    flat   neutral
    warm   -0.8 dB per octave tilt through 1 kHz, gentler on long listening
    room   in-room curve: +4 dB bass shelf below ~100 Hz and a gentle treble tilt
    Any other value is read as a text file of "frequency dB" lines.
    """
    octaves = np.log2(GRID / 1000.0)
    if name == "flat":
        return np.zeros_like(GRID)
    if name == "warm":
        return np.clip(-0.8 * octaves, -4.0, 3.0)
    if name == "room":
        shelf = 4.0 / (1 + (GRID / 100.0) ** 2)
        tilt = np.where(octaves > 0, -0.6 * octaves, 0.0)
        return shelf + tilt
    pairs = np.loadtxt(name, comments="#", ndmin=2)
    curve = np.interp(np.log(GRID), np.log(pairs[:, 0]), pairs[:, 1])
    return curve - np.interp(np.log(1000.0), np.log(pairs[:, 0]), pairs[:, 1])


# ---------- Filters ----------

def biquad_db(kind, freq, gain=0.0, q=0.707, fs=FS, freqs=GRID):
    """Magnitude in dB of an RBJ cookbook biquad, the same CamillaDSP uses."""
    b, den = biquad(kind, freq, gain, q, fs)
    z = np.exp(-1j * 2 * np.pi * freqs / fs)
    h = (b[0] + b[1] * z + b[2] * z ** 2) / (den[0] + den[1] * z + den[2] * z ** 2)
    return 20 * np.log10(np.abs(h) + 1e-15)


def biquad(kind, freq, gain=0.0, q=0.707, fs=FS):
    """RBJ cookbook coefficients (b, a), unnormalized."""
    w0 = 2 * np.pi * freq / fs
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2 * q)
    if kind == "Peaking":
        a = 10 ** (gain / 40)
        b = [1 + alpha * a, -2 * cw, 1 - alpha * a]
        den = [1 + alpha / a, -2 * cw, 1 - alpha / a]
    elif kind == "Highpass":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        den = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "Lowshelf":
        a = 10 ** (gain / 40)
        sa = 2 * np.sqrt(a) * alpha
        b = [a * ((a + 1) - (a - 1) * cw + sa), 2 * a * ((a - 1) - (a + 1) * cw), a * ((a + 1) - (a - 1) * cw - sa)]
        den = [(a + 1) + (a - 1) * cw + sa, -2 * ((a - 1) + (a + 1) * cw), (a + 1) + (a - 1) * cw - sa]
    else:
        raise ValueError(kind)
    return b, den


def peq_db(filters, freqs=GRID):
    total = np.zeros_like(freqs)
    for f in filters:
        total += biquad_db(f["type"], f["freq"], f.get("gain", 0.0), f.get("q", 0.707), freqs=freqs)
    return total


def find_knee(mean, target):
    """Where the speaker gives up: the highest frequency below 400 Hz that falls
    more than 12 dB short of the target. 0 when it reaches the bottom of the band."""
    # Smoothed by a third of an octave, so a narrow cancellation notch is not
    # mistaken for the speaker running out.
    short = (GRID < 400) & (smooth(mean, 3) - target < -12)
    if not short.any():
        return 0.0
    return float(np.clip(GRID[short].max(), 25.0, 200.0))


def fit_floor(mean, target, knee):
    """Lowest frequency the EQ works on: above the knee, and above where the
    response is still more than 6 dB short of the target.

    The roll-off between the two belongs to the protective high-pass. Fitting it
    would only invite a broad cut everywhere else, which after level matching is
    a bass boost in disguise.
    """
    short = (GRID < 400) & (smooth(mean, 3) - target < -6)
    floor = float(GRID[short].max()) * 1.1 if short.any() else F_LOW
    return max(F_LOW, knee * 1.2 if knee else F_LOW, floor)


def _weights(band, snr, spread):
    w = band.astype(float)
    w *= 1 / (1 + np.exp(-(snr - 12) / 3))          # trust only what stands above the noise
    w *= 1 / (1 + (spread / 2.0) ** 2)              # and what the repeats agree on
    return w


def _cost(residual, w):
    # A remaining peak is audible and worth removing; a dip of the same depth
    # is usually a cancellation at the microphone that moves with the head.
    r = np.where(residual > 0, residual, math.sqrt(DIP_WEIGHT) * residual)
    return np.sqrt(np.sum(w * r ** 2) / max(np.sum(w), 1e-9))


def correction_error(channel, target, filters, fmax=16000.0):
    """The weighted error fit_peq minimizes, for any complete filter list
    (high-pass included), so another correction can be scored the same way."""
    mean = channel["mean"]
    knee = find_knee(mean, target)
    hp = peq_db([f for f in filters if f["type"] == "Highpass"])
    goal = target + hp
    fmin = fit_floor(mean, target, knee)
    w = _weights((GRID >= fmin) & (GRID <= fmax), channel["snr"], channel["spread"])
    curve = mean + peq_db(filters)
    curve = curve + np.sum(w * (goal - curve)) / max(np.sum(w), 1e-9)
    return _cost(curve - goal, w)


def fit_peq(channel, target, max_filters=10, fmax=16000.0, boost_ok=True):
    """Cut-first parametric EQ for one channel.

    Returns (filters, info). filters always starts with the protective high-pass
    when the speaker has a knee.
    """
    mean, spread, snr = channel["mean"], channel["spread"], channel["snr"]
    knee = find_knee(mean, target)
    base = []
    if knee > 30:
        base.append({"type": "Highpass", "freq": round(knee, 1), "q": 0.707})
    hp = peq_db(base)
    # The high-pass is part of the plan, so the target includes it and no filter
    # is spent boosting back what was removed on purpose.
    goal = target + hp
    fmin = fit_floor(mean, target, knee)
    band = (GRID >= fmin) & (GRID <= fmax)
    w = _weights(band, snr, spread)
    boost_scale = np.clip((GRID / max(fmin * 2, 60)) ** 2, 0, 1)  # boosts are cone travel: cheap high, dear low

    def level(curve):
        return np.sum(w * (goal - curve)) / max(np.sum(w), 1e-9)

    def residual_for(filters):
        corrected = mean + hp + peq_db(filters)
        corrected = corrected + level(corrected)
        return corrected - goal

    filters = []
    best = _cost(residual_for(filters), w)
    start_cost = best
    for _ in range(max_filters):
        r = residual_for(filters)
        score = np.where(r > 0, r, -r * 0.5 * boost_scale) * w
        # Try the strongest few separate problems, not just the strongest: the
        # biggest one is sometimes a dip the budget will not let us fill.
        best_candidate = None
        for i in _candidate_peaks(score):
            gain = float(np.clip(-r[i] * 0.8, MAX_CUT_PER_FILTER, MAX_BOOST_PER_FILTER if boost_ok else 0.0))
            candidate = filters + [{"type": "Peaking", "freq": float(GRID[i]), "gain": gain, "q": _width_q(r, i)}]
            candidate = _refine(candidate, mean, hp, goal, w, fmin, fmax, boost_scale, boost_ok)
            cost = _cost(residual_for(candidate), w)
            if best_candidate is None or cost < best_candidate[1]:
                best_candidate = (candidate, cost)
        if best_candidate is None or best - best_candidate[1] < 0.05:
            break
        filters, best = best_candidate

    filters = _prune(filters, channel, hp, goal, w, level)
    final = _cost(residual_for(filters), w)
    info = {"knee": knee, "fmin": fmin, "fmax": fmax, "error_before": start_cost, "error_after": final}
    return base + filters, info


def _candidate_peaks(score, count=3, threshold=0.5):
    """Indices of the strongest local maxima of score, a third of an octave apart."""
    picked = []
    for i in np.argsort(score)[::-1]:
        if score[i] < threshold or len(picked) == count:
            break
        if all(abs(math.log2(GRID[i] / GRID[j])) > 1 / 3 for j in picked):
            picked.append(int(i))
    return picked


def _width_q(r, i):
    """Q from where the residual falls to half its value either side of i."""
    lo, hi = i, i
    while lo > 0 and abs(r[lo]) > abs(r[i]) / 2 and np.sign(r[lo]) == np.sign(r[i]):
        lo -= 1
    while hi < len(GRID) - 1 and abs(r[hi]) > abs(r[i]) / 2 and np.sign(r[hi]) == np.sign(r[i]):
        hi += 1
    bw = max(math.log2(GRID[hi] / GRID[lo]), 0.1)
    return float(np.clip(math.sqrt(2 ** bw) / (2 ** bw - 1), 0.5, 6.0))


def _refine(filters, mean, hp, goal, w, fmin, fmax, boost_scale, boost_ok):
    """Jointly optimize every peaking filter's frequency, gain and Q."""
    x0, lo, hi = [], [], []
    for f in filters:
        x0 += [math.log(f["freq"]), f["gain"], math.log(f["q"])]
        lo += [math.log(fmin), MAX_CUT_PER_FILTER, math.log(0.5)]
        hi += [math.log(fmax), MAX_BOOST_PER_FILTER if boost_ok else 0.0, math.log(6.0)]
    x0 = np.clip(x0, lo, hi)
    sw = np.sqrt(w)

    def unpack(x):
        return [{"type": "Peaking", "freq": math.exp(x[3 * k]), "gain": x[3 * k + 1], "q": math.exp(x[3 * k + 2])}
                for k in range(len(filters))]

    def resid(x):
        fs = unpack(x)
        corr = peq_db(fs)
        curve = mean + hp + corr
        curve = curve + np.sum(w * (goal - curve)) / max(np.sum(w), 1e-9)
        r = curve - goal
        r = np.where(r > 0, r, math.sqrt(DIP_WEIGHT) * r)
        terms = [sw * r]
        # Boosts are paid for: a little everywhere, a lot at low frequencies.
        # One term per filter, so the residual keeps a fixed length.
        prices = [0.3 * max(f["gain"], 0.0) / max(float(np.interp(math.log(f["freq"]), np.log(GRID), boost_scale)), 0.05)
                  for f in fs]
        terms.append(np.array(prices))
        # Total correction stays inside the budget at every frequency.
        terms.append(np.maximum(corr - MAX_TOTAL_BOOST, 0) * 3)
        terms.append(np.minimum(corr - MAX_TOTAL_CUT, 0) * 3)
        return np.concatenate(terms)

    sol = optimize.least_squares(resid, x0, bounds=(lo, hi), max_nfev=400)
    return unpack(sol.x)


def _prune(filters, channel, hp, goal, w, level):
    """Drop every filter that does not help each repeat on its own.

    The fit used the average; a filter that only fits the average's noise does
    not improve the individual repeats and is removed.
    """
    reps = channel["repeats"]
    if len(reps) < 2:
        return filters

    def held_out(fs):
        errs = []
        for rep in reps:
            curve = rep + hp + peq_db(fs)
            curve = curve + level(curve)
            errs.append(_cost(curve - goal, w))
        return max(errs)

    kept = list(filters)
    changed = True
    while changed and kept:
        changed = False
        base = held_out(kept)
        for k in range(len(kept)):
            trial = kept[:k] + kept[k + 1:]
            if held_out(trial) <= base + 0.02:
                kept = trial
                changed = True
                break
    return kept


def magnitude_correction(channel, target, fmax=18000.0):
    """The smoothed tone correction in dB on GRID, and the knee.

    Same budget as the PEQ: dips are only half filled, boosts and cuts are
    clamped, and the correction fades to 0 dB outside the measured band.
    """
    mean = smooth(channel["mean"], 6)
    knee = find_knee(mean, target)
    base = [{"type": "Highpass", "freq": knee, "q": 0.707}] if knee > 30 else []
    goal = target + peq_db(base)
    fmin = fit_floor(mean, target, knee)
    band = (GRID >= fmin) & (GRID <= fmax)
    level = np.mean((goal - mean)[band & (GRID >= 300) & (GRID <= 3000)]) if (band & (GRID >= 300)).any() else 0.0
    corr = goal - (mean + level) - peq_db(base)
    corr = np.where(corr > 0, corr * 0.5, corr)
    corr = np.clip(corr, MAX_TOTAL_CUT, MAX_TOTAL_BOOST)
    # Trust: fade out where the noise floor or the repeats say not to.
    trust = (1 / (1 + np.exp(-(channel["snr"] - 12) / 3))) / (1 + (channel["spread"] / 2.0) ** 2)
    corr *= trust
    fade_lo = np.clip(np.log2(GRID / fmin) * 2 + 1, 0, 1)
    fade_hi = np.clip(np.log2(fmax / GRID) * 2 + 1, 0, 1)
    corr *= fade_lo * fade_hi
    return knee, corr


def fir_correction(channel, target, taps=8192, fs=FS, fmax=18000.0):
    """Minimum-phase FIR that corrects the smoothed response to the target.

    Returns (taps array, knee, correction dB on GRID).
    """
    knee, corr = magnitude_correction(channel, target, fmax)
    n = taps * 2
    freqs = np.fft.rfftfreq(n, 1 / fs)
    mag_db = np.interp(np.log(np.maximum(freqs, F_LOW)), np.log(GRID), corr)
    taps_out = minimum_phase(10 ** (mag_db / 20), n)[:taps]
    taps_out *= np.hanning(2 * taps)[taps:] ** 0.5 if taps > 64 else 1.0
    return taps_out.astype(np.float32), knee, corr


def minimum_phase(magnitude, n):
    """Minimum-phase impulse response with the given rfft magnitude (cepstrum)."""
    return np.fft.irfft(minimum_phase_spectrum(magnitude, n), n)


def minimum_phase_spectrum(magnitude, n):
    """The minimum-phase rfft spectrum with the given magnitude."""
    log_mag = np.log(np.maximum(magnitude, 1e-8))
    cep = np.fft.irfft(log_mag, n)
    fold = np.zeros(n)
    fold[0] = cep[0]
    fold[1:n // 2] = 2 * cep[1:n // 2]
    fold[n // 2] = cep[n // 2]
    return np.exp(np.fft.rfft(fold, n))


# ---------- Timing ----------

# Room Correction brings the bass forward in time to arrive with the rest. It
# works below TIMING_FMAX, where a speaker's timing holds across the room;
# above it the same correction would help one spot and harm every other.
TIMING_FMAX = 500.0
# The added latency: the most the bass can be brought forward.
TIMING_LATENCY = 0.05
# Never by more than this many periods, which keeps the ringing the
# correction adds ahead of a bass note short enough to stay unheard.
TIMING_MAX_CYCLES = 1.0
# How far, in periods, the positions and repeats may disagree on the timing
# before it is trusted less.
TIMING_AGREEMENT = 0.1


def timing_lag(channel, filters, corr, fs=FS):
    """How late each frequency arrives, in ms on GRID, after the high-pass
    and the minimum-phase tone correction, relative to 500 Hz to 1.5 kHz."""
    lag = np.asarray(channel["delay"], dtype=float).copy()
    w = 2 * np.pi * GRID / fs
    for f in filters:
        b, a = biquad(f["type"], f["freq"], f.get("gain", 0.0), f.get("q", 0.707), fs)
        lag += signal.group_delay((b, a), w=w)[1] / fs * 1000
    n = 1 << 16
    freqs = np.fft.rfftfreq(n, 1 / fs)
    mag_db = np.interp(np.log(np.maximum(freqs, F_LOW)), np.log(GRID), corr)
    lag += _group_delay(minimum_phase_spectrum(10 ** (mag_db / 20), n), freqs, fraction=48)
    ref = (GRID >= 500) & (GRID <= 1500)
    return lag - np.mean(lag[ref])


def timing_error(lag, lo, hi=300.0):
    """RMS lateness in ms over lo..hi Hz, the bass a timing correction can reach."""
    band = (GRID >= max(lo, 30.0)) & (GRID <= hi)
    return float(np.sqrt(np.mean(lag[band] ** 2))) if band.any() else 0.0


def timing_correction(channel, target, taps=32768, fs=FS, fmax=18000.0):
    """FIR that corrects the tone like fir_correction and the timing of the bass.

    The bass is brought forward to arrive with the midrange, by at most
    TIMING_MAX_CYCLES periods and TIMING_LATENCY, which the whole filter is
    delayed by so it stays causal. Where the noise floor or the positions
    and repeats disagree, the timing is corrected less.
    Returns (taps array, knee, tone correction dB on GRID, info) with the
    lateness before and after in info.
    """
    if "delay" not in channel:
        raise MeasurementError("These measurements were made before Room Correction existed. Measure again to use it.")
    knee, corr = magnitude_correction(channel, target, fmax)
    filters = [{"type": "Highpass", "freq": knee, "q": 0.707}] if knee > 30 else []
    lag = timing_lag(channel, filters, corr, fs)

    # Where to correct: from just above the knee to TIMING_FMAX, half an
    # octave in at each end, and only what the measurements agree on.
    lo = max(knee, F_LOW * 1.25)
    fade = np.clip(np.log2(GRID / lo) * 2, 0, 1) * np.clip(np.log2(TIMING_FMAX / GRID) * 1.5, 0, 1)
    trust = 1 / (1 + np.exp(-(channel["snr"] - 12) / 3))
    # Disagreement swings from one frequency to the next; judge it over a
    # third of an octave, the span the shift is smoothed over.
    trust /= 1 + (_smooth_linear(channel["delay_spread"], 3) / 1000 * GRID / TIMING_AGREEMENT) ** 2
    shift = -lag * fade * trust
    limit = np.minimum(TIMING_MAX_CYCLES / GRID, TIMING_LATENCY * 0.9) * 1000
    shift = _smooth_linear(np.clip(shift, -limit, limit), 3)

    # Group delay to phase: integrate down from the top, where the shift is 0.
    n = taps * 2
    freqs = np.fft.rfftfreq(n, 1 / fs)
    gd = np.interp(np.log(np.maximum(freqs, F_LOW)), np.log(GRID), shift, left=0.0, right=0.0) / 1000
    gd[freqs < F_LOW] = 0.0
    df = freqs[1]
    phase = 2 * np.pi * (np.cumsum(gd[::-1])[::-1] - gd) * df
    # A real filter has a real response at 0 Hz: wind what the shift left
    # there to the nearest whole turn, below the band where nothing plays.
    corner = max(lo / 2, 8.0)
    below = freqs < corner
    turn = 2 * np.pi * round(phase[0] / (2 * np.pi))
    phase[below] += (turn - phase[0]) * (1 - freqs[below] / corner)

    mag_db = np.interp(np.log(np.maximum(freqs, F_LOW)), np.log(GRID), corr)
    spectrum = minimum_phase_spectrum(10 ** (mag_db / 20), n) * np.exp(1j * phase - 2j * np.pi * freqs * TIMING_LATENCY)
    out = np.fft.irfft(spectrum, n)[:taps]
    rise = int(0.002 * fs)
    out[:rise] *= 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, rise))
    out *= np.hanning(2 * taps)[taps:] ** 0.5
    info = {"timing_before": timing_error(lag, lo), "timing_after": timing_error(lag + shift, lo),
            "timing_from": lo, "latency_ms": TIMING_LATENCY * 1000, "shift": shift}
    return out.astype(np.float32), knee, corr, info


def _smooth_linear(values, fraction):
    """Plain fractional-octave moving average on GRID."""
    logf = np.log2(GRID)
    half = 0.5 / fraction
    return np.array([values[np.abs(logf - lf) <= half].mean() for lf in logf])


# ---------- Alignment ----------

def alignment(channels, spread_db=0.5):
    """Delay and level trims so both speakers arrive together and equally loud.

    Only meaningful with the microphone at the listening position. The later
    speaker is not delayed; the earlier one is. Levels are only ever turned down.
    """
    arrivals = [c["arrival"] for c in channels]
    latest = max(arrivals)
    band = (GRID >= 300) & (GRID <= 3000)
    levels = [float(np.mean(c["mean"][band])) for c in channels]
    quietest = min(levels)
    out = []
    for c, arrival, lvl in zip(channels, arrivals, levels):
        delay_ms = (latest - arrival) * 1000
        noise = float(np.median(c["spread"][band]))
        trim = -(lvl - quietest) if lvl - quietest > max(spread_db, noise) else 0.0
        out.append({"delay_ms": round(delay_ms, 3) if 0.04 < delay_ms < 10 else 0.0,
                    "trim_db": round(max(trim, -6.0), 2)})
    return out


# ---------- Output ----------

def preamp_for(corrections):
    """Input trim that keeps the loudest corrected frequency below 0 dBFS."""
    peak = max(float(np.max(c)) for c in corrections)
    return -round(max(peak, 0.0) + PREAMP_MARGIN, 2)


# The switches on a calibrated preset (see preset_yaml).
DEFAULT_OPTIONS = {"loudness": False, "fuller_bass": False, "louder": False, "deep_bass": False,
                   "calibration": True, "loudness_level": "volume"}
# How much quieter than the output's own volume the Loudness filter is told
# the music plays. "volume" follows the output alone; the others are for a
# volume set further on, on an amplifier, where the output sits at full.
LOUDNESS_LEVELS = {"volume": 0.0, "some": -12.0, "full": -25.0}
# Loudness compensation follows the listening level, which lib/loudness-tracker
# writes for camilladsp-omarchy to fill into the active config: the same two
# shelves CamillaDSP's own Loudness filter uses, at a share of their boost,
# with the mids turned down by the larger boost so it cannot clip. Full boost
# 20 dB below the reference. It is not CamillaDSP's Loudness filter, which
# would need its websocket, and that would be open to every local account.
LOUDNESS = {"reference_level": -5.0, "low_boost": 6.0, "high_boost": 3.0}
# Placeholders in a preset, replaced with gains when the config is rendered.
LOUDNESS_PLACEHOLDERS = ("__loudness_low__", "__loudness_high__", "__loudness_mid__")
# Fuller bass: a low shelf clear of the high-pass corner, paid for by an
# input trim of the same size so the bass it adds cannot clip.
FULLER_BASS_DB = 4.0
# Deep bass: what lies below its corner is driven into a hard clipper, and the
# harmonics it makes between the corner and three times it are mixed back.
DEEP_BASS_DRIVE_DB = 18.0
DEEP_BASS_CLIP_DB = -10.0
DEEP_BASS_MIX_DB = -12.0
MAX_MAKEUP_DB = 6.0
LIMIT_DB = -1.0


def loudness_gains(level_db):
    """Low shelf, high shelf and mid gains for a listening level in dB, as
    CamillaDSP's Loudness filter works them out with attenuate_mid."""
    share = min(max((LOUDNESS["reference_level"] - level_db) / 20.0, 0.0), 1.0)
    low, high = LOUDNESS["low_boost"] * share, LOUDNESS["high_boost"] * share
    return round(low, 2), round(high, 2), round(-max(low, high), 2)


def deep_bass_corner(knee, fmin):
    """Where Deep bass starts: where the speaker is about 6 dB short of the
    target, not only where it gives up (the 12 dB knee), so its harmonics
    cover the bass the speaker plays thinly as well as what it cannot play.

    fmin is fit_floor(), 1.1 times the 6 dB point. 0 when the speaker
    reaches the bottom of the measurement and nothing is missing.
    """
    corner = min(max(knee, fmin / 1.1), 200.0)
    return round(corner, 1) if corner > 30 else 0.0


def fuller_bass_freq(knee, corner):
    """The Fuller bass shelf: twice the high-pass corner, where the cone can
    still move freely, and not below the thinly played bass."""
    return round(min(max(2 * knee, corner, 60.0), 200.0), 1)


def correction_level(corrections):
    """How much the whole correction turns the sound down, in dB.

    The mean gain over the band that carries most of the loudness, 200 Hz to
    5 kHz, averaged over the channels. Negative when the correction is
    mostly cuts. "Make it louder" gives this back; "Calibration" off plays the
    plain speakers this much quieter, so switching only changes the tone.
    """
    band = (GRID >= 200) & (GRID <= 5000)
    return round(float(np.mean([np.mean(np.asarray(c)[band]) for c in corrections])), 2)


def preset_yaml(title, description, per_channel, preamp_db, fir_files=None, options=None, knee=0.0, level_db=0.0,
                deep_bass_hz=None, fuller_hz=None):
    """CamillaDSP preset for a calibration, with its four switches applied.

    per_channel: list of dicts with "filters" (PEQ list), "delay_ms", "trim_db".
    fir_files: optional list of WAV paths, one per channel.
    options: the switches (DEFAULT_OPTIONS for keys). knee is the lowest
    knee of the channels, level_db what correction_level() reported,
    deep_bass_hz where Deep bass starts (deep_bass_corner(), the knee when
    not given), fuller_hz where the Fuller bass shelf sits.

    Order: loudness, deep bass (ahead of the correction, so the EQ shapes the
    harmonics too), preamp, the per-channel correction, then make-up gain and a
    limiter whenever anything can push the signal past full scale.
    """
    opts = dict(DEFAULT_OPTIONS, **(options or {}))
    channels = len(per_channel)
    filters, mixers, steps = [], [], []

    def add_filter(name, kind, **params):
        filters.extend([f"  {name}:", f"    type: {kind}", "    parameters:"])
        filters.extend(f"      {k}: {v}" for k, v in params.items())
        return name

    def filter_step(names, chans=None):
        steps.append("  - type: Filter")
        if chans is not None:
            steps.append(f"    channels: [{', '.join(str(c) for c in chans)}]")
        steps.append(f"    names: [{', '.join(names)}]")

    if opts["loudness"]:
        low, high, mid = LOUDNESS_PLACEHOLDERS
        filter_step([add_filter("loudness_low", "Biquad", type="Lowshelf", freq=70, slope=12, gain=low),
                     add_filter("loudness_high", "Biquad", type="Highshelf", freq=3500, slope=12, gain=high),
                     add_filter("loudness_mid", "Gain", gain=mid)])

    corner = knee if deep_bass_hz is None else deep_bass_hz
    deep_bass = opts["deep_bass"] and corner > 30
    if deep_bass:
        # Each channel gets a copy on channel + n that becomes its harmonics.
        mixers += ["  deep_bass_split:", "    channels:", f"      in: {channels}", f"      out: {channels * 2}",
                   "    mapping:"]
        for dest in range(channels * 2):
            mixers += [f"      - dest: {dest}", "        sources:", f"          - channel: {dest % channels}"]
        mixers += ["  deep_bass_join:", "    channels:", f"      in: {channels * 2}", f"      out: {channels}",
                   "    mapping:"]
        for dest in range(channels):
            mixers += [f"      - dest: {dest}", "        sources:", f"          - channel: {dest}",
                       f"          - channel: {dest + channels}", f"            gain: {DEEP_BASS_MIX_DB}"]
        steps += ["  - type: Mixer", "    name: deep_bass_split"]
        names = [add_filter("deep_bass_below", "Biquad", type="Lowpass", freq=round(corner, 1), q=0.707),
                 add_filter("deep_bass_drive", "Gain", gain=DEEP_BASS_DRIVE_DB),
                 add_filter("deep_bass_clip", "Limiter", soft_clip="false", clip_limit=DEEP_BASS_CLIP_DB),
                 # Two high-passes: the fundamentals the clipper was fed must not
                 # come back, only what it made above them.
                 add_filter("deep_bass_above", "Biquad", type="Highpass", freq=round(corner, 1), q=0.707),
                 add_filter("deep_bass_above2", "Biquad", type="Highpass", freq=round(corner, 1), q=0.707),
                 add_filter("deep_bass_top", "Biquad", type="Lowpass", freq=round(corner * 3, 1), q=0.707)]
        filter_step(names, range(channels, channels * 2))
        steps += ["  - type: Mixer", "    name: deep_bass_join"]

    if opts["calibration"]:
        filter_step([add_filter("preamp", "Gain", gain=preamp_db)])
        for ch, spec in enumerate(per_channel):
            names = []
            for k, f in enumerate(spec["filters"]):
                params = {"type": f["type"], "freq": round(f["freq"], 1)}
                if f["type"] != "Highpass":
                    params["gain"] = round(f["gain"], 2)
                params["q"] = round(f["q"], 3)
                names.append(add_filter(f"ch{ch}_{f['type'].lower()}_{k}", "Biquad", **params))
            if fir_files:
                names.append(add_filter(f"ch{ch}_fir", "Conv", type="Wav", filename=yaml_str(fir_files[ch])))
            if spec.get("delay_ms"):
                names.append(add_filter(f"ch{ch}_delay", "Delay", delay=spec["delay_ms"], unit="ms",
                                        subsample="true"))
            if spec.get("trim_db"):
                names.append(add_filter(f"ch{ch}_trim", "Gain", gain=spec["trim_db"]))
            if names:
                filter_step(names, [ch])
    elif level_db:
        # The plain speakers, as loud as the calibration plays them.
        filter_step([add_filter("level_match", "Gain", gain=level_db)])

    fuller = opts["fuller_bass"]
    if fuller:
        shelf = fuller_hz if fuller_hz else fuller_bass_freq(knee, corner)
        filter_step([add_filter("fuller_trim", "Gain", gain=-FULLER_BASS_DB),
                     add_filter("fuller_bass", "Biquad", type="Lowshelf", freq=shelf, gain=FULLER_BASS_DB, q=0.707)])

    # Make it louder gives back what the correction took and what Fuller bass
    # trimmed; the limiter catches what that pushes past full scale.
    makeup = (min(max(-level_db, 0.0), MAX_MAKEUP_DB) + (FULLER_BASS_DB if fuller else 0.0)) if opts["louder"] else 0.0
    if makeup:
        filter_step([add_filter("makeup", "Gain", gain=round(makeup, 2))])
    if opts["louder"] or deep_bass or opts["loudness"] or fuller:
        # Hard clipping: CamillaDSP's soft clip bends the signal well below the
        # limit, which measured as distortion on clean music; hard leaves
        # everything under -1 dBFS untouched.
        filter_step([add_filter("limiter", "Limiter", soft_clip="false", clip_limit=LIMIT_DB)])

    lines = [f"title: {yaml_str(title)}", f"description: {yaml_str(description)}", "filters:"] + filters
    if mixers:
        lines += ["mixers:"] + mixers
    return "\n".join(lines + ["pipeline:"] + steps) + "\n"


def yaml_str(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
