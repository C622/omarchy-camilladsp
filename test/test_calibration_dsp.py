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
