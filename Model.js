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
  return "On"
}

function outputLabel(status) {
  if (!status) return ""
  return status.targetDescription || status.target || ""
}

function bypassReason(status) {
  if (!status || !status.defaultSink || status.defaultSink === SINK) return ""
  return "Audio goes straight to " + status.defaultSink + ". Turn on again to route it through CamillaDSP."
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
  var kind = { fir: "Detailed EQ", mixed: "Room Correction", "mixed-gentle": "Room Correction, gentle" }[fit.mode] || filters + " filters"
  return "Off target by " + db(before) + " before, " + db(after) + " planned (" + kind + ")" + timingSummary(fit.channels, "planned")
}

// Room Correction also brings the bass forward: how late it arrives, as the
// mean of the channels, before and after. Empty for the other corrections.
function timingSummary(channels, word) {
  var before = 0, after = 0, n = 0
  for (var i = 0; i < channels.length; i++) {
    var b = Number(channels[i].timing_before), a = Number(channels[i].timing_after)
    if (channels[i].timing_before === undefined || !isFinite(b) || !isFinite(a)) continue
    before += b
    after += a
    n++
  }
  if (n === 0) return ""
  return "; bass " + (before / n).toFixed(1) + " ms late before, " + (after / n).toFixed(1) + " ms " + word
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
    + timingSummary(check.channels, "with")
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

// The switches a calibrated preset can carry (lib/calibrate option). Each has
// its own hue among the six every theme defines (orange is not one of them:
// terminal-style themes have no slot for it, and it falls back to bright red).
var SWITCHES = [
  { key: "loudness", option: "loudness", label: "Loudness", color: "yellow",
    description: "Fuller sound at low volume, like the loudness button on a stereo. Follows the output's volume." },
  { key: "fuller_bass", option: "fuller-bass", label: "Fuller bass", color: "blue",
    description: "Lifts the bass 4 dB just above where the speakers give up, trimming the rest so it cannot clip." },
  { key: "louder", option: "louder", label: "Make it louder", color: "orange",
    description: "Gives back the volume the correction takes away. A limiter catches the peaks." },
  { key: "deep_bass", option: "deep-bass", label: "Deep bass", color: "magenta",
    description: "Suggests low notes the speakers cannot play, using their harmonics." },
  { key: "calibration", option: "calibration", label: "Calibration", color: "green",
    description: "Off plays the plain speakers, level matched, so only the tone changes." }
]

// How much of the Loudness boost is applied now, from the listening level the
// tracker last wrote: none at the reference level (-5 dB, LOUDNESS in
// calibration_dsp.py), all of it 20 dB below.
function loudnessNote(status) {
  var level = status ? Number(status.loudnessLevel) : NaN
  if (!status || status.loudnessLevel === null || status.loudnessLevel === undefined || !isFinite(level))
    return "Waiting for the listening level."
  var share = Math.max(0, Math.min(1, (-5 - level) / 20))
  var amount = share === 0 ? "no boost" : (share === 1 ? "the full boost" : Math.round(share * 100) + "% of the boost")
  return "Now: " + amount + " (listening level " + level.toFixed(0) + " dB)."
}

// The switches that are on, for the folded section.
function switchSummary(options) {
  var on = []
  for (var i = 0; i < SWITCHES.length; i++) {
    var s = SWITCHES[i]
    var value = options ? options[s.key] : undefined
    if (s.key === "calibration") {
      if (value === false) on.push("calibration off")
    } else if (value === true) {
      on.push(s.label.toLowerCase())
    }
  }
  return on.length ? on.join(" · ") : "plain calibration"
}

function deepBassDescription(corner) {
  if (!(corner > 0)) return "These speakers reach the bottom of the measurement; there is no missing bass to suggest."
  return "Suggests the bass below " + Math.round(corner) + " Hz, which these speakers play thinly or not at all, through harmonics up to "
    + Math.round(corner * 3) + " Hz."
}

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
    var m = /^\s*([a-z_][a-z0-9_]*)\s*=\s*["'](#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?)["']/.exec(lines[i])
    if (m) out[m[1]] = m[2]
  }
  return out
}

// Themes name their palette either by colour (red = ...) or the terminal way
// (color1 = ...), where 1 to 6 are red, green, yellow, blue, magenta and cyan.
// Orange has no terminal slot; bright red (color9) is the nearest.
var PALETTE_NAMES = {
  red: ["red", "color1"], green: ["green", "color2"], yellow: ["yellow", "color3"],
  blue: ["blue", "color4"], magenta: ["magenta", "color5"], cyan: ["cyan", "color6"],
  orange: ["orange", "bright_red", "color9", "red", "color1"]
}

function paletteColor(colors, name) {
  var keys = PALETTE_NAMES[name] || [name]
  for (var i = 0; i < keys.length; i++) {
    if (colors && colors[keys[i]]) return colors[keys[i]]
  }
  return ""
}

// ---------- What the switches do, for the graph ----------

// Magnitude in dB at hz of an RBJ shelf with slope 12 (Q 0.707), the shelves
// CamillaDSP's Loudness filter uses, at 48 kHz.
function shelfDb(kind, freq, gain, hz) {
  var fs = 48000
  var a = Math.pow(10, gain / 40)
  var w0 = 2 * Math.PI * freq / fs
  var cs = Math.cos(w0), sn = Math.sin(w0)
  var alpha = sn / 2 * Math.SQRT2
  var beta = 2 * Math.sqrt(a) * alpha
  var b, d
  if (kind === "low") {
    b = [a * ((a + 1) - (a - 1) * cs + beta), 2 * a * ((a - 1) - (a + 1) * cs), a * ((a + 1) - (a - 1) * cs - beta)]
    d = [(a + 1) + (a - 1) * cs + beta, -2 * ((a - 1) + (a + 1) * cs), (a + 1) + (a - 1) * cs - beta]
  } else {
    b = [a * ((a + 1) + (a - 1) * cs + beta), -2 * a * ((a - 1) + (a + 1) * cs), a * ((a + 1) + (a - 1) * cs - beta)]
    d = [(a + 1) - (a - 1) * cs + beta, 2 * ((a - 1) - (a + 1) * cs), (a + 1) - (a - 1) * cs - beta]
  }
  var w = 2 * Math.PI * hz / fs
  function mag(c) {
    var re = c[0] + c[1] * Math.cos(w) + c[2] * Math.cos(2 * w)
    var im = -c[1] * Math.sin(w) - c[2] * Math.sin(2 * w)
    return Math.sqrt(re * re + im * im)
  }
  return 20 * Math.log10(mag(b) / mag(d))
}

// LOUDNESS and LOUDNESS_LEVELS in calibration_dsp.py.
var LOUDNESS = { reference: -5, low: 6, high: 3 }
var LOUDNESS_LEVELS = { volume: 0, some: -12, full: -25 }

// How much of the Loudness boost applies, 0 to 1: from the listening level the
// tracker wrote when there is one, otherwise from the strength alone.
function loudnessShare(status, options) {
  var known = status && status.loudnessLevel !== null && status.loudnessLevel !== undefined
  var level = known ? Number(status.loudnessLevel)
    : LOUDNESS_LEVELS[(options && options.loudness_level) || "volume"] || 0
  return Math.max(0, Math.min(1, (LOUDNESS.reference - level) / 20))
}

// The Loudness filter's response at share of its boost: two shelves, and the
// mids turned down by the larger boost so it cannot clip.
function loudnessCurve(grid, share) {
  var low = LOUDNESS.low * share, high = LOUDNESS.high * share
  var out = []
  for (var i = 0; i < (grid || []).length; i++)
    out.push(shelfDb("low", 70, low, grid[i]) + shelfDb("high", 3500, high, grid[i]) - Math.max(low, high))
  return out
}

// What the correction does to the level, as correction_level() works it out:
// the mean of planned minus measured, preamp included, from 200 Hz to 5 kHz.
function correctionLevel(fit) {
  if (!fit || !fit.channels || !fit.grid) return 0
  var sum = 0, count = 0
  for (var c = 0; c < fit.channels.length; c++) {
    var ch = fit.channels[c]
    for (var i = 0; i < fit.grid.length; i++) {
      if (fit.grid[i] < 200 || fit.grid[i] > 5000) continue
      sum += ch.predicted[i] - ch.measured[i] + (Number(fit.preamp_db) || 0)
      count++
    }
  }
  return count ? sum / count : 0
}

// The planned response with the switches applied, averaged over the
// channels, on the fit's own grid. Deep bass is not here: it adds harmonics,
// which no response curve can show, and the graph marks its band instead.
function switchedCurve(fit, options, share) {
  if (!fit || !fit.channels || !fit.grid) return []
  var opts = options || {}
  var level = correctionLevel(fit)
  var loud = opts.loudness === true ? loudnessCurve(fit.grid, share) : null
  var fuller = opts.fuller_bass === true
  var shelfAt = fuller ? fullerBassFreq(fit) : 0
  var makeup = opts.louder === true ? Math.min(Math.max(-level, 0), 6) + (fuller ? FULLER_BASS_DB : 0) : 0
  var out = []
  for (var i = 0; i < fit.grid.length; i++) {
    var sum = 0
    for (var c = 0; c < fit.channels.length; c++) {
      var ch = fit.channels[c]
      // Calibration off: the plain speakers, at the calibration's level.
      sum += opts.calibration === false ? ch.measured[i] + level : ch.predicted[i]
    }
    var bass = fuller ? shelfDb("low", shelfAt, FULLER_BASS_DB, fit.grid[i]) - FULLER_BASS_DB : 0
    out.push(sum / fit.channels.length + (loud ? loud[i] : 0) + bass + makeup)
  }
  return out
}

// Whether the switches change the response at all (Deep bass aside).
function switchesShape(options) {
  return !!options && (options.loudness === true || options.louder === true || options.fuller_bass === true
    || options.calibration === false)
}

// Where Deep bass starts and where the Fuller bass shelf sits, the same as
// deep_bass_corner() and fuller_bass_freq() in calibration_dsp.py: from where
// the speakers are about 6 dB short (fit_floor, 1.1 times that point), and the
// bass limit.
function deepBassCorner(fit) {
  var channels = (fit && fit.channels) || []
  if (!channels.length) return 0
  var fmin = Infinity
  for (var i = 0; i < channels.length; i++) fmin = Math.min(fmin, Number(channels[i].fmin) || 20)
  var corner = Math.min(Math.max(fitKnee(fit), fmin / 1.1), 200)
  return corner > 30 ? Math.round(corner * 10) / 10 : 0
}

function fullerBassFreq(fit) {
  return Math.min(Math.max(2 * fitKnee(fit), deepBassCorner(fit), 60), 200)
}

var FULLER_BASS_DB = 4

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
    calibrationId: calibrationId, isCalibrated: isCalibrated, parse: parse, state: state, presetTitle: presetTitle,
    heroStatus: heroStatus, outputLabel: outputLabel, bypassReason: bypassReason,
    tooltip: tooltip, usableMics: usableMics, micInfo: micInfo, calibrationOptions: calibrationOptions, calibrationPath: calibrationPath,
    isMeasurementMic: isMeasurementMic, calibrationHint: calibrationHint, event: event, fitSummary: fitSummary,
    checkSummary: checkSummary, curvePoints: curvePoints, smoothCurve: smoothCurve, micSummary: micSummary,
    SWITCHES: SWITCHES, fitKnee: fitKnee, themeColors: themeColors, paletteColor: paletteColor, loudnessNote: loudnessNote,
    switchSummary: switchSummary, deepBassDescription: deepBassDescription,
    shelfDb: shelfDb, loudnessShare: loudnessShare, loudnessCurve: loudnessCurve, correctionLevel: correctionLevel,
    switchedCurve: switchedCurve, switchesShape: switchesShape, deepBassCorner: deepBassCorner,
    fullerBassFreq: fullerBassFreq, targetDb: targetDb, targetCurve: targetCurve, logGrid: logGrid, frequencyX: frequencyX, meanCurve: meanCurve
  }
}
