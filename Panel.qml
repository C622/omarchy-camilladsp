import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.Pipewire
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// CamillaDSP in the bar: on/off, presets, and a measure-and-correct
// calibration flow. All real work happens in bin/camilladsp-omarchy; this file
// only runs it and shows what it reports.
Panel {
  id: root
  moduleName: "community.camilladsp"
  ipcTarget: "community.camilladsp"
  // The panel owns the single IpcHandler its target permits, so it can add
  // refresh and the power/preset methods next to open/close/toggle.
  manageIpc: false

  readonly property string ctl: decodeURIComponent(Qt.resolvedUrl("bin/camilladsp-omarchy").toString().replace(/^file:\/\//, ""))

  property var status: null
  property string actionError: ""
  property int presetIndex: 0
  property bool cursorActive: false

  // Calibration state.
  property var devices: null
  property var session: null
  property bool session0Loaded: false
  property string mic: ""
  property string position: "nearfield"
  property string target: "flat"
  property string mode: "peq"
  property bool calibrating: false
  property string calStage: ""
  property string calMessage: ""
  property real calFraction: 0
  property string calError: ""
  property string calCrash: ""
  readonly property string calCrashMessage: "The calibrator stopped unexpectedly"
  property var calWarnings: []

  // What the current Sound and Correction choices would do with the
  // measurements, worked out without saving anything (calibrate fit --preview).
  // The theme's full palette, read from the same colors.toml as the shell,
  // so the graph and switches follow a theme switch. A colour a theme does
  // not define falls back to its accent or foreground.
  property var themePalette: ({})
  // Used when the theme file cannot be read or lacks a colour. Not the
  // accent: in some themes that is the text colour, and nothing would stand
  // out from anything else.
  readonly property var fallbackColors: ({
    orange: "#f5a97f", blue: "#8aadf4", green: "#a6da95", yellow: "#eed49f",
    cyan: "#8bd5ca", magenta: "#c6a0f6", red: "#ed8796"
  })
  function themeColor(name) {
    return Model.paletteColor(themePalette, name) || fallbackColors[name] || Commons.Color.accent
  }
  readonly property color colorMeasured: themeColor("orange")
  readonly property color colorPlanned: themeColor("blue")
  readonly property color colorChecked: themeColor("green")
  readonly property color colorTarget: themeColor("yellow")
  readonly property color colorOn: themeColor("green")
  readonly property color colorSwitched: themeColor("cyan")
  readonly property color colorDeepBass: themeColor("magenta")
  readonly property var targetColors: ({
    flat: themeColor("cyan"), warm: themeColor("orange"), room: themeColor("magenta")
  })

  FileView {
    id: themeFile
    path: Quickshell.env("HOME") + "/.local/state/omarchy/current/theme/colors.toml"
    watchChanges: true
    printErrors: false
    onLoaded: {
      root.themePalette = Model.themeColors(text())
      console.info("community.camilladsp: theme colours from " + path + ": " + Object.keys(root.themePalette).join(" "))
    }
    onFileChanged: reload()
  }

  // A theme switch can replace the whole theme folder, which a file watch does
  // not always see; the shell's own colours changing says it happened.
  Connections {
    target: Commons.Color
    function onAccentChanged() { themeFile.reload() }
  }

  // Folded sections, remembered in this widget's shell.json entry.
  property bool presetsCollapsed: setting("presetsCollapsed", false) === true
  property bool micCollapsed: setting("micCollapsed", false) === true
  property bool soundCollapsed: setting("soundCollapsed", false) === true

  function setCollapsed(key, value) {
    if (key === "presetsCollapsed") presetsCollapsed = value
    else if (key === "soundCollapsed") soundCollapsed = value
    else micCollapsed = value
    var next = Object.assign({}, settings)
    next[key] = value
    if (bar && bar.shell && typeof bar.shell.updateEntryInline === "function")
      bar.shell.updateEntryInline(moduleName, next)
  }

  // The switches act on calibrated presets built from a saved fit.
  readonly property var activeFit: status && session && session.fits ? (session.fits[status.preset] || null) : null
  readonly property var options: session && session.options ? session.options : ({})

  function setOption(option, on) {
    run(["calibrate", "option", option, on ? "on" : "off"])
  }

  property var preview: null
  property string previewKey: ""
  property string previewRunningKey: ""
  property string previewFailedKey: ""
  property bool previewCancelled: false
  readonly property string selectionKey: target + "/" + mode + "/" + measurementCount
  // The built calibration already is the current choice: show it and its check.
  readonly property bool fitMatches: !!fit && fit.target === target && fit.mode === mode
    && fit.positions === measurementCount
  readonly property bool previewReady: !fitMatches && !!preview && previewKey === selectionKey
  readonly property bool previewing: !fitMatches && measurementCount > 0 && !previewReady

  // PipeWire tells the shell when the default output changes, so the icon
  // follows an output switched elsewhere without polling.
  readonly property var defaultSink: Pipewire.defaultAudioSink
  readonly property bool routedLive: !!(defaultSink && defaultSink.name === Model.SINK) && !!status && status.service === "active"
  readonly property string dspState: Model.state(status, routedLive)
  readonly property bool hideWhenOff: setting("hideWhenOff", false) === true
  readonly property var presets: (status && status.presets) || []
  // Every Sound and Correction combination builds its own preset; these are
  // the fit and check of the one the current choices name.
  readonly property string choiceId: Model.calibrationId(target, mode)
  readonly property var fit: session && session.fits ? (session.fits[choiceId] || null) : null
  readonly property var check: session && session.checks ? (session.checks[choiceId] || null) : null
  readonly property int measurementCount: session ? (session.measurements || 0) : 0
  readonly property var micOptions: Model.usableMics(devices)
  readonly property var micInfo: Model.micInfo(devices, mic)
  readonly property var calibrationOptions: Model.calibrationOptions(devices)
  readonly property string micCalibration: micInfo && micInfo.calibration ? micInfo.calibration.file : ""
  readonly property string output: devices ? (devices.output || "") : ""
  readonly property bool busy: actionProc.running || calibrating

  function run(args) {
    if (actionProc.running) return
    actionError = ""
    actionProc.command = [ctl].concat(args)
    actionProc.running = true
  }

  function refresh() {
    if (!statusProc.running) statusProc.running = true
  }

  function refreshCalibration() {
    if (!devicesProc.running) devicesProc.running = true
    if (!summaryProc.running) summaryProc.running = true
  }

  // Every bar surface has its own instance; the one that owns the IPC target
  // passes a refresh on to the others.
  function refreshAll() {
    var items = bar && typeof bar.moduleWidgets === "function" ? bar.moduleWidgets(moduleName) : [root]
    for (var i = 0; i < items.length; i++) {
      if (items[i] && typeof items[i].refresh === "function") items[i].refresh()
    }
    if (items.indexOf(root) < 0) refresh()
  }

  function togglePower() {
    if (dspState === "setup") setup()
    else run([dspState === "on" ? "off" : "on"])
  }

  function applyPreset(id) {
    if (!id) return
    // Picking a calibration also picks its settings, so the graph and the
    // summary describe the calibration that is playing.
    var built = session && session.fits ? session.fits[id] : null
    if (built) {
      target = built.target
      mode = built.mode
    }
    run(["preset", id])
  }

  function stepPreset(delta) {
    if (dspState !== "on" && dspState !== "bypassed" && dspState !== "off") return
    run(["preset", delta > 0 ? "next" : "prev", "--notify"])
  }

  function setup() {
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", Util.shellQuote(ctl) + " setup"])
    close()
  }

  function editPresets() {
    if (status && status.presetsDir) Quickshell.execDetached(["xdg-open", status.presetsDir])
  }

  function startCalibration(command, extra) {
    if (calibrating || actionProc.running) return
    calError = ""
    calWarnings = []
    calFraction = 0
    calMessage = command === "check" ? "Checking the calibration" : "Starting"
    calStage = command
    calProc.command = [ctl, "calibrate", command].concat(extra || [])
    calibrating = true
    calProc.running = true
  }

  function measure() {
    if (!output || !mic) return
    startCalibration("measure", ["--output", output, "--mic", mic, "--position", position])
  }

  function setMicCalibration(file) {
    if (!mic || file === micCalibration) return
    var path = Model.calibrationPath(devices, file)
    run(path ? ["calibrate", "mic-cal", "--mic", mic, "--file", path] : ["calibrate", "mic-cal", "--mic", mic, "--clear"])
  }

  function build() {
    startCalibration("fit", ["--target", target, "--mode", mode])
  }

  function runPreview() {
    if (fitMatches || measurementCount === 0 || calibrating) return
    if (previewKey === selectionKey && preview) return
    // A choice that failed once fails again; it waits for a new measurement or choice.
    if (previewFailedKey === selectionKey) return
    // A newer choice replaces one still being worked out; onExited starts it.
    if (previewProc.running) {
      previewCancelled = true
      previewProc.running = false
      return
    }
    previewRunningKey = selectionKey
    previewProc.command = [ctl, "calibrate", "fit", "--target", target, "--mode", mode, "--preview"]
    previewProc.running = true
  }

  function handleEvent(line) {
    var e = Model.event(line)
    if (!e) return
    if (e.event === "progress") {
      calMessage = e.message || ""
      calFraction = Number(e.fraction) || 0
    } else if (e.event === "error") {
      calError = e.message || "The calibration failed."
    } else if (e.event === "result") {
      calWarnings = e.warnings || []
    }
  }

  function calibrationFinished(exitCode) {
    var stage = calStage
    if (exitCode !== 0 && calError === "")
      calError = calCrash ? calCrashMessage + ": " + calCrash : calCrashMessage
    calCrash = ""
    calibrating = false
    calMessage = ""
    calFraction = 0
    refreshCalibration()
    // A fresh fit is only worth hearing: apply it at once. The previous preset
    // is a click away in the list for comparison.
    if (exitCode === 0 && stage === "fit") run(["calibrate", "apply"])
    else refresh()
  }

  // A path through the points with rounded joins: each corner becomes a
  // quadratic curve through the midpoints on either side of it.
  function tracePath(ctx, pts) {
    ctx.beginPath()
    ctx.moveTo(pts[0].x, pts[0].y)
    for (var i = 1; i < pts.length - 1; i++)
      ctx.quadraticCurveTo(pts[i].x, pts[i].y, (pts[i].x + pts[i + 1].x) / 2, (pts[i].y + pts[i + 1].y) / 2)
    ctx.lineTo(pts[pts.length - 1].x, pts[pts.length - 1].y)
  }

  function activateCursor() {
    if (presetIndex >= 0 && presetIndex < presets.length) applyPreset(presets[presetIndex].id)
  }

  // Quickshell's own IpcHandler rather than Omarchy's ShellIpc, which older
  // Omarchy releases do not have. omarchy-shell falls back to qs ipc for it.
  IpcHandler {
    target: "community.camilladsp"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { root.refreshAll() }
    function togglePower(): void { root.togglePower() }
    function nextPreset(): void { root.stepPreset(1) }
    function prevPreset(): void { root.stepPreset(-1) }
  }

  onOpenedChanged: {
    if (!opened) return
    refresh()
    refreshCalibration()
    cursorActive = false
    for (var i = 0; i < presets.length; i++) {
      if (presets[i].id === status.preset) presetIndex = i
    }
  }

  onRoutedLiveChanged: refresh()
  Component.onCompleted: refresh()

  // Sized from its own condition rather than from `visible`, which also goes
  // false while any parent is hidden and would keep the slot at zero width.
  readonly property bool shown: !(hideWhenOff && dspState !== "on")
  visible: shown
  implicitWidth: shown ? button.implicitWidth : 0
  implicitHeight: shown ? button.implicitHeight : 0

  Process {
    id: statusProc
    command: [root.ctl, "status", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var next = Model.parse(text)
        if (next) root.status = next
      }
    }
  }

  Process {
    id: actionProc
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var message = String(text || "").trim().split("\n").pop()
        if (message !== "") root.actionError = message.replace(/^camilladsp-omarchy: /, "")
      }
    }
    onExited: function(exitCode) {
      if (exitCode === 0) root.actionError = ""
      root.refresh()
      root.refreshCalibration()
    }
  }

  Process {
    id: devicesProc
    command: [root.ctl, "calibrate", "devices"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var next = Model.parse(text)
        if (!next) return
        root.devices = next
        var keep = false
        for (var i = 0; i < (next.mics || []).length; i++) {
          if (next.mics[i].name === root.mic && next.mics[i].usable) keep = true
        }
        if (!keep) root.mic = next.mic || ""
      }
    }
  }

  Process {
    id: summaryProc
    command: [root.ctl, "calibrate", "summary"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var next = Model.parse(text)
        if (!next) return
        root.session = next
        // Start from the calibration built last, but only when the panel first
        // learns of it: later refreshes must not undo a choice being made.
        if (next.fit && !root.session0Loaded) {
          root.target = next.fit.target || root.target
          root.mode = next.fit.mode || root.mode
        }
        root.session0Loaded = true
        if (next.positions && next.positions.length > 0) root.position = next.positions[next.positions.length - 1]
      }
    }
  }

  Process {
    id: calProc
    stdout: SplitParser { onRead: function(line) { root.handleEvent(line) } }
    // A crash prints a traceback rather than an error event; keep its last line.
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var last = String(text || "").trim().split("\n").pop()
        // Exit and end of stream come in either order: fill in the message
        // when the exit got there first.
        if (!root.calibrating && root.calError === root.calCrashMessage) root.calError = root.calCrashMessage + ": " + last
        else root.calCrash = last
      }
    }
    onExited: function(exitCode) { root.calibrationFinished(exitCode) }
  }

  Process {
    id: previewProc
    stdout: SplitParser {
      onRead: function(line) {
        var e = Model.event(line)
        if (e && e.event === "result" && e.preview) {
          root.preview = e
          root.previewKey = root.previewRunningKey
        }
      }
    }
    onExited: function(exitCode) {
      if (exitCode !== 0 && !root.previewCancelled) root.previewFailedKey = root.previewRunningKey
      root.previewCancelled = false
      previewTimer.restart()
    }
  }

  // Wait for the clicking to settle before fitting: a PEQ fit takes seconds.
  Timer {
    id: previewTimer
    interval: 400
    onTriggered: root.runPreview()
  }

  onSelectionKeyChanged: previewTimer.restart()
  onFitMatchesChanged: previewTimer.restart()

  // Load and clipping only change while audio plays, and only matter while
  // someone is looking: read them while the panel is open.
  Timer {
    interval: 2000
    repeat: true
    running: root.opened && root.dspState === "on" && !root.busy
    onTriggered: root.refresh()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "\uf1de"
    dimmed: root.dspState !== "on"
    tooltipText: root.opened ? "" : Model.tooltip(root.status, root.routedLive)
    onPressed: function(b) {
      if (b === Qt.RightButton) root.togglePower()
      else if (b === Qt.MiddleButton) root.refresh()
      else root.toggle()
    }
    onWheelMoved: function(delta) { root.stepPreset(delta > 0 ? -1 : 1) }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    // The card goes in as children rather than through `content`, which
    // Omarchy 4.0.x does not have; newer releases still accept children.
    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (root.presets.length === 0) return
        if (!root.cursorActive) { root.cursorActive = true; return }
        var delta = dy !== 0 ? dy : dx
        root.presetIndex = (root.presetIndex + delta + root.presets.length) % root.presets.length
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero: icon · title/status · power ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, powerButton.implicitHeight)

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            text: "\uf1de"
            color: root.dspState === "on" ? root.colorOn : root.bar.foreground
            opacity: root.dspState === "on" ? 1 : 0.45
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: powerButton.left
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              text: root.dspState === "on" ? Model.presetTitle(root.status, root.status.preset) : "CamillaDSP"
              textFormat: Text.PlainText
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              text: Model.heroStatus(root.status, root.routedLive).toUpperCase()
              textFormat: Text.PlainText
              color: root.dspState === "on" ? root.colorOn
                : (root.dspState === "failed" ? Commons.Color.urgent
                  : (root.dspState === "bypassed" ? root.colorMeasured : Qt.darker(root.bar.foreground, 1.4)))
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }

          Button {
            id: powerButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            visible: root.dspState !== "loading"
            text: root.dspState === "setup" ? "Set up" : (root.dspState === "on" ? "Turn off" : "Turn on")
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            bordered: true
            active: root.dspState === "on"
            enabled: !root.busy
            opacity: enabled ? 1 : 0.4
            onClicked: root.togglePower()
          }
        }

        Message {
          visible: root.dspState === "setup"
          text: root.status && root.status.installed && !root.status.pipewire
            ? "This CamillaDSP was built without the PipeWire backend. Set up builds one that has it into ~/.local/bin."
            : "Set up installs CamillaDSP and a virtual CamillaDSP output. It opens a terminal, since installing packages may ask for your password."
        }

        Message {
          visible: root.dspState === "bypassed" && text !== ""
          text: Model.bypassReason(root.status)
        }

        Message {
          visible: root.dspState === "failed"
          text: "CamillaDSP stopped. See: journalctl --user -u omarchy-camilladsp"
          urgent: true
        }

        Message {
          visible: root.actionError !== ""
          text: root.actionError
          urgent: true
        }

        Column {
          width: parent.width
          spacing: Style.spacing.labelGap
          visible: root.dspState !== "setup" && root.dspState !== "loading"

          InfoPair { label: "Output"; value: Model.outputLabel(root.status) || "—" }

          Row {
            width: parent.width
            visible: Model.clipped(root.status) > 0
            spacing: Style.space(8)

            Text {
              width: parent.width - resetClip.width - parent.spacing
              anchors.verticalCenter: parent.verticalCenter
              text: Model.clipped(root.status) + " samples clipped. Lower the preset's preamp gain."
              textFormat: Text.PlainText
              wrapMode: Text.WordWrap
              color: Commons.Color.urgent
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Button {
              id: resetClip
              text: "Reset"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              fontSize: Style.font.caption
              bordered: true
              onClicked: root.run(["reset-clipping"])
            }
          }
        }

        // ---------- Presets ----------
        PanelSeparator { foreground: root.bar.foreground; visible: root.presets.length > 0 }

        Column {
          width: parent.width
          spacing: Style.space(6)
          visible: root.presets.length > 0

          Item {
            width: parent.width
            implicitHeight: Math.max(presetHeader.implicitHeight, editButton.implicitHeight)

            FoldHeader {
              id: presetHeader
              anchors.left: parent.left
              anchors.right: editButton.visible ? editButton.left : parent.right
              anchors.rightMargin: editButton.visible ? Style.space(8) : 0
              anchors.verticalCenter: parent.verticalCenter
              text: "Presets"
              collapsed: root.presetsCollapsed
              summary: root.status ? Model.presetTitle(root.status, root.status.preset) : ""
              onToggled: root.setCollapsed("presetsCollapsed", !root.presetsCollapsed)
            }

            Button {
              id: editButton
              visible: !root.presetsCollapsed
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: "Edit"
                iconText: "\uf044"
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                fontSize: Style.font.caption
                onClicked: root.editPresets()
              }
          }

          Column {
            width: parent.width
            spacing: Style.space(6)
            visible: !root.presetsCollapsed

            Repeater {
              model: root.presets

              Button {
                id: presetButton
                required property var modelData
                required property int index
                width: parent.width
                leftAlign: true
                // Long names (a calibration is named after the output) end in "…"
                // instead of running past the panel: Button only elides on newer
                // Omarchy releases, so the text is elided here.
                text: presetName.elidedText
                tooltipText: presetName.elidedText !== modelData.title ? modelData.title : ""
                iconText: Model.isCalibrated(modelData.id) ? "\uf130" : ""
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                fontSize: Style.font.bodySmall
                active: !!root.status && root.status.preset === modelData.id
                hasCursor: root.cursorActive && root.presetIndex === index
                enabled: !root.busy
                onClicked: root.applyPreset(modelData.id)
                onHovered: function(h) {
                  if (h) {
                    root.cursorActive = true
                    root.presetIndex = index
                  }
                }

                TextMetrics {
                  id: presetName
                  text: presetButton.modelData.title
                  font.family: presetButton.fontFamily
                  font.pixelSize: presetButton.fontSize
                  elide: Text.ElideRight
                  elideWidth: Math.max(0, presetButton.width - presetButton.horizontalPadding * 2
                    - (presetButton.iconText !== "" ? presetButton.iconSize + Style.space(10) : 0) - Style.space(6))
                }
              }
          }
          }
        }

        // ---------- Calibrated sound: the four switches ----------
        PanelSeparator { foreground: root.bar.foreground; visible: !!root.activeFit }

        Column {
          width: parent.width
          spacing: Style.space(8)
          visible: !!root.activeFit

          FoldHeader {
            width: parent.width
            text: "Calibrated sound"
            collapsed: root.soundCollapsed
            summary: Model.switchSummary(root.options)
            onToggled: root.setCollapsed("soundCollapsed", !root.soundCollapsed)
          }

          Repeater {
            model: root.soundCollapsed ? [] : Model.SWITCHES

            SwitchRow {
              required property var modelData
              label: modelData.label
              tint: root.themeColor(modelData.color)
              description: modelData.key === "deep_bass" ? Model.deepBassDescription(Model.fitKnee(root.activeFit))
                : modelData.description
              checked: root.options[modelData.key] === true
              available: modelData.key !== "deep_bass" || Model.fitKnee(root.activeFit) > 0
              onSwitched: function(on) { root.setOption(modelData.option, on) }
            }
          }

          // Loudness follows the output's volume. When the volume is turned on
          // an amplifier instead, the output sits at full and there is nothing
          // to follow: the strength says how loud to assume the music plays.
          Column {
            width: parent.width
            spacing: Style.space(6)
            visible: !root.soundCollapsed && root.options.loudness === true

            Choice {
              label: "Loudness strength"
              options: [
                { value: "volume", label: "Follow volume" },
                { value: "some", label: "Some" },
                { value: "full", label: "Full" }
              ]
              value: root.options.loudness_level || "volume"
              onPicked: function(v) { root.run(["calibrate", "option", "loudness-level", v]) }
            }

            Message {
              text: Model.loudnessNote(root.status)
                + ((root.options.loudness_level || "volume") === "volume"
                  ? " Turn the volume down to hear it, or use Some or Full if the volume is set on an amplifier." : "")
            }
          }
        }

        // ---------- Calibration ----------
        PanelSeparator { foreground: root.bar.foreground; visible: root.dspState !== "setup" && root.dspState !== "loading" }

        Column {
          width: parent.width
          spacing: Style.space(10)
          visible: root.dspState !== "setup" && root.dspState !== "loading"

          FoldHeader {
            width: parent.width
            text: "Calibrate with a microphone"
            collapsed: root.micCollapsed
            summary: Model.micSummary(root.micInfo, root.position)
            onToggled: root.setCollapsed("micCollapsed", !root.micCollapsed)
          }

          // The microphone segment, down to where the sound is chosen.
          Column {
            width: parent.width
            spacing: Style.space(10)
            visible: !root.micCollapsed

            Dropdown {
              width: parent.width
              label: "Microphone"
              fontFamily: root.bar.fontFamily
              options: root.micOptions
              value: root.mic
              enabled: !root.busy && root.measurementCount === 0
              onChanged: function(v) { root.mic = v }
          }

          Dropdown {
            width: parent.width
            label: "Microphone calibration"
            fontFamily: root.bar.fontFamily
            options: root.calibrationOptions
            value: root.micCalibration
            visible: root.mic !== "" && (root.calibrationOptions.length > 1 || root.micCalibration !== "")
            enabled: !root.busy
            onChanged: function(v) { root.setMicCalibration(v) }
          }

          Message {
            text: Model.calibrationHint(root.micInfo, root.calibrationOptions)
          }

          Choice {
            label: "Microphone is"
            options: [
              { value: "nearfield", label: "Near the speakers" },
              { value: "listening", label: "Where I sit" }
            ]
            value: root.position
            onPicked: function(v) { root.position = v }
          }

          Message {
            text: root.position === "listening"
              ? "Put the microphone where your head usually is. Measure from a few spots around it and the correction averages them, which also tames the room. Both speakers are time and level aligned to that spot."
              : "Measures the speakers themselves: the room barely counts. The built-in microphone works for this."
          }
          }

          Choice {
            label: "Sound"
            options: [
              { value: "flat", label: "Neutral" },
              { value: "warm", label: "Warm" },
              { value: "room", label: "Room" }
            ]
            value: root.target
            onPicked: function(v) { root.target = v }
          }

          TargetStrip {
            width: parent.width
            height: Style.space(44)
          }

          Choice {
            label: "Correction"
            options: [
              { value: "peq", label: "Parametric EQ" },
              { value: "fir", label: "FIR" }
            ]
            value: root.mode
            onPicked: function(v) { root.mode = v }
          }

          ResponseGraph {
            width: parent.width
            height: Style.space(130)
            visible: !!(root.session && root.session.grid)
          }

          // Progress while measuring.
          Column {
            width: parent.width
            spacing: Style.space(6)
            visible: root.calibrating

            Text {
              text: root.calMessage + (root.calStage === "measure" ? " · keep quiet" : "")
              textFormat: Text.PlainText
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Rectangle {
              width: parent.width
              height: Style.space(6)
              radius: height / 2
              color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.12)

              Rectangle {
                height: parent.height
                radius: parent.radius
                width: Math.max(parent.height, parent.width * root.calFraction)
                color: root.colorPlanned
                // Style.duration honours reduced motion where the release has it.
                Behavior on width { NumberAnimation { duration: typeof Style.duration === "function" ? Style.duration(250) : 250; easing.type: Easing.OutCubic } }
              }
            }
          }

          Message { visible: root.calError !== ""; text: root.calError; urgent: true }

          Repeater {
            model: root.calWarnings
            Message { required property var modelData; text: modelData }
          }

          Message {
            visible: !root.calibrating && text !== ""
            text: root.fitMatches
              ? Model.fitSummary(root.fit) + (Model.checkSummary(root.check) ? "\n" + Model.checkSummary(root.check) : "")
              : root.previewReady
                ? "With these settings: " + Model.fitSummary(root.preview).replace(/^Off target by/, "off target by") + ". Build to use them."
                : (root.previewing && root.previewFailedKey !== root.selectionKey ? "Working out what these settings would do…" : "")
          }

          Row {
            id: actions
            width: parent.width
            spacing: Style.space(6)
            readonly property real cellWidth: (width - spacing * 3) / 4

            Button {
              width: actions.cellWidth
              text: root.measurementCount === 0 ? "Measure" : "Measure +1"
              tooltipText: root.measurementCount === 0
                ? "About 30 seconds of sweeps"
                : root.measurementCount + " position" + (root.measurementCount === 1 ? "" : "s") + " so far. Move the microphone a little and measure again"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              fontSize: Style.font.bodySmall
              bordered: true
              enabled: !root.busy && root.mic !== "" && root.output !== ""
              opacity: enabled ? 1 : 0.4
              onClicked: root.measure()
            }

            Button {
              width: actions.cellWidth
              text: "Build"
              tooltipText: "Fit filters to the measurements and listen"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              fontSize: Style.font.bodySmall
              bordered: true
              active: root.measurementCount > 0 && !root.fit
              enabled: !root.busy && root.measurementCount > 0
              opacity: enabled ? 1 : 0.4
              onClicked: root.build()
            }

            Button {
              width: actions.cellWidth
              text: "Check"
              tooltipText: "Measure again through CamillaDSP and compare with the plan"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              fontSize: Style.font.bodySmall
              bordered: true
              // Checks the calibration the current choices name, so it must be the one playing.
              enabled: !root.busy && root.fitMatches && root.dspState === "on" && root.status.preset === root.fit.preset
              opacity: enabled ? 1 : 0.4
              onClicked: root.startCalibration("check")
            }

            Button {
              width: actions.cellWidth
              text: "Start over"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              fontSize: Style.font.bodySmall
              enabled: !root.busy && root.measurementCount > 0
              opacity: enabled ? 1 : 0.4
              onClicked: root.run(["calibrate", "reset"])
            }
          }
        }
      }
    }
  }

  // ---------- Components ----------

  component Message: Text {
    property bool urgent: false
    width: parent ? parent.width : 0
    visible: text !== ""
    textFormat: Text.PlainText
    wrapMode: Text.WordWrap
    color: urgent ? Commons.Color.urgent : root.bar.foreground
    opacity: urgent ? 1 : 0.7
    font.family: root.bar.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  // A label and a row of mutually exclusive buttons.
  component Choice: Column {
    id: choice
    property string label: ""
    property var options: []
    property string value: ""
    signal picked(string value)

    width: parent ? parent.width : 0
    spacing: Style.space(4)

    Text {
      text: choice.label
      textFormat: Text.PlainText
      color: root.bar.foreground
      opacity: 0.6
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.caption
    }

    Row {
      id: choiceRow
      width: parent.width
      spacing: Style.space(6)
      readonly property real cellWidth: choice.options.length > 0
        ? (width - spacing * (choice.options.length - 1)) / choice.options.length : 0

      Repeater {
        model: choice.options
        Button {
          required property var modelData
          width: choiceRow.cellWidth
          text: modelData.label
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          fontSize: Style.font.bodySmall
          bordered: true
          active: choice.value === modelData.value
          enabled: !root.busy
          onClicked: choice.picked(modelData.value)
        }
      }
    }
  }

  component InfoPair: Row {
    property string label: ""
    property string value: ""

    width: parent ? parent.width : 0
    spacing: Style.space(8)

    Text {
      id: pairLabel
      text: parent.label
      textFormat: Text.PlainText
      color: root.bar.foreground
      opacity: 0.6
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Text {
      width: parent.width - pairLabel.implicitWidth - parent.spacing
      horizontalAlignment: Text.AlignRight
      elide: Text.ElideMiddle
      text: parent.value
      textFormat: Text.PlainText
      color: root.bar.foreground
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }

  // Measured (faint), target (accent), and planned or checked (solid), on a
  // log-frequency axis from 20 Hz to 20 kHz, ±15 dB.
  component ResponseGraph: Canvas {
    id: graph
    readonly property var grid: root.session ? root.session.grid : null
    // After a check, both curves come from the spot it measured at; before
    // one, the faint curve is the average of every position.
    readonly property var measured: root.fitMatches && root.check && root.check.channels[0].raw
      ? Model.meanCurve(root.check.channels.map(function(c) { return c.raw }))
      : (root.session ? Model.meanCurve(root.session.curves) : [])
    // The target follows the Sound choice at once; the solid curve is the built
    // calibration (or its check) when it matches the choices, otherwise the
    // preview of what they would do.
    readonly property var targetCurve: grid ? Model.targetCurve(root.target, grid) : []
    readonly property bool showCheck: root.fitMatches && !!root.check
    // The built calibration with the switches applied, when they change it.
    readonly property var switched: root.fitMatches && Model.switchesShape(root.options)
      ? Model.switchedCurve(root.fit, root.options, Model.loudnessShare(root.status, root.options)) : []
    // Deep bass acts below the bass limit and adds harmonics up to three times it.
    readonly property real deepBassKnee: root.fitMatches && root.options.deep_bass === true ? Model.fitKnee(root.fit) : 0
    readonly property var planned: showCheck
      ? Model.meanCurve(root.check.channels.map(function(c) { return c.checked }))
      : (root.fitMatches ? Model.meanCurve(root.fit.channels.map(function(c) { return c.predicted }))
        : (root.previewReady ? Model.meanCurve(root.preview.channels.map(function(c) { return c.predicted })) : []))
    readonly property color ink: root.bar.foreground
    readonly property color accent: Commons.Color.accent

    onMeasuredChanged: requestPaint()
    onPlannedChanged: requestPaint()
    onSwitchedChanged: requestPaint()
    onDeepBassKneeChanged: requestPaint()
    Connections {
      target: root
      function onThemePaletteChanged() { graph.requestPaint() }
    }
    onTargetCurveChanged: requestPaint()
    onWidthChanged: requestPaint()
    onInkChanged: requestPaint()

    function stroke(ctx, values, color, alpha, width, dashed, smoothed) {
      var pts = Model.curvePoints(grid, smoothed ? Model.smoothCurve(values, 2) : values, graph.width, graph.height, 15)
      if (pts.length < 2) return
      ctx.save()
      ctx.globalAlpha = alpha
      ctx.strokeStyle = color
      ctx.lineWidth = width
      ctx.lineJoin = "round"
      if (dashed) ctx.setLineDash([4, 4])
      root.tracePath(ctx, pts)
      ctx.stroke()
      ctx.restore()
    }

    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      ctx.save()
      ctx.globalAlpha = 0.12
      ctx.strokeStyle = ink
      ctx.lineWidth = 1
      var marks = [100, 1000, 10000]
      for (var i = 0; i < marks.length; i++) {
        var x = Model.frequencyX(grid, marks[i], width)
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke()
      }
      ctx.beginPath(); ctx.moveTo(0, height / 2); ctx.lineTo(width, height / 2); ctx.stroke()
      ctx.restore()
      if (deepBassKnee > 0) {
        var below = Model.frequencyX(grid, deepBassKnee, width)
        var top = Model.frequencyX(grid, deepBassKnee * 3, width)
        ctx.save()
        ctx.fillStyle = root.colorDeepBass
        // Faint where the missing notes are, stronger where their harmonics go.
        ctx.globalAlpha = 0.07
        ctx.fillRect(0, 0, below, height)
        ctx.globalAlpha = 0.16
        ctx.fillRect(below, 0, top - below, height)
        ctx.restore()
      }
      stroke(ctx, measured, root.colorMeasured, 0.75, 1.5, false, true)
      stroke(ctx, targetCurve, root.colorTarget, 0.9, 1.5, true, false)
      stroke(ctx, planned, showCheck ? root.colorChecked : root.colorPlanned, switched.length > 0 ? 0.55 : 1.0, 2, false, true)
      stroke(ctx, switched, root.colorSwitched, 1.0, 2, false, true)
    }

    // A key with a sample of each line in its colour; wraps when it is long.
    Flow {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.bottom: parent.bottom
      spacing: Style.space(10)

      LegendItem { tint: root.colorMeasured; label: graph.showCheck ? "without" : "measured" }
      LegendItem {
        visible: graph.planned.length > 0
        tint: graph.showCheck ? root.colorChecked : root.colorPlanned
        label: graph.showCheck ? "with correction" : "planned"
      }
      LegendItem { tint: root.colorTarget; label: "target"; dashed: true }
      LegendItem { visible: graph.switched.length > 0; tint: root.colorSwitched; label: "with switches" }
      LegendItem { visible: graph.deepBassKnee > 0; tint: root.colorDeepBass; label: "deep bass" }
      LegendItem {
        visible: graph.planned.length === 0 && root.previewing
        tint: root.colorPlanned
        label: "working out the plan…"
        swatch: false
      }
    }
  }

  // Neutral, Warm and Room side by side, the chosen one in the accent colour:
  // what each choice asks the correction to aim for, ±5 dB from 20 Hz to 20 kHz.
  component TargetStrip: Canvas {
    id: strip
    readonly property var grid: Model.logGrid(96)
    readonly property string selected: root.target
    readonly property color ink: root.bar.foreground
    readonly property color accent: Commons.Color.accent

    onSelectedChanged: requestPaint()
    Connections {
      target: root
      function onThemePaletteChanged() { strip.requestPaint() }
    }
    onWidthChanged: requestPaint()
    onInkChanged: requestPaint()

    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      var plot = height - Style.space(12)
      ctx.save()
      ctx.globalAlpha = 0.12
      ctx.strokeStyle = ink
      ctx.lineWidth = 1
      ctx.beginPath(); ctx.moveTo(0, plot / 2); ctx.lineTo(width, plot / 2); ctx.stroke()
      ctx.restore()
      var names = ["flat", "warm", "room"]
      // The chosen curve last, so it is drawn on top.
      names.sort(function(a, b) { return (a === selected) - (b === selected) })
      for (var n = 0; n < names.length; n++) {
        var pts = Model.curvePoints(grid, Model.targetCurve(names[n], grid), width, plot, 5)
        ctx.save()
        ctx.globalAlpha = names[n] === selected ? 1.0 : 0.35
        ctx.strokeStyle = root.targetColors[names[n]]
        ctx.lineWidth = names[n] === selected ? 2 : 1
        root.tracePath(ctx, pts)
        ctx.stroke()
        ctx.restore()
      }
    }

    Repeater {
      model: [{ hz: 100, label: "100" }, { hz: 1000, label: "1k" }, { hz: 10000, label: "10k" }]
      Text {
        required property var modelData
        x: Model.frequencyX(strip.grid, modelData.hz, strip.width) - width / 2
        anchors.bottom: parent.bottom
        text: modelData.label
        textFormat: Text.PlainText
        color: root.bar.foreground
        opacity: 0.45
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
      }
    }
  }

  // A section title that folds its section away, showing a one-line summary
  // of it in its place.
  component FoldHeader: Item {
    id: fold
    property string text: ""
    property string summary: ""
    property bool collapsed: false
    signal toggled()

    implicitHeight: Math.max(foldTitle.implicitHeight, chevron.implicitHeight)

    Text {
      id: chevron
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(14)
      text: fold.collapsed ? "\uf054" : "\uf078"
      textFormat: Text.PlainText
      color: root.bar.foreground
      opacity: foldMouse.containsMouse ? 0.9 : 0.5
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.caption
    }

    PanelSectionHeader {
      id: foldTitle
      anchors.left: chevron.right
      anchors.verticalCenter: parent.verticalCenter
      text: fold.text
      foreground: root.bar.foreground
      fontFamily: root.bar.fontFamily
    }

    Text {
      anchors.left: foldTitle.right
      anchors.leftMargin: Style.space(10)
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      visible: fold.collapsed && fold.summary !== ""
      text: fold.summary
      textFormat: Text.PlainText
      horizontalAlignment: Text.AlignRight
      elide: Text.ElideRight
      color: root.bar.foreground
      opacity: 0.7
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    MouseArea {
      id: foldMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: fold.toggled()
    }
  }

  // A switch with its name and what it does.
  component SwitchRow: Item {
    id: row
    property string label: ""
    property string description: ""
    property bool checked: false
    property bool available: true
    property color tint: Commons.Color.accent
    signal switched(bool on)

    width: parent ? parent.width : 0
    implicitHeight: Math.max(rowText.implicitHeight, toggle.implicitHeight)
    opacity: available ? 1 : 0.5

    Column {
      id: rowText
      anchors.left: parent.left
      anchors.right: toggle.left
      anchors.rightMargin: Style.space(12)
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(2)

      Text {
        width: parent.width
        text: row.label
        textFormat: Text.PlainText
        color: row.checked && row.available ? row.tint : root.bar.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
      }

      Text {
        width: parent.width
        text: row.description
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: root.bar.foreground
        opacity: 0.6
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    ToggleSwitch {
      id: toggle
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      checked: row.checked
      busy: root.busy
      interactive: row.available && !root.busy
      foreground: root.bar.foreground
      accent: row.tint
      onToggled: row.switched(!row.checked)
    }
  }

  // One entry of the graph's key: a short sample of the line, and its name.
  component LegendItem: Row {
    property color tint: Commons.Color.accent
    property string label: ""
    property bool dashed: false
    property bool swatch: true
    spacing: Style.space(4)

    Row {
      visible: swatch
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(2)
      Repeater {
        model: dashed ? 2 : 1
        Rectangle {
          width: dashed ? Style.space(4) : Style.space(10)
          height: Math.max(2, Style.space(2))
          radius: height / 2
          color: tint
        }
      }
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      text: label
      textFormat: Text.PlainText
      color: tint
      opacity: 0.9
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}
