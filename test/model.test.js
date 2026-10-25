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
  dsp: { GetState: "Running", GetProcessingLoad: 0.4123, GetClippedSamples: 0, GetCaptureRate: 44100 },
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
  "hero status reads rate and load"() {
    assert.strictEqual(M.heroStatus(base, true), "On · 44.1 kHz · 0.4% load")
    assert.strictEqual(M.heroStatus({ ...base, dsp: null }, true), "On")
    assert.strictEqual(M.heroStatus({ ...base, pipewire: false }, true), "Needs the PipeWire backend")
    assert.strictEqual(M.rateLabel(48000), "48 kHz")
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
