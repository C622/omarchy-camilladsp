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

  // PipeWire tells the shell when the default output changes, so the icon
  // follows an output switched elsewhere without polling.
  readonly property var defaultSink: Pipewire.defaultAudioSink
  readonly property bool routedLive: !!(defaultSink && defaultSink.name === Model.SINK) && !!status && status.service === "active"
  readonly property string dspState: Model.state(status, routedLive)
  readonly property bool hideWhenOff: setting("hideWhenOff", false) === true
  readonly property var presets: (status && status.presets) || []
  readonly property var fit: session ? session.fit : null
  readonly property var check: session ? session.check : null
  readonly property int measurementCount: session ? (session.measurements || 0) : 0
  readonly property var micOptions: Model.usableMics(devices)
  readonly property var micInfo: Model.micInfo(devices, mic)
  readonly property var calibrationOptions: Model.calibrationOptions(devices)
  readonly property string micCalibration: micInfo && micInfo.calibration ? micInfo.calibration.path : ""
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
    if (id) run(["preset", id])
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

  function setMicCalibration(path) {
    if (!mic || path === micCalibration) return
    run(path ? ["calibrate", "mic-cal", "--mic", mic, "--file", path] : ["calibrate", "mic-cal", "--mic", mic, "--clear"])
  }

  function build() {
    startCalibration("fit", ["--target", target, "--mode", mode])
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

  function activateCursor() {
    if (presetIndex >= 0 && presetIndex < presets.length) applyPreset(presets[presetIndex].id)
  }

  ShellIpc {
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

  visible: !(hideWhenOff && dspState !== "on")
  implicitWidth: visible ? button.implicitWidth : 0
  implicitHeight: visible ? button.implicitHeight : 0

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
        if (next.fit) {
          root.target = next.fit.target || root.target
          root.mode = next.fit.mode || root.mode
        }
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
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight)

    content: PanelKeyCatcher {
      anchors.fill: parent
      implicitHeight: column.implicitHeight
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
            color: root.bar.foreground
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
              color: Qt.darker(root.bar.foreground, 1.4)
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

            PanelSectionHeader {
              id: presetHeader
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: "Presets"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
            }

            Button {
              id: editButton
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

          Repeater {
            model: root.presets

            Button {
              required property var modelData
              required property int index
              width: parent.width
              leftAlign: true
              text: modelData.title
              iconText: modelData.id === Model.CALIBRATED ? "\uf130" : ""
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
            }
          }
        }

        // ---------- Calibration ----------
        PanelSeparator { foreground: root.bar.foreground; visible: root.dspState !== "setup" && root.dspState !== "loading" }

        Column {
          width: parent.width
          spacing: Style.space(10)
          visible: root.dspState !== "setup" && root.dspState !== "loading"

          PanelSectionHeader {
            text: "Calibrate with a microphone"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
          }

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

          Choice {
            label: "Correction"
            options: [
              { value: "peq", label: "Parametric EQ" },
              { value: "fir", label: "FIR" }
            ]
            value: root.mode
            onPicked: function(v) { root.mode = v }
          }

          Message {
            text: root.position === "listening"
              ? "Put the microphone where your head usually is. Measure from a few spots around it and the correction averages them, which also tames the room. Both speakers are time and level aligned to that spot."
              : "Measures the speakers themselves: the room barely counts. The built-in microphone works for this."
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
                color: root.bar.foreground
                Behavior on width { NumberAnimation { duration: Style.duration(250); easing.type: Easing.Bezier; easing.bezierCurve: Style.easeOut } }
              }
            }
          }

          Message { visible: root.calError !== ""; text: root.calError; urgent: true }

          Repeater {
            model: root.calWarnings
            Message { required property var modelData; text: modelData }
          }

          Message {
            visible: !root.calibrating && Model.fitSummary(root.fit) !== ""
            text: Model.fitSummary(root.fit) + (Model.checkSummary(root.check) ? "\n" + Model.checkSummary(root.check) : "")
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
              enabled: !root.busy && !!root.fit && root.dspState === "on" && root.status.preset === Model.CALIBRATED
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
    readonly property var measured: root.session ? Model.meanCurve(root.session.curves) : []
    readonly property var targetCurve: root.fit ? root.fit.target_curve : []
    readonly property var planned: root.check
      ? Model.meanCurve(root.check.channels.map(function(c) { return c.checked }))
      : (root.fit ? Model.meanCurve(root.fit.channels.map(function(c) { return c.predicted })) : [])
    readonly property color ink: root.bar.foreground
    readonly property color accent: Commons.Color.accent

    onMeasuredChanged: requestPaint()
    onPlannedChanged: requestPaint()
    onTargetCurveChanged: requestPaint()
    onWidthChanged: requestPaint()
    onInkChanged: requestPaint()

    function stroke(ctx, values, color, alpha, width, dashed) {
      var pts = Model.curvePoints(grid, values, graph.width, graph.height, 15)
      if (pts.length < 2) return
      ctx.save()
      ctx.globalAlpha = alpha
      ctx.strokeStyle = color
      ctx.lineWidth = width
      if (dashed) ctx.setLineDash([4, 4])
      ctx.beginPath()
      ctx.moveTo(pts[0].x, pts[0].y)
      for (var i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y)
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
      stroke(ctx, measured, ink, 0.35, 1.5, false)
      stroke(ctx, targetCurve, accent, 0.9, 1.5, true)
      stroke(ctx, planned, ink, 1.0, 2, false)
    }

    Text {
      anchors.left: parent.left
      anchors.bottom: parent.bottom
      text: root.check ? "faint: before · solid: checked · dashed: target"
        : (root.fit ? "faint: measured · solid: planned · dashed: target" : "measured")
      textFormat: Text.PlainText
      color: root.bar.foreground
      opacity: 0.5
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}
