// Tests for Model.js, the panel's pure helpers. Run with: node test/model.test.js
const assert = require("assert")
const fs = require("fs")
const path = require("path")

// Model.js is a QML JavaScript library; drop the pragma and load it as CommonJS.
const source = fs.readFileSync(path.join(__dirname, "..", "Model.js"), "utf8").replace(/^\.pragma library\s*/, "")
const module_ = { exports: {} }
new Function("module", source)(module_)
const M = module_.exports

const base = {
  installed: true, pipewire: true, setup: true, service: "active", defaultSink: "camilladsp",
  target: "alsa_output.speakers", targetDescription: "Speakers", preset: "10-bass-boost",
  presets: [{ id: "00-flat", title: "Flat" }, { id: "10-bass-boost", title: "Bass boost" }],
  loudnessLevel: null,
}
const tests = {
  "state follows setup, service and routing"() {
    assert.strictEqual(M.state(null, false), "loading")
    assert.strictEqual(M.state({ ...base, setup: false }, true), "setup")
    assert.strictEqual(M.state({ ...base, pipewire: false }, true), "setup")
    assert.strictEqual(M.state({ ...base, service: "inactive" }, false), "off")
    assert.strictEqual(M.state({ ...base, service: "failed" }, false), "failed")
    assert.strictEqual(M.state(base, false), "bypassed")
    assert.strictEqual(M.state(base, true), "on")
  },
  "hero status says what is running"() {
    assert.strictEqual(M.heroStatus(base, true), "On")
    assert.strictEqual(M.heroStatus({ ...base, pipewire: false }, true), "Needs the PipeWire backend")
  },
  "bypass names where audio goes instead"() {
    assert.match(M.bypassReason({ ...base, defaultSink: "alsa_output.headphones" }), /alsa_output.headphones/)
    assert.strictEqual(M.bypassReason(base), "")
  },
  "tooltip names the preset when on"() {
    assert.strictEqual(M.tooltip(base, true), "CamillaDSP: Bass boost")
    assert.strictEqual(M.tooltip({ ...base, service: "inactive" }, false), "CamillaDSP: off")
  },
  "microphones are labelled and unusable ones flagged"() {
    const opts = M.usableMics({ mics: [
      { name: "a", description: "UMIK-1", usable: true, builtin: false, muted: false },
      { name: "b", description: "Headset", usable: false, reason: "too narrowband", builtin: false },
      { name: "c", description: "Laptop", usable: true, builtin: true, muted: true },
    ] })
    assert.deepStrictEqual(opts.map(o => o.usable), [true, false, false])
    assert.strictEqual(opts[1].label, "Headset (too narrowband)")
    assert.strictEqual(opts[2].label, "Laptop (muted)")
  },
  "calibration file options and hints"() {
    const devices = { calibration_files: [{ path: "/c/7103946_90deg.txt", label: "Serial 7103946 (90°)" }] }
    const devices2 = { calibration_files: [{ path: "/c/7103946_90deg.txt", file: "7103946_90deg.txt", label: "Serial 7103946 (90°)" }] }
    // Keyed by file name, so the copy in the plugin's folder still matches a pick from Downloads.
    assert.deepStrictEqual(M.calibrationOptions(devices2).map(o => o.value), ["", "7103946_90deg.txt"])
    assert.strictEqual(M.calibrationPath(devices2, "7103946_90deg.txt"), "/c/7103946_90deg.txt")
    assert.strictEqual(M.calibrationPath(devices2, ""), "")
    const umik = { name: "alsa_input.usb-miniDSP_Umik-1_Gain__18dB_00002-00.analog-stereo", description: "Umik-1" }
    assert.match(M.calibrationHint(umik, [{ value: "" }]), /Download the calibration file/)
    assert.strictEqual(M.calibrationHint(umik, M.calibrationOptions(devices)), "")
    assert.match(M.calibrationHint({ ...umik, calibration: { file: "7103946_90deg.txt", serial: "7103946" } }, []), /ceiling/)
    assert.match(M.calibrationHint({ ...umik, calibration: { file: "7103946.txt", serial: "7103946" } }, []), /at the speakers/)
    assert.strictEqual(M.calibrationHint({ name: "laptop", description: "Built-in" }, [{ value: "" }]), "")
  },
  "progress lines parse, other output does not"() {
    assert.deepStrictEqual(M.event('{"event": "progress", "fraction": 0.5}'), { event: "progress", fraction: 0.5 })
    assert.strictEqual(M.event("warning: something"), null)
    assert.strictEqual(M.event('{"no": "event"}'), null)
  },
  "fit and check summaries average the channels"() {
    const fit = { mode: "peq", channels: [
      { error_before: 3, error_after: 1, filters: [1, 2, 3] }, { error_before: 2, error_after: 1.2, filters: [1, 2] }] }
    assert.strictEqual(M.fitSummary(fit), "Off target by 2.5 dB before, 1.1 dB planned (5 filters)")
    assert.strictEqual(M.fitSummary({ ...fit, mode: "fir" }), "Off target by 2.5 dB before, 1.1 dB planned (FIR)")
    assert.strictEqual(M.checkSummary({ channels: [{ error_before: 4, error_after: 1, plan_deviation: 0.3 }] }),
      "Checked here: 4.0 dB off target without, 1.0 dB with; the correction matches the plan")
    assert.match(M.checkSummary({ channels: [{ error_after: 1, plan_deviation: 3 }] }), /measure again/)
  },
  "target curves match the calibrator's"() {
    // Reference values from target_curve() in lib/calibration_dsp.py.
    const hz = [20, 50, 100, 300, 1000, 3000, 10000, 20000]
    const expected = {
      flat: [0, 0, 0, 0, 0, 0, 0, 0],
      warm: [3.0, 3.0, 2.658, 1.39, 0.0, -1.268, -2.658, -3.458],
      room: [3.846, 3.2, 2.0, 0.4, 0.037, -0.947, -1.993, -2.593],
    }
    for (const [name, values] of Object.entries(expected))
      hz.forEach((f, i) => assert.ok(Math.abs(M.targetDb(name, f) - values[i]) < 0.01, `${name} at ${f} Hz`))
    const grid = M.logGrid(4)
    assert.ok(Math.abs(grid[0] - 20) < 1e-9 && Math.abs(grid[3] - 20000) < 1e-6)
    assert.strictEqual(M.targetCurve("warm", grid).length, 4)
  },
  "calibration presets are named per combination"() {
    // Must match preset_id() in lib/calibrate.
    assert.strictEqual(M.calibrationId("flat", "peq"), "80-cal-peq-flat")
    assert.strictEqual(M.calibrationId("room", "fir"), "80-cal-fir-room")
    assert.ok(M.isCalibrated("80-cal-fir-warm"))
    assert.ok(M.isCalibrated("80-calibrated"))
    assert.ok(!M.isCalibrated("10-bass-boost"))
    assert.ok(!M.isCalibrated("80-calibrated-but-mine"))
  },
  "smoothing softens spikes and keeps flat curves flat"() {
    assert.deepStrictEqual(M.smoothCurve([1, 1, 1, 1, 1]), [1, 1, 1, 1, 1])
    const spike = M.smoothCurve([0, 0, 0, 9, 0, 0, 0])
    assert.ok(spike[3] < 4 && spike[2] > 0 && spike[4] > 0)
    assert.strictEqual(spike.length, 7)
  },
  "mic summary and switches"() {
    const mic = { description: "Umik-1  Gain  18dB Analog Stereo", calibration: { file: "7157757_90deg.txt" } }
    assert.strictEqual(M.micSummary(mic, "listening"), "Umik-1 Gain 18dB Analog Stereo · 90° file · where I sit")
    assert.deepStrictEqual(M.SWITCHES.map(s => s.option), ["loudness", "fuller-bass", "louder", "deep-bass", "calibration"])
    assert.strictEqual(M.fitKnee({ channels: [{ knee: 0 }, { knee: 95 }, { knee: 80 }] }), 80)
    assert.strictEqual(M.fitKnee({ channels: [{ knee: 0 }] }), 0)
  },
  "theme palette is read from colors.toml"() {
    const colors = M.themeColors('mode = "dark"\naccent = "#89b4fa"\nred = "#f38ba8"\n# green = "#000000"\nbright_red = "#F38BA8ff"\n')
    assert.deepStrictEqual(colors, { accent: "#89b4fa", red: "#f38ba8", bright_red: "#F38BA8ff" })
    assert.ok(M.SWITCHES.every(s => s.color))
    // No two switches share a hue, counting orange as the red it falls back to.
    const hues = M.SWITCHES.map(s => s.color === "orange" ? "red" : s.color)
    assert.strictEqual(new Set(hues).size, hues.length)
    // Terminal-style themes name the palette color0 to color15.
    const ansi = M.themeColors("accent = '#7aa2f7'\ncolor1 = \"#f7768e\"\ncolor2 = \"#9ece6a\"\ncolor4 = \"#7aa2f7\"\ncolor9 = \"#ff899d\"\n")
    assert.strictEqual(M.paletteColor(ansi, "red"), "#f7768e")
    assert.strictEqual(M.paletteColor(ansi, "green"), "#9ece6a")
    assert.strictEqual(M.paletteColor(ansi, "orange"), "#ff899d")
    assert.strictEqual(M.paletteColor(ansi, "cyan"), "")
    assert.strictEqual(M.paletteColor(colors, "orange"), "#F38BA8ff")
  },
  "loudness note, switch summary, deep bass text"() {
    const at = v => ({ loudnessLevel: v })
    assert.strictEqual(M.loudnessNote(at(0)), "Now: no boost (listening level 0 dB).")
    assert.strictEqual(M.loudnessNote(at(-15)), "Now: 50% of the boost (listening level -15 dB).")
    assert.strictEqual(M.loudnessNote(at(-40)), "Now: the full boost (listening level -40 dB).")
    assert.match(M.loudnessNote({ loudnessLevel: null }), /Waiting/)
    assert.strictEqual(M.switchSummary({ loudness: true, louder: false, deep_bass: true, calibration: true }), "loudness · deep bass")
    assert.strictEqual(M.switchSummary({ calibration: false }), "calibration off")
    assert.strictEqual(M.switchSummary({}), "plain calibration")
    assert.match(M.deepBassDescription(80.2), /below 80 Hz.*up to 241 Hz/)
    assert.match(M.deepBassDescription(0), /no missing bass/)
  },
  "the graph shows what the switches do"() {
    const grid = [50, 300, 1000, 3000, 12000]
    // A correction that turns everything down 4 dB, preamp included.
    const fit = { grid, preamp_db: -1, channels: [{ measured: [0, 0, 0, 0, 0], predicted: [-3, -3, -3, -3, -3] }] }
    assert.strictEqual(M.correctionLevel(fit), -4)
    assert.deepStrictEqual(M.switchedCurve(fit, { calibration: true }, 0), [-3, -3, -3, -3, -3])
    // Make it louder gives the 4 dB back.
    assert.deepStrictEqual(M.switchedCurve(fit, { louder: true }, 0), [1, 1, 1, 1, 1])
    // Calibration off: the plain speakers at the calibration's level.
    assert.deepStrictEqual(M.switchedCurve(fit, { calibration: false }, 0), [-4, -4, -4, -4, -4])
    // Loudness lifts the bass against the mids.
    const loud = M.switchedCurve(fit, { loudness: true }, 1)
    assert.ok(loud[0] - loud[2] > 4, JSON.stringify(loud))
    assert.ok(M.switchesShape({ louder: true }) && !M.switchesShape({ deep_bass: true, calibration: true }))
    assert.strictEqual(M.loudnessShare({ loudnessLevel: -15 }, {}), 0.5)
    assert.strictEqual(M.loudnessShare(null, { loudness_level: "full" }), 1)
  },
  "fuller bass and wider deep bass"() {
    // The speaker gives up at 46 Hz and is 6 dB short at 80 Hz (fmin is 1.1 times that).
    const fit = { grid: [30, 60, 90, 1000], preamp_db: 0,
      channels: [{ knee: 46, fmin: 88, measured: [0, 0, 0, 0], predicted: [0, 0, 0, 0] }] }
    assert.strictEqual(M.deepBassCorner(fit), 80)
    assert.strictEqual(M.fullerBassFreq(fit), 92)
    assert.strictEqual(M.deepBassCorner({ channels: [{ knee: 0, fmin: 20 }] }), 0)
    const curve = M.switchedCurve(fit, { fuller_bass: true }, 0)
    // Bass lifted against the mids by about the shelf, mids trimmed.
    assert.ok(curve[0] - curve[3] > 3.5 && curve[3] < -3.9, JSON.stringify(curve))
    // Make it louder gives the trim back.
    assert.ok(Math.abs(M.switchedCurve(fit, { fuller_bass: true, louder: true }, 0)[3]) < 0.1)
  },
  "curve points span the box and clamp"() {
    const pts = M.curvePoints([20, 200, 2000, 20000], [0, 15, -15, 40], 300, 100, 15)
    assert.strictEqual(pts[0].x, 0)
    assert.ok(Math.abs(pts[3].x - 300) < 1e-9)
    assert.strictEqual(pts[0].y, 50)
    assert.strictEqual(pts[1].y, 0)
    assert.strictEqual(pts[2].y, 100)
    assert.strictEqual(pts[3].y, 0)
    assert.ok(Math.abs(M.frequencyX([20, 20000], 632.4555, 100) - 50) < 0.01)
    assert.deepStrictEqual(M.meanCurve([[0, 2], [2, 4]]), [1, 3])
  },
}
let failed = 0
for (const [name, fn] of Object.entries(tests)) {
  try { fn(); console.log("ok   " + name) } catch (e) { failed++; console.log("FAIL " + name + "\n     " + e.message) }
}
process.exit(failed ? 1 : 0)
