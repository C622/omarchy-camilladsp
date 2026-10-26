"""Tests for lib/calibration_dsp.py against a simulated speaker and room.

Run with: python3 -m unittest discover -s test
"""

import os
import sys
import unittest

import numpy as np
from scipy import signal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import calibration_dsp as dsp  # noqa: E402

FS = dsp.FS


def rbj_sos(kind, freq, gain=0.0, q=0.707):
    w0 = 2 * np.pi * freq / FS
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2 * q)
    if kind == "Peaking":
        a = 10 ** (gain / 40)
        b = [1 + alpha * a, -2 * cw, 1 - alpha * a]
        den = [1 + alpha / a, -2 * cw, 1 - alpha / a]
    else:
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        den = [1 + alpha, -2 * cw, 1 - alpha]
    return np.array(b + den) / den[0]


# A small speaker: nothing below ~150 Hz, a boxy resonance, a dip, a sharp
# treble peak. Left and right differ a little, as real ones do.
SPEAKER = [
    [("Highpass", 150, 0, 0.9), ("Peaking", 900, 8, 3.0), ("Peaking", 3000, -6, 2.0), ("Peaking", 6500, 5, 4.0)],
    [("Highpass", 160, 0, 0.9), ("Peaking", 850, 7, 3.0), ("Peaking", 3200, -5, 2.0), ("Peaking", 6000, 6, 4.0)],
]


def speaker_db(ch):
    return dsp.peq_db([{"type": k, "freq": f, "gain": g, "q": q} for k, f, g, q in SPEAKER[ch]])


def simulate(played, delays_ms=(2.0, 2.6), latency=4321, noise_db=-75, gain_db=-6, seed=1):
    """What a microphone would record when `played` goes through the speakers."""
    rng = np.random.default_rng(seed)
    out = np.zeros(len(played) + latency + FS)
    for ch in range(played.shape[1]):
        sos = np.array([rbj_sos(k, f, g, q) for k, f, g, q in SPEAKER[ch]])
        y = signal.sosfilt(sos, played[:, ch].astype(np.float64)) * 10 ** (gain_db / 20)
        d = latency + int(delays_ms[ch] * FS / 1000)
        out[d:d + len(y)] += y
        # A desk reflection 1.5 ms later and a late room reflection at 25 ms.
        r1 = d + int(0.0015 * FS)
        out[r1:r1 + len(y)] += 0.25 * y
        r2 = d + int(0.025 * FS)
        out[r2:r2 + len(y)] += 0.3 * y
    out += rng.normal(0, 10 ** (noise_db / 20), len(out))
    return out


class AnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.played, cls.schedule = dsp.excitation(level_db=-12)
        cls.result = dsp.analyze(simulate(cls.played), cls.played, cls.schedule)
        cls.channels = dsp.combine([cls.result])

    def test_recovers_the_speaker_response(self):
        band = (dsp.GRID > 300) & (dsp.GRID < 12000)
        for ch in range(2):
            measured = dsp.smooth(self.channels[ch]["mean"], 6)
            truth = dsp.smooth(speaker_db(ch), 6)
            diff = (measured - truth)[band]
            diff -= diff.mean()
            # The desk reflection is inside the window and colours the curve a
            # little; the late room reflection must not.
            self.assertLess(np.sqrt(np.mean(diff ** 2)), 1.5, f"channel {ch}")

    def test_finds_the_arrival_difference(self):
        delta = (self.channels[1]["arrival"] - self.channels[0]["arrival"]) * 1000
        self.assertAlmostEqual(delta, 0.6, delta=0.05)

    def test_snr_is_high_for_a_clean_recording(self):
        mid = (dsp.GRID > 200) & (dsp.GRID < 5000)
        self.assertGreater(np.median(np.array(self.result["channels"][0]["snr"])[mid]), 30)

    def test_peq_fit_flattens_the_speaker(self):
        target = dsp.target_curve("flat")
        for ch in range(2):
            filters, info = dsp.fit_peq(self.channels[ch], target)
            self.assertEqual(filters[0]["type"], "Highpass")
            self.assertGreater(filters[0]["freq"], 60)
            self.assertLess(filters[0]["freq"], 200)
            self.assertLess(info["error_after"], info["error_before"] * 0.6, info)
            # What is left is the room (the desk reflection's comb, which must
            # not be boosted) and the roll-off: even the exact inverse of the
            # simulated speaker does no better than the fit.
            exact = [filters[0]] + [{"type": "Peaking", "freq": f, "gain": -g, "q": q}
                                    for k, f, g, q in SPEAKER[ch] if k == "Peaking"]
            self.assertLessEqual(info["error_after"], dsp.correction_error(self.channels[ch], target, exact) * 1.05)
            # The roll-off is left to the high-pass, not fitted.
            self.assertGreater(info["fmin"], 100, info)
            peqs = [f for f in filters if f["type"] == "Peaking"]
            self.assertLessEqual(len(peqs), 10)
            for f in peqs:
                self.assertGreaterEqual(f["gain"], dsp.MAX_CUT_PER_FILTER - 1e-6)
                self.assertLessEqual(f["gain"], dsp.MAX_BOOST_PER_FILTER + 1e-6)
            # The biggest problem, the resonance near 900 Hz, gets cut.
            near = [f for f in peqs if 600 < f["freq"] < 1300]
            self.assertTrue(near and min(f["gain"] for f in near) < -4, peqs)

    def test_fir_correction_stays_in_budget(self):
        taps, knee, corr = dsp.fir_correction(self.channels[0], dsp.target_curve("warm"))
        self.assertEqual(len(taps), 8192)
        self.assertTrue(np.isfinite(taps).all())
        self.assertLessEqual(corr.max(), dsp.MAX_TOTAL_BOOST + 1e-6)
        self.assertGreaterEqual(corr.min(), dsp.MAX_TOTAL_CUT - 1e-6)
        # The FIR does what the curve says around the resonance.
        n = 1 << 16
        h = 20 * np.log10(np.abs(np.fft.rfft(taps, n)) + 1e-12)
        f = np.fft.rfftfreq(n, 1 / FS)
        at = lambda hz: float(np.interp(hz, f, h))  # noqa: E731
        self.assertAlmostEqual(at(900), float(np.interp(900, dsp.GRID, corr)), delta=1.5)

    def test_alignment_delays_the_earlier_speaker(self):
        align = dsp.alignment(self.channels)
        self.assertAlmostEqual(align[0]["delay_ms"], 0.6, delta=0.05)
        self.assertEqual(align[1]["delay_ms"], 0.0)

    def test_preset_is_valid_yaml_shape(self):
        target = dsp.target_curve("flat")
        per = []
        corrections = []
        for ch in range(2):
            filters, _ = dsp.fit_peq(self.channels[ch], target)
            per.append({"filters": filters, "delay_ms": 0.0, "trim_db": 0.0})
            corrections.append(dsp.peq_db([f for f in filters if f["type"] == "Peaking"]))
        text = dsp.preset_yaml("Calibrated", "test", per, dsp.preamp_for(corrections))
        self.assertIn("pipeline:", text)
        self.assertIn("channels: [0]", text)
        self.assertIn("channels: [1]", text)


class MicCalibrationTest(unittest.TestCase):
    def write(self, text):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def umik(self, curve):
        lines = ['"Sens Factor =-1.378dB, AGain =18dB, SERNO: 7103946"']
        lines += [f"{f:.3f}\t{curve(f):.3f}" for f in np.geomspace(10, 20000, 200)]
        return self.write("\n".join(lines) + "\n")

    def test_reads_a_umik_file(self):
        cal = dsp.read_mic_calibration(self.umik(lambda f: 0.0))
        self.assertEqual(cal["serial"], "7103946")
        self.assertAlmostEqual(cal["sensitivity"], -1.378)
        self.assertEqual(len(cal["freq"]), 200)

    def test_reads_a_rew_file_with_phase(self):
        text = "* REW calibration\n* Freq(Hz) SPL(dB) Phase(degrees)\n"
        text += "".join(f"{f:.1f}, {1.5:.2f}, 0.0\n" for f in np.geomspace(20, 20000, 50))
        cal = dsp.read_mic_calibration(self.write(text))
        self.assertTrue(np.allclose(cal["db"], 1.5))

    def test_refuses_other_files(self):
        with self.assertRaises(ValueError):
            dsp.read_mic_calibration(self.write("Shopping list\n2 eggs\n1 milk\n"))
        with self.assertRaises(ValueError):
            dsp.read_mic_calibration(self.write("".join(f"{f} 0\n" for f in range(1000, 2000, 50))))

    def test_removes_the_microphone_from_the_measurement(self):
        # This microphone over-reads the treble by up to 6 dB.
        rise = lambda f: 6.0 / (1 + (8000.0 / f) ** 2)  # noqa: E731
        cal = dsp.read_mic_calibration(self.umik(rise))
        played, schedule = dsp.excitation(level_db=-12)
        rec = simulate(played)
        # Colour the recording the way the microphone would.
        n = 1 << int(np.ceil(np.log2(len(rec))))
        spec = np.fft.rfft(rec, n)
        f = np.fft.rfftfreq(n, 1 / FS)
        spec *= 10 ** (np.array([rise(x) if x > 0 else 0 for x in f]) / 20)
        coloured = np.fft.irfft(spec, n)[:len(rec)]

        band = (dsp.GRID > 2000) & (dsp.GRID < 16000)

        def treble_error(result):
            c = dsp.combine([result])[0]
            diff = dsp.smooth(c["mean"], 6) - dsp.smooth(speaker_db(0), 6)
            diff -= diff[(dsp.GRID > 300) & (dsp.GRID < 1000)].mean()
            return np.abs(diff[band]).max()

        raw = dsp.analyze(coloured, played, schedule)
        self.assertGreater(treble_error(raw), 4)
        corrected = dsp.apply_mic_calibration(raw, dsp.mic_calibration_curve(cal))
        self.assertLess(treble_error(corrected), 2)


class SwitchesTest(unittest.TestCase):
    PER = [{"filters": [{"type": "Highpass", "freq": 90.0, "q": 0.707},
                        {"type": "Peaking", "freq": 200.0, "gain": -6.0, "q": 2.0}], "delay_ms": 0.0, "trim_db": 0.0}] * 2

    def preset(self, **options):
        return dsp.preset_yaml("t", "d", self.PER, -0.5, options=options, knee=90.0, level_db=-3.0)

    def test_defaults_are_the_plain_calibration(self):
        text = self.preset()
        self.assertIn("ch0_peaking_1", text)
        for name in ("loudness", "deep_bass", "makeup", "limiter", "level_match"):
            self.assertNotIn(name + ":", text)

    def test_calibration_off_is_level_matched(self):
        text = self.preset(calibration=False)
        self.assertNotIn("ch0_peaking", text)
        self.assertIn("level_match:", text)
        self.assertIn("gain: -3.0", text)

    def test_louder_gives_back_what_the_correction_took(self):
        text = self.preset(louder=True)
        self.assertIn("makeup:", text)
        self.assertIn("gain: 3.0", text)
        self.assertIn("limiter:", text)
        self.assertIn("soft_clip: false", text)

    def test_deep_bass_needs_a_knee(self):
        self.assertIn("deep_bass_split", self.preset(deep_bass=True))
        text = dsp.preset_yaml("t", "d", self.PER, -0.5, options={"deep_bass": True}, knee=0.0, level_db=-3.0)
        self.assertNotIn("deep_bass", text)

    def test_loudness_is_shelves_filled_in_at_render_time(self):
        text = self.preset(loudness=True)
        self.assertNotIn("type: Loudness", text)
        for placeholder in dsp.LOUDNESS_PLACEHOLDERS:
            self.assertIn(placeholder, text)
        self.assertIn("type: Lowshelf", text)
        self.assertIn("type: Highshelf", text)
        # No boost at the reference level, all of it 20 dB below.
        self.assertEqual(dsp.loudness_gains(-5.0), (0.0, 0.0, 0.0))
        self.assertEqual(dsp.loudness_gains(-15.0), (3.0, 1.5, -3.0))
        self.assertEqual(dsp.loudness_gains(-60.0), (6.0, 3.0, -6.0))

    def test_fuller_bass_is_a_trimmed_shelf(self):
        text = dsp.preset_yaml("t", "d", self.PER, -0.5, options={"fuller_bass": True}, knee=46.0,
                               level_db=-3.0, deep_bass_hz=80.0, fuller_hz=92.0)
        self.assertIn("fuller_bass:", text)
        self.assertIn("type: Lowshelf", text)
        self.assertIn("freq: 92.0", text)
        self.assertIn("gain: -4.0", text)
        self.assertIn("limiter:", text)
        # Make it louder gives the trim back as well as the correction's 3 dB.
        louder = dsp.preset_yaml("t", "d", self.PER, -0.5, options={"fuller_bass": True, "louder": True},
                                 knee=46.0, level_db=-3.0, deep_bass_hz=80.0, fuller_hz=92.0)
        self.assertIn("gain: 7.0", louder)

    def test_deep_bass_starts_where_the_speaker_plays_thinly(self):
        self.assertEqual(dsp.deep_bass_corner(46.0, 88.0), 80.0)
        self.assertEqual(dsp.deep_bass_corner(0.0, dsp.F_LOW), 0.0)
        text = dsp.preset_yaml("t", "d", self.PER, -0.5, options={"deep_bass": True}, knee=46.0,
                               level_db=-3.0, deep_bass_hz=80.0)
        self.assertIn("freq: 240.0", text)

    def test_correction_level_is_the_midband_mean(self):
        corr = np.where((dsp.GRID >= 200) & (dsp.GRID <= 5000), -4.0, 10.0)
        self.assertAlmostEqual(dsp.correction_level([corr, corr]), -4.0)


# A bass-reflex speaker: a fourth-order roll-off at 45 Hz, which makes the
# deep bass arrive late, and a box resonance.
PORTED = [("Highpass", 45, 0, 0.707), ("Highpass", 45, 0, 0.707), ("Peaking", 900, 4, 3.0)]


def ported_recording(played, reflections=((0.0015, 0.25),), shift_ms=0.0, seed=3):
    rng = np.random.default_rng(seed)
    sos = np.array([rbj_sos(k, f, g, q) for k, f, g, q in PORTED])
    out = np.zeros(len(played) + 4321 + FS)
    for ch in range(played.shape[1]):
        y = signal.sosfilt(sos, played[:, ch].astype(np.float64)) * 0.5
        d = 4321 + int((2.0 + 0.6 * ch) * FS / 1000)
        out[d:d + len(y)] += y
        for delay, gain in reflections:
            r = d + int((delay + shift_ms / 1000) * FS)
            out[r:r + len(y)] += gain * y
    return out + rng.normal(0, 10 ** (-75 / 20), len(out))


def chain_lag(taps, extra):
    """Lateness in ms on GRID of the ported speaker, the given biquads, then taps."""
    imp = np.zeros(1 << 17)
    imp[0] = 1.0
    for k, f, g, q in PORTED + extra:
        imp = signal.sosfilt(rbj_sos(k, f, g, q)[None, :], imp)
    if taps is not None:
        imp = signal.fftconvolve(imp, taps.astype(np.float64))[:len(imp)]
    f = np.fft.rfftfreq(len(imp), 1 / FS)
    lag = dsp._group_delay(np.fft.rfft(imp), f)
    return lag - np.mean(lag[(dsp.GRID >= 500) & (dsp.GRID <= 1500)])


class TimingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.played, cls.schedule = dsp.excitation(level_db=-12)
        cls.channels = dsp.combine([dsp.analyze(ported_recording(cls.played), cls.played, cls.schedule)])

    def test_measures_how_late_the_bass_arrives(self):
        truth = chain_lag(None, [])
        lag = self.channels[0]["delay"] - np.mean(self.channels[0]["delay"][(dsp.GRID >= 500) & (dsp.GRID <= 1500)])
        for hz in (50, 80, 150, 300):
            self.assertAlmostEqual(float(np.interp(hz, dsp.GRID, lag)), float(np.interp(hz, dsp.GRID, truth)),
                                   delta=0.6, msg=f"{hz} Hz")

    def test_room_correction_brings_the_bass_in_time(self):
        taps, knee, corr, info = dsp.timing_correction(self.channels[0], dsp.target_curve("flat"))
        self.assertEqual(len(taps), 32768)
        self.assertTrue(np.isfinite(taps).all())
        hp = [("Highpass", knee, 0, 0.707)]
        before, after = chain_lag(None, hp), chain_lag(taps, hp)
        band = (dsp.GRID >= 60) & (dsp.GRID <= 300)
        self.assertGreater(np.max(before[band]), 5.0)
        self.assertLess(np.max(np.abs(after[band])), 1.0)
        self.assertLess(info["timing_after"], info["timing_before"] / 2)
        # The midrange and treble are left alone, apart from the latency.
        lag = dsp._group_delay(np.fft.rfft(taps.astype(np.float64), 1 << 17), np.fft.rfftfreq(1 << 17, 1 / FS))
        treble = (dsp.GRID >= 1000) & (dsp.GRID <= 15000)
        self.assertLess(np.max(np.abs(lag[treble] - dsp.TIMING_LATENCY * 1000)), 0.3)

    def test_room_correction_rings_little_ahead_of_a_note(self):
        taps, knee, _corr, _info = dsp.timing_correction(self.channels[0], dsp.target_curve("flat"))
        # Nothing before the start, and little energy ahead of the main peak.
        self.assertLess(np.sum(taps[:48] ** 2) / np.sum(taps ** 2), 1e-6)
        peak = int(np.argmax(np.abs(taps)))
        self.assertAlmostEqual(peak / FS, dsp.TIMING_LATENCY, delta=0.002)
        self.assertLess(np.sum(taps[:peak - 48] ** 2) / np.sum(taps ** 2), 0.01)

    def test_room_correction_tone_matches_detailed_eq(self):
        taps, _knee, corr, _info = dsp.timing_correction(self.channels[0], dsp.target_curve("warm"))
        n = 1 << 17
        h = 20 * np.log10(np.abs(np.fft.rfft(taps.astype(np.float64), n)) + 1e-12)
        f = np.fft.rfftfreq(n, 1 / FS)
        for hz in (100, 900, 5000):
            self.assertAlmostEqual(float(np.interp(hz, f, h)), float(np.interp(hz, dsp.GRID, corr)), delta=1.0)

    def test_positions_that_disagree_are_corrected_less(self):
        # Two spots with different late reflections disagree on the timing
        # above the bass. Averaged, less is corrected there than for either
        # spot alone, which would fit the correction to that one spot.
        spots = [dsp.analyze(ported_recording(self.played, ((0.0015, 0.25), (0.004 + k * 0.003, 0.5)), seed=k),
                             self.played, self.schedule) for k in range(2)]
        mid = (dsp.GRID >= 120) & (dsp.GRID <= 400)

        def moved(measurements):
            info = dsp.timing_correction(dsp.combine(measurements)[0], dsp.target_curve("flat"))[3]
            self.assertLessEqual(np.max(-info["shift"]), dsp.TIMING_LATENCY * 1000)
            return np.mean(np.abs(info["shift"][mid]))

        self.assertGreater(np.max(dsp.combine(spots)[0]["delay_spread"][mid]), 1.0)
        self.assertLess(moved(spots), min(moved([spots[0]]), moved([spots[1]])))

    def test_older_measurements_are_refused(self):
        old = dict(self.channels[0])
        del old["delay"], old["delay_spread"]
        with self.assertRaises(dsp.MeasurementError):
            dsp.timing_correction(old, dsp.target_curve("flat"))


class SafetyTest(unittest.TestCase):
    def test_clipping_is_refused(self):
        played, schedule = dsp.excitation(level_db=-12)
        rec = simulate(played, gain_db=20)
        with self.assertRaises(dsp.MeasurementError):
            dsp.analyze(np.clip(rec, -1, 1), played, schedule)

    def test_silence_is_refused(self):
        played, schedule = dsp.excitation(level_db=-12)
        rec = np.random.default_rng(0).normal(0, 1e-3, len(played) + FS)
        with self.assertRaises(dsp.MeasurementError):
            dsp.analyze(rec, played, schedule)

    def test_noisy_room_is_refused(self):
        played, schedule = dsp.excitation(level_db=-40)
        with self.assertRaises(dsp.MeasurementError):
            dsp.analyze(simulate(played, noise_db=-45, gain_db=-20), played, schedule)

    def test_level_choice_respects_the_ceiling(self):
        self.assertEqual(dsp.choose_level(-35, -60, -90), -6.0)
        self.assertAlmostEqual(dsp.choose_level(-35, -20, -90), -27.0)
        with self.assertRaises(dsp.MeasurementError):
            dsp.choose_level(-35, -88, -90)

    def test_flat_speaker_gets_almost_nothing(self):
        played, schedule = dsp.excitation(level_db=-12)
        global SPEAKER
        saved = SPEAKER
        SPEAKER = [[("Peaking", 1000, 0, 1.0)], [("Peaking", 1000, 0, 1.0)]]
        try:
            rec = np.zeros(len(played) + 4321 + FS)
            for ch in range(2):
                rec[4321:4321 + len(played)] += played[:, ch] * 0.5
            rec += np.random.default_rng(2).normal(0, 1e-4, len(rec))
            channels = dsp.combine([dsp.analyze(rec, played, schedule)])
            filters, info = dsp.fit_peq(channels[0], dsp.target_curve("flat"))
        finally:
            SPEAKER = saved
        self.assertEqual([f for f in filters if f["type"] == "Highpass"], [])
        self.assertLessEqual(len(filters), 1, filters)
        self.assertLess(info["error_after"], 0.5)


if __name__ == "__main__":
    unittest.main()
