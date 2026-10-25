.pragma library

// Pure helpers for Panel.qml, kept apart so they can be tested with node.

var SINK = "camilladsp"
// Presets the calibrator builds, one per Sound and Correction combination
// (lib/calibrate preset_id); 80-calibrated is the single preset of 0.1.

function calibrationId(target, mode) {
  return "80-cal-" + mode + "-" + target
}

function isCalibrated(id) {
  return /^80-cal(-|ibrated$)/.test(String(id || ""))
}

function parse(text) {
  try {
    var value = JSON.parse(String(text || "").trim())
    return value && typeof value === "object" ? value : null
  } catch (e) {
    return null
  }
}

function rateLabel(hz) {
  var value = Number(hz)
  if (!isFinite(value) || value <= 0) return ""
  var khz = Math.round(value / 100) / 10
  return (khz % 1 === 0 ? khz.toFixed(0) : khz.toFixed(1)) + " kHz"
}

function loadLabel(percent) {
  var value = Number(percent)
  if (!isFinite(value)) return ""
  return (value < 10 ? value.toFixed(1) : value.toFixed(0)) + "% load"
}

function state(status, routedLive) {
  if (!status) return "loading"
  if (!status.installed || !status.pipewire || !status.setup) return "setup"
  if (status.service === "failed") return "failed"
  if (status.service !== "active") return "off"
  return routedLive ? "on" : "bypassed"
}

function presetTitle(status, id) {
  var presets = (status && status.presets) || []
  for (var i = 0; i < presets.length; i++) {
    if (presets[i].id === id) return presets[i].title
  }
  return id || ""
}

function heroStatus(status, routedLive) {
  switch (state(status, routedLive)) {
  case "loading": return ""
  case "setup":
    if (status.installed && !status.pipewire) return "Needs the PipeWire backend"
    return "Not set up"
  case "failed": return "Stopped with an error"
  case "off": return "Off"
  case "bypassed": return "Bypassed"
  }
  var parts = ["On"]
  var dsp = status.dsp || {}
  var rate = rateLabel(dsp.GetCaptureRate)
  if (rate) parts.push(rate)
  var load = loadLabel(dsp.GetProcessingLoad)
  if (load) parts.push(load)
  return parts.join(" · ")
}

function outputLabel(status) {
  if (!status) return ""
  return status.targetDescription || status.target || ""
}

function bypassReason(status) {
  if (!status || !status.defaultSink || status.defaultSink === SINK) return ""
  return "Audio goes straight to " + status.defaultSink + ". Turn on again to route it through CamillaDSP."
}

function clipped(status) {
  var dsp = status && status.dsp
  var value = dsp ? Number(dsp.GetClippedSamples) : 0
  return isFinite(value) ? value : 0
}

function tooltip(status, routedLive) {
  var s = state(status, routedLive)
  if (s === "on") return "CamillaDSP: " + presetTitle(status, status.preset)
  if (s === "setup") return "CamillaDSP: not set up"
  return "CamillaDSP: " + heroStatus(status, routedLive).toLowerCase()
}

// ---------- Calibration ----------

function usableMics(devices) {
  var mics = (devices && devices.mics) || []
  var out = []
  for (var i = 0; i < mics.length; i++) {
    var m = mics[i]
    var label = m.description || m.name
    if (!m.usable) label += " (" + m.reason + ")"
    else if (m.muted) label += " (muted)"
    else if (m.builtin) label += " (built in)"
    out.push({ value: m.name, label: label, usable: m.usable && !m.muted })
  }
  return out
}

function micInfo(devices, name) {
  var mics = (devices && devices.mics) || []
  for (var i = 0; i < mics.length; i++) {
    if (mics[i].name === name) return mics[i]
  }
  return null
}

// "None" first, then every calibration file found in Downloads or the
// plugin's mic-calibration folder. Options are keyed by file name, not path:
// picking a file in Downloads copies it into the plugin's folder, and the
// dropdown keeps the value that was picked, which must still match.
function calibrationOptions(devices) {
  var files = (devices && devices.calibration_files) || []
  var out = [{ value: "", label: "None" }]
  for (var i = 0; i < files.length; i++) out.push({ value: files[i].file, label: files[i].label })
  return out
}

function calibrationPath(devices, file) {
  var files = (devices && devices.calibration_files) || []
  for (var i = 0; i < files.length; i++) {
    if (files[i].file === file) return files[i].path
  }
  return ""
}

function isMeasurementMic(mic) {
  return !!mic && /umik|ecm8000|emm-6|mm-1|xlr-?usb|dayton|minidsp/i.test(mic.name + " " + (mic.description || ""))
}

function calibrationHint(mic, options) {
  if (!mic) return ""
  if (mic.calibration) {
    // miniDSP ships two files: 0 degrees for a microphone aimed at the
    // speakers, 90 degrees for one pointed at the ceiling.
    if (/90/.test(mic.calibration.file)) return "Using the 90° file: point the microphone at the ceiling."
    if (mic.calibration.serial) return "Using the 0° file: point the microphone at the speakers."
    return ""
  }
  if (isMeasurementMic(mic) && options.length <= 1) {
    return "Download the calibration file for this microphone's serial number (miniDSP has them on its site) into Downloads, and pick it here."
  }
  return ""
}

// One line of the calibrator's output: progress, result, or error.
function event(line) {
  var value = parse(line)
  return value && value.event ? value : null
}

function db(value) {
  var n = Number(value)
  return isFinite(n) ? n.toFixed(1) + " dB" : "—"
}

function fitSummary(fit) {
  if (!fit || !fit.channels || fit.channels.length === 0) return ""
  var before = 0, after = 0
  for (var i = 0; i < fit.channels.length; i++) {
    before += Number(fit.channels[i].error_before) || 0
    after += Number(fit.channels[i].error_after) || 0
  }
  before /= fit.channels.length
  after /= fit.channels.length
  var filters = 0
  for (var j = 0; j < fit.channels.length; j++) filters += (fit.channels[j].filters || []).length
  var kind = fit.mode === "fir" ? "FIR" : filters + " filters"
  return "Off target by " + db(before) + " before, " + db(after) + " planned (" + kind + ")"
}

function checkSummary(check) {
  if (!check || !check.channels || check.channels.length === 0) return ""
  var before = 0, after = 0, deviation = 0
  for (var i = 0; i < check.channels.length; i++) {
    before += Number(check.channels[i].error_before) || 0
    after += Number(check.channels[i].error_after) || 0
    deviation += Number(check.channels[i].plan_deviation) || 0
  }
  before /= check.channels.length
  after /= check.channels.length
  deviation /= check.channels.length
  // The deviation compares what the correction did at the checked spot with
  // what was planned, so it does not depend on where the microphone stands.
  var verdict = deviation < 1 ? "the correction matches the plan"
    : (deviation < 2 ? "the correction is close to the plan" : "the correction is off the plan, measure again")
  return "Checked here: " + db(before) + " off target without, " + db(after) + " with; " + verdict
}

// ---------- Response graph ----------

// Map a curve on a log-frequency grid into a width x height box, with dbRange
// dB above and below the middle. Returns [{x, y}].
function curvePoints(grid, values, width, height, dbRange) {
  var out = []
  if (!grid || !values || grid.length < 2 || width <= 0 || height <= 0) return out
  var lo = Math.log(grid[0])
  var span = Math.log(grid[grid.length - 1]) - lo
  var range = dbRange || 15
  var n = Math.min(grid.length, values.length)
  for (var i = 0; i < n; i++) {
    var v = Math.max(-range, Math.min(range, Number(values[i]) || 0))
    out.push({
      x: (Math.log(grid[i]) - lo) / span * width,
      y: height / 2 - v / range * height / 2
    })
  }
  return out
}

function frequencyX(grid, hz, width) {
  if (!grid || grid.length < 2) return 0
  var lo = Math.log(grid[0])
  return (Math.log(hz) - lo) / (Math.log(grid[grid.length - 1]) - lo) * width
}

// The calibrator's target curves, the same as target_curve() in
// lib/calibration_dsp.py, so the panel can draw a choice the moment it is made.
function targetDb(name, hz) {
  var octaves = Math.log(hz / 1000) / Math.LN2
  if (name === "warm") return Math.max(-4, Math.min(3, -0.8 * octaves))
  if (name === "room") return 4 / (1 + Math.pow(hz / 100, 2)) + (octaves > 0 ? -0.6 * octaves : 0)
  return 0
}

function targetCurve(name, grid) {
  var out = []
  for (var i = 0; i < (grid || []).length; i++) out.push(targetDb(name, grid[i]))
  return out
}

// Log-spaced frequencies from 20 Hz to 20 kHz.
function logGrid(count) {
  var out = []
  for (var i = 0; i < count; i++) out.push(20 * Math.pow(1000, i / (count - 1)))
  return out
}

// Gentle smoothing for drawing: a triangular average over radius points
// either side. The panel's curves have 12 points per octave, so radius 2 is
// about a third of an octave: it softens spikes without hiding a resonance.
function smoothCurve(values, radius) {
  var r = radius === undefined ? 2 : radius
  var out = []
  for (var i = 0; i < (values || []).length; i++) {
    var sum = 0, weight = 0
    for (var k = -r; k <= r; k++) {
      var j = i + k
      if (j < 0 || j >= values.length) continue
      var w = r + 1 - Math.abs(k)
      sum += (Number(values[j]) || 0) * w
      weight += w
    }
    out.push(sum / weight)
  }
  return out
}

// One line for the microphone segment when it is folded away.
function micSummary(mic, position) {
  if (!mic) return ""
  var parts = [String(mic.description || mic.name).replace(/\s+/g, " ")]
  if (mic.calibration) parts.push(/90/.test(mic.calibration.file) ? "90° file" : "calibrated")
  parts.push(position === "listening" ? "where I sit" : "near the speakers")
  return parts.join(" · ")
}

// The switches a calibrated preset can carry (lib/calibrate option).
var SWITCHES = [
  { key: "loudness", option: "loudness", label: "Loudness", color: "yellow",
    description: "Fuller sound at low volume, like the loudness button on a stereo. Follows the output's volume." },
  { key: "louder", option: "louder", label: "Make it louder", color: "orange",
    description: "Gives back the volume the correction takes away. A limiter catches the peaks." },
  { key: "deep_bass", option: "deep-bass", label: "Deep bass", color: "magenta",
    description: "Suggests low notes the speakers cannot play, using their harmonics." },
  { key: "calibration", option: "calibration", label: "Calibration", color: "green",
    description: "Off plays the plain speakers, level matched, so only the tone changes." }
]

// The lowest bass limit any channel of a fit found; 0 when the speakers
// reach the bottom of the measurement, where Deep bass has nothing to add.
function fitKnee(fit) {
  var knee = 0
  var channels = (fit && fit.channels) || []
  for (var i = 0; i < channels.length; i++) {
    var k = Number(channels[i].knee) || 0
    if (k > 30 && (knee === 0 || k < knee)) knee = k
  }
  return knee
}

// The theme's palette from its colors.toml ("red = \"#f38ba8\"" lines), so
// the panel's colours follow the theme the way the rest of the shell does.
function themeColors(text) {
  var out = {}
  var lines = String(text || "").split("\n")
  for (var i = 0; i < lines.length; i++) {
    var m = /^\s*([a-z_]+)\s*=\s*"(#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?)"/.exec(lines[i])
    if (m) out[m[1]] = m[2]
  }
  return out
}

// Average the channels into one curve for the graph.
function meanCurve(curves) {
  if (!curves || curves.length === 0) return []
  var out = []
  for (var i = 0; i < curves[0].length; i++) {
    var sum = 0
    for (var c = 0; c < curves.length; c++) sum += Number(curves[c][i]) || 0
    out.push(sum / curves.length)
  }
  return out
}

if (typeof module !== "undefined") {
  module.exports = {
    calibrationId: calibrationId, isCalibrated: isCalibrated, parse: parse, rateLabel: rateLabel, loadLabel: loadLabel, state: state, presetTitle: presetTitle,
    heroStatus: heroStatus, outputLabel: outputLabel, bypassReason: bypassReason, clipped: clipped,
    tooltip: tooltip, usableMics: usableMics, micInfo: micInfo, calibrationOptions: calibrationOptions, calibrationPath: calibrationPath,
    isMeasurementMic: isMeasurementMic, calibrationHint: calibrationHint, event: event, fitSummary: fitSummary,
    checkSummary: checkSummary, curvePoints: curvePoints, smoothCurve: smoothCurve, micSummary: micSummary,
    SWITCHES: SWITCHES, fitKnee: fitKnee, themeColors: themeColors, targetDb: targetDb, targetCurve: targetCurve, logGrid: logGrid, frequencyX: frequencyX, meanCurve: meanCurve
  }
}
