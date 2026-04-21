import atexit
import signal
import tkinter as tk
from tkinter import ttk
import subprocess
import time
from audio_utils import list_devices, get_bt_devices, ensure_a2dp_sink, ensure_a2dp_source, recover_bt_audio_device, _extract_mac, _normalize_mac
from capture import CapturePipeline, NullSinkManager, check_active_links, cleanup_stale_bt_routes

class MultiPhoneSwitcher(tk.Tk):
    DEVICE_REFRESH_MS = 2000
    SOURCE_ACTIVATION_RETRIES = 10
    PENDING_ROUTE_POLL_MS = 500
    PENDING_ROUTE_RECOVERY_SECONDS = 3
    PENDING_ROUTE_DIAGNOSTIC_SECONDS = 8
    PENDING_ROUTE_STABLE_SECONDS = 1.5
    RECOVERY_COOLDOWN_SECONDS = 12

    def __init__(self):
        super().__init__()
        self.title("Pi 5 Audio Hub")
        self.geometry("450x350")

        # Style for a more modern look
        style = ttk.Style(self)
        style.theme_use('clam')

        # Audio backend
        self.null_sink_manager = NullSinkManager()
        self.capture_pipeline = None
        self.speaker_sinks = []
        self.bt_devices = []
        self.pending_route = None
        self.pending_route_after_id = None
        self._cleanup_done = False
        self._recovery_cooldowns = {}
        self._device_state_snapshot = {}
        self._session_log_active = False

        # UI Layout
        self.label = ttk.Label(self, text="Select Active Phone", font=("Roboto", 20))
        self.label.pack(pady=10)

        # Dropdown for Paired Devices
        self.device_var = tk.StringVar(value="Select a device")
        self.device_menu = ttk.Combobox(self, textvariable=self.device_var, width=40)
        self.device_menu.pack(pady=5)
        self.device_menu.bind("<<ComboboxSelected>>", self.on_source_choice_changed)


        # Dropdown for Speaker Output
        self.speaker_var = tk.StringVar(value="Select a speaker")
        self.speaker_menu = ttk.Combobox(self, textvariable=self.speaker_var, width=40)
        self.speaker_menu.pack(pady=5)
        self.speaker_menu.bind("<<ComboboxSelected>>", self.on_sink_select)

        # Refresh Button
        self.refresh_btn = ttk.Button(self, text="Refresh Lists", command=self.refresh_lists)
        self.refresh_btn.pack(pady=5)

        # Single action button — refreshes lists then routes selected phone → speaker
        self.connect_btn = ttk.Button(self, text="Connect Pair", command=self.connect_pair)
        self.connect_btn.pack(pady=10)

        self.status_label = ttk.Label(self, text="Status: Initializing...", foreground="gray")
        self.status_label.pack(pady=10)

        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        atexit.register(self._shutdown_audio_state)
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(signum, self._handle_exit_signal)
            except (ValueError, OSError):
                pass
        self.refresh_lists()
        self.after(300, self._try_auto_start)
        self.after(self.DEVICE_REFRESH_MS, self._schedule_device_refresh)

    def _shutdown_audio_state(self):
        """Best-effort audio cleanup for UI closes and process exits."""
        if self._cleanup_done:
            return
        self._cleanup_done = True

        try:
            self._cancel_pending_route()
        except Exception:
            pass

        try:
            self.null_sink_manager.stop_watcher()
        except Exception:
            pass

        if self.capture_pipeline:
            try:
                subprocess.run(
                    ["pactl", "set-sink-mute", self.capture_pipeline.sink_name, "0"],
                    capture_output=True, text=True,
                )
            except Exception:
                pass
            try:
                self.capture_pipeline.teardown()
            except Exception:
                pass
            self.capture_pipeline = None

        try:
            self.null_sink_manager.teardown()
        except Exception:
            pass

    def _handle_exit_signal(self, signum, frame):
        """Ensure audio cleanup still runs when the process exits via signal."""
        del frame
        self._shutdown_audio_state()
        raise SystemExit(128 + signum)

    def refresh_lists(self, update_status=True):
        """Refreshes the list of available Bluetooth devices and speaker sinks."""
        selected_device = next(
            (dev for dev in self.bt_devices if dev['description'] == self.device_var.get()),
            None,
        )
        selected_device_mac = selected_device.get('device_mac') if selected_device else None
        selected_speaker = self.speaker_var.get()

        new_bt_devices = get_bt_devices()
        self.speaker_sinks = [
            sink for sink in list_devices("sinks")
            if sink.get("name") != self.null_sink_manager.NULL_SINK_NAME
        ]

        self._sync_device_menu(new_bt_devices, preferred_mac=selected_device_mac)

        bt_names = [dev['description'] for dev in self.bt_devices]
        speaker_names = [f"{s['description']}" for s in self.speaker_sinks]

        self.speaker_menu['values'] = speaker_names if speaker_names else ["No speakers found"]

        if not speaker_names:
            self.speaker_var.set("No speakers found")
        else:
            if selected_speaker in speaker_names:
                self.speaker_var.set(selected_speaker)
            elif self.speaker_var.get() not in speaker_names:
                preferred_bluetooth_speaker = next(
                    (
                        sink['description']
                        for sink in self.speaker_sinks
                        if sink.get('name', '').startswith('bluez_output.')
                    ),
                    None,
                )
                self.speaker_var.set(preferred_bluetooth_speaker or speaker_names[0])

        if update_status:
            self.status_label.config(text="Lists Refreshed", foreground="gray")

    def _schedule_device_refresh(self):
        """Keep the device list current even while the app stays open for a long time."""
        try:
            self.refresh_lists(update_status=False)
        finally:
            if self.winfo_exists():
                self.after(self.DEVICE_REFRESH_MS, self._schedule_device_refresh)

    def _cancel_pending_route(self, stop_idle_watcher=True):
        """Cancel any queued start or switch waiting for a source node to appear."""
        if self.pending_route_after_id:
            try:
                self.after_cancel(self.pending_route_after_id)
            except tk.TclError:
                pass
        self.pending_route = None
        self.pending_route_after_id = None
        self.null_sink_manager.set_protected_device(None)
        if stop_idle_watcher and not (self.capture_pipeline and self.capture_pipeline.is_running()):
            self.null_sink_manager.stop_watcher()

    def _queue_pending_route(self, selected_device, selected_sink, mode):
        """Wait for the chosen device to start streaming, then finish the route automatically."""
        self.pending_route = {
            "device_mac": selected_device.get('device_mac'),
            "device_description": selected_device.get('description'),
            "sink_name": selected_sink.get('name') if selected_sink else None,
            "sink_description": selected_sink.get('description') if selected_sink else self.speaker_var.get(),
            "mode": mode,
            "queued_at": time.time(),
            "source_recovery_attempted": False,
            "diagnostic_logged": False,
            "stable_source_name": None,
            "stable_since": None,
        }
        self.null_sink_manager.set_protected_device(selected_device.get('device_mac'))
        watcher_source = None
        if self.capture_pipeline and self.capture_pipeline.is_running():
            if mode == 'switch':
                # Keep the current source playing until the pending target
                # actually exposes a live PipeWire input node.
                self.null_sink_manager.stop_watcher()
            else:
                watcher_source = self.capture_pipeline.source_name
        if selected_sink and selected_sink.get('name') and mode != 'switch':
            self.null_sink_manager.start_watcher(watcher_source, selected_sink.get('name'))
        self.status_label.config(
            text=f"Waiting for audio from {selected_device.get('description', 'device')}...",
            foreground="orange",
        )
        if self.pending_route_after_id is None and self.winfo_exists():
            self.pending_route_after_id = self.after(
                self.PENDING_ROUTE_POLL_MS,
                self._poll_pending_route,
            )

    def _poll_pending_route(self):
        """Complete a queued start or switch after the chosen source node appears."""
        self.pending_route_after_id = None
        pending = self.pending_route
        if not pending or not self.winfo_exists():
            return

        self.refresh_lists(update_status=False)

        pending_mac = _normalize_mac(pending.get('device_mac'))
        selected_device = next(
            (
                dev for dev in self.bt_devices
                if _normalize_mac(dev.get('device_mac')) == pending_mac
            ),
            None,
        )
        selected_sink = next(
            (
                sink for sink in self.speaker_sinks
                if sink.get('name') == pending.get('sink_name')
            ),
            None,
        )
        if not selected_sink:
            selected_sink = next(
                (
                    sink for sink in self.speaker_sinks
                    if sink.get('description') == pending.get('sink_description')
                ),
                None,
            )

        elapsed = max(0.0, time.time() - float(pending.get('queued_at') or time.time()))
        waiting_for = (selected_device or {}).get('description') or pending.get('device_description') or 'device'

        if (
            pending_mac
            and selected_device
            and not selected_device.get('source_name')
            and not pending.get('source_recovery_attempted')
            and elapsed >= self.PENDING_ROUTE_RECOVERY_SECONDS
        ):
            pending['source_recovery_attempted'] = True
            self._append_session_log(
                "PENDING_ROUTE_SOURCE_RECOVERY: "
                f"mode={pending.get('mode')} "
                f"device={waiting_for} "
                f"mac={pending_mac} "
                f"elapsed={elapsed:.1f}s "
                f"card_present={bool(selected_device.get('pipewire_card_present', True))} "
                f"profile_ready={bool(selected_device.get('audio_profile_ready', False))}\n"
            )
            self.status_label.config(
                text=f"Recovering audio source for {waiting_for}...",
                foreground="yellow",
            )
            self.update_idletasks()
            recover_bt_audio_device(
                pending_mac,
                log_file="pipeline_errors.log",
                require_live_source=True,
            )
            self.refresh_lists(update_status=False)
            selected_device = next(
                (
                    dev for dev in self.bt_devices
                    if _normalize_mac(dev.get('device_mac')) == pending_mac
                ),
                None,
            )
            selected_sink = next(
                (
                    sink for sink in self.speaker_sinks
                    if sink.get('name') == pending.get('sink_name')
                ),
                None,
            )
            if not selected_sink:
                selected_sink = next(
                    (
                        sink for sink in self.speaker_sinks
                        if sink.get('description') == pending.get('sink_description')
                    ),
                    None,
                )

        if selected_device and selected_device.get('source_name') and selected_sink:
            source_name = selected_device.get('source_name')
            stable_source_name = pending.get('stable_source_name')
            stable_since = pending.get('stable_since')
            now = time.time()

            if stable_source_name != source_name:
                pending['stable_source_name'] = source_name
                pending['stable_since'] = now
            elif stable_since is None:
                pending['stable_since'] = now

            stable_elapsed = max(0.0, now - float(pending.get('stable_since') or now))
            if stable_elapsed < self.PENDING_ROUTE_STABLE_SECONDS:
                waiting_for = selected_device.get('description') or pending.get('device_description') or 'device'
                self.status_label.config(
                    text=f"Confirming stable audio from {waiting_for}...",
                    foreground="orange",
                )
                if self.winfo_exists():
                    self.pending_route_after_id = self.after(
                        self.PENDING_ROUTE_POLL_MS,
                        self._poll_pending_route,
                    )
                return

            self.pending_route = None
            self.device_var.set(selected_device['description'])
            self.speaker_var.set(selected_sink['description'])
            self._append_session_log(
                "PENDING_ROUTE_READY: "
                f"mode={pending.get('mode')} "
                f"device={selected_device.get('description')} "
                f"source={selected_device.get('source_name')} "
                f"sink={selected_sink.get('name')}\n"
            )
            if pending.get('mode') == 'switch' and self.capture_pipeline and self.capture_pipeline.is_running():
                self.on_source_select()
            else:
                self.start_hub()
            return

        if elapsed >= self.PENDING_ROUTE_DIAGNOSTIC_SECONDS and not pending.get('diagnostic_logged'):
            pending['diagnostic_logged'] = True
            self._append_session_log(
                "PENDING_ROUTE_STILL_IDLE: "
                f"mode={pending.get('mode')} "
                f"device={waiting_for} "
                f"elapsed={elapsed:.1f}s\n"
            )
            self._log_runtime_snapshot(
                f"PENDING_ROUTE_STILL_IDLE {waiting_for} elapsed={elapsed:.1f}s"
            )

        if elapsed >= self.PENDING_ROUTE_DIAGNOSTIC_SECONDS:
            self.status_label.config(
                text=(
                    f"{waiting_for} is connected but no live audio source appeared yet. "
                    "Start playback on the device."
                ),
                foreground="orange",
            )
        else:
            self.status_label.config(
                text=f"Waiting for audio from {waiting_for}...",
                foreground="orange",
            )
        if self.winfo_exists():
            self.pending_route_after_id = self.after(
                self.PENDING_ROUTE_POLL_MS,
                self._poll_pending_route,
            )

    def _reset_session_log(self):
        """Start a fresh log for the next connection attempt."""
        with open("pipeline_errors.log", "w") as log_handle:
            log_handle.write("--- New Session ---\n")
        self._session_log_active = True
        self._device_state_snapshot = {}

    def _append_session_log(self, content):
        """Append a debug snapshot to the session log file."""
        with open("pipeline_errors.log", "a") as log_handle:
            log_handle.write(content)

    def _log_device_state_changes(self, devices):
        """Record card/source availability transitions once per MAC."""
        next_snapshot = {}
        now_text = time.strftime('%Y-%m-%d %H:%M:%S')

        for device in devices:
            device_mac = _normalize_mac(device.get('device_mac') or '')
            if not device_mac:
                continue

            state = {
                'card_present': bool(device.get('pipewire_card_present', True)),
                'profile_ready': bool(device.get('audio_profile_ready', False)),
                'source_present': bool(device.get('source_name')),
                'description': device.get('description') or device_mac,
            }
            previous_state = self._device_state_snapshot.get(device_mac)

            if self._session_log_active and previous_state and previous_state != state:
                self._append_session_log(
                    f"DEVICE_STATE_CHANGE @ {now_text}: "
                    f"mac={device_mac} "
                    f"description={state['description']} "
                    f"card_present={previous_state['card_present']}->{state['card_present']} "
                    f"profile_ready={previous_state['profile_ready']}->{state['profile_ready']} "
                    f"source_present={previous_state['source_present']}->{state['source_present']}\n"
                )

            next_snapshot[device_mac] = state

        if self._session_log_active:
            for device_mac, previous_state in self._device_state_snapshot.items():
                if device_mac in next_snapshot:
                    continue
                self._append_session_log(
                    f"DEVICE_STATE_CHANGE @ {now_text}: "
                    f"mac={device_mac} "
                    f"description={previous_state['description']} "
                    "removed_from_device_list=True\n"
                )

        self._device_state_snapshot = next_snapshot

    def _summarize_bt_cards(self):
        """Return a concise summary of Bluetooth card names and active profiles."""
        result = subprocess.run(
            ["pactl", "list", "cards"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return [f"ERROR: pactl list cards failed: {result.stderr.strip()}"]

        summary_lines = []
        include_card = False
        for raw_line in result.stdout.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("Card #"):
                include_card = False
            elif stripped.startswith("Name:") and "bluez_card." in stripped:
                include_card = True
                summary_lines.append(stripped)
            elif include_card and (
                stripped.startswith("device.description =")
                or stripped.startswith("device.alias =")
                or stripped.startswith("Active Profile:")
            ):
                summary_lines.append(stripped)

        return summary_lines or ["[no Bluetooth cards found]"]

    def _run_command_for_log(self, command):
        """Run a command and return stdout/stderr for log snapshots."""
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
        )
        output = result.stdout.strip()
        error = result.stderr.strip()
        if error:
            output = f"{output}\n[stderr]\n{error}" if output else f"[stderr]\n{error}"
        return output or "[no output]"

    def _log_runtime_snapshot(self, label):
        """Capture the current Bluetooth and PipeWire state in the session log."""
        lines = [f"\n=== {label} @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"]
        lines.append(f"Selected source: {self.device_var.get()}\n")
        lines.append(f"Selected sink: {self.speaker_var.get()}\n")
        if self.capture_pipeline:
            lines.append(
                "Capture pipeline: "
                f"running={self.capture_pipeline.is_running()} "
                f"source={self.capture_pipeline.source_name} "
                f"sink={self.capture_pipeline.sink_name}\n"
            )
        else:
            lines.append("Capture pipeline: [none]\n")

        lines.append("Known BT devices:\n")
        if self.bt_devices:
            for device in self.bt_devices:
                lines.append(
                    "  - "
                    f"{device.get('description')} | "
                    f"card={device.get('name')} | "
                    f"source={device.get('source_name')} | "
                    f"mac={device.get('device_mac')} | "
                    f"card_present={device.get('pipewire_card_present', True)} | "
                    f"profile_ready={device.get('audio_profile_ready', False)}\n"
                )
        else:
            lines.append("  [none]\n")

        lines.append("Bluetooth cards:\n")
        for line in self._summarize_bt_cards():
            lines.append(f"  {line}\n")

        commands = [
            (["pactl", "list", "sources", "short"], "pactl list sources short"),
            (["pactl", "list", "sinks", "short"], "pactl list sinks short"),
            (["pw-link", "-iol"], "pw-link -iol"),
        ]
        for command, label_text in commands:
            lines.append(f"--- {label_text} ---\n")
            lines.append(f"{self._run_command_for_log(command)}\n")

        self._append_session_log("".join(lines))

    def _sync_device_menu(self, devices, preferred_mac=None):
        """Update the source dropdown while preserving the selected device."""
        self._log_device_state_changes(devices)
        self.bt_devices = devices
        bt_names = [dev['description'] for dev in devices]
        self.device_menu['values'] = bt_names if bt_names else ["No BT inputs found"]

        normalized_preferred_mac = _normalize_mac(preferred_mac or "")
        preferred_device = None
        if normalized_preferred_mac:
            preferred_device = next(
                (
                    dev for dev in devices
                    if _normalize_mac(dev.get('device_mac')) == normalized_preferred_mac
                ),
                None,
            )

        if preferred_device:
            self.device_var.set(preferred_device['description'])
        elif not bt_names:
            self.device_var.set("No BT inputs found")
        elif self.device_var.get() not in bt_names:
            self.device_var.set(bt_names[0])

    def _activate_selected_device(self, selected_device, status_text):
        """Ensure the selected BT device profile is active and refresh its latest state."""
        if selected_device.get('source_name'):
            return selected_device

        device_mac = selected_device.get('device_mac', '')
        latest_devices = self.bt_devices
        active_capture_mac = ''
        if self.capture_pipeline and self.capture_pipeline.is_running():
            active_capture_mac = _normalize_mac(_extract_mac(self.capture_pipeline.source_name))

        if (
            device_mac
            and not selected_device.get('pipewire_card_present', True)
            and device_mac == active_capture_mac
        ):
            self._append_session_log(
                "ACTIVE_SOURCE_IDLE_MISSING: "
                f"mac={device_mac} current pipeline source is selected but PipeWire "
                "has no live card/source right now; waiting for it to return without Bluetooth reconnect.\n"
            )
            latest_devices = get_bt_devices()
            self._sync_device_menu(latest_devices, preferred_mac=device_mac)
        elif device_mac and not selected_device.get('pipewire_card_present', True):
            now = time.time()
            last_recovery_at = float(self._recovery_cooldowns.get(device_mac) or 0)
            if now - last_recovery_at >= self.RECOVERY_COOLDOWN_SECONDS:
                self.status_label.config(
                    text=f"Recovering audio for {selected_device.get('description', 'device')}...",
                    foreground="yellow",
                )
                self.update_idletasks()
                self._recovery_cooldowns[device_mac] = now
                recover_bt_audio_device(device_mac, log_file="pipeline_errors.log")
            else:
                remaining = max(0.0, self.RECOVERY_COOLDOWN_SECONDS - (now - last_recovery_at))
                self._append_session_log(
                    "RECOVER_BT_AUDIO_COOLDOWN: "
                    f"mac={device_mac} remaining={remaining:.1f}s; skipping reconnect and waiting for a stable source.\n"
                )
            latest_devices = get_bt_devices()
            self._sync_device_menu(latest_devices, preferred_mac=device_mac)

        selected_device = next(
            (
                dev for dev in latest_devices
                if dev['device_mac'] == device_mac
            ),
            selected_device,
        )
        card_name = selected_device.get('name', '')

        if selected_device.get('pipewire_card_present', True) and card_name.startswith('bluez_card.'):
            self.status_label.config(text=status_text, foreground="yellow")
            self.update_idletasks()
            if not ensure_a2dp_source(card_name, log_file="pipeline_errors.log"):
                return None
            latest_devices = get_bt_devices()
            self._sync_device_menu(latest_devices, preferred_mac=device_mac)
            selected_device = next(
                (
                    dev for dev in latest_devices
                    if dev['device_mac'] == device_mac
                ),
                selected_device,
            )

        return selected_device

    def _enforce_exclusive_source(self, active_source_name, active_sink_name):
        """Keep only the selected source linked to the speaker, without disconnecting others."""
        if not active_source_name or not active_sink_name:
            return
        removed_links = self.null_sink_manager.sync_inactive_sources(active_source_name, active_sink_name)
        self._append_session_log(
            f"EXCLUSIVE: active_source={active_source_name} active_sink={active_sink_name} "
            f"removed_links={removed_links}\n"
        )

    def on_source_choice_changed(self, event=None):
        """Update the UI selection without starting routing work yet."""
        del event

        if self.pending_route:
            self._cancel_pending_route(stop_idle_watcher=False)
            if self.capture_pipeline and self.capture_pipeline.is_running():
                self.null_sink_manager.start_watcher(
                    self.capture_pipeline.source_name,
                    self.capture_pipeline.sink_name,
                )

        choice = self.device_var.get()
        if choice in ("No BT inputs found", "Select a device"):
            self.status_label.config(
                text="Select a Bluetooth input, then press Connect Pair.",
                foreground="gray",
            )
            return

        action = "switch" if self.capture_pipeline and self.capture_pipeline.is_running() else "start"
        self.status_label.config(
            text=f"Selected {choice}. Press Connect Pair to {action}.",
            foreground="cyan",
        )

    def on_source_select(self, event=None):
        """Apply the currently selected source after an explicit action request."""
        self._cancel_pending_route()
        choice = self.device_var.get()
        if choice in ("No BT inputs found", "Select a device"):
            self.status_label.config(text="No Bluetooth input is available to switch.", foreground="orange")
            return
        if not (self.capture_pipeline and self.capture_pipeline.is_running()):
            self.start_hub()
            return

        self._log_runtime_snapshot(f"SWITCH_REQUEST {choice}")

        selected_device = next((dev for dev in self.bt_devices if dev['description'] == choice), None)
        if not selected_device:
            self.status_label.config(text=f"Error: Could not find device for {choice}", foreground="red")
            self._log_runtime_snapshot(f"SWITCH_SELECTION_MISSING {choice}")
            return

        selected_device = self._activate_selected_device(
            selected_device,
            f"Activating {choice}...",
        )
        if selected_device is None:
            self.status_label.config(
                text=f"Could not activate {choice}. See pipeline_errors.log.",
                foreground="orange",
            )
            self._log_runtime_snapshot(f"SWITCH_ACTIVATION_FAILED {choice}")
            return

        source_name = selected_device.get('source_name')

        if not source_name:
            active_sink = next(
                (
                    sink for sink in self.speaker_sinks
                    if sink.get('name') == self.capture_pipeline.sink_name
                ),
                {
                    'name': self.capture_pipeline.sink_name,
                    'description': self.speaker_var.get(),
                },
            )
            self._queue_pending_route(selected_device, active_sink, mode='switch')
            self._log_runtime_snapshot(f"SWITCH_WAITING_FOR_SOURCE {choice}")
            return

        # Quiet the outgoing source before the active handoff completes.
        previous_source = self.capture_pipeline.source_name
        if previous_source and previous_source != source_name:
            self.null_sink_manager.hold_source(previous_source)
        self.null_sink_manager.set_active_source(source_name)

        if self.capture_pipeline.switch_source(source_name):
            self._enforce_exclusive_source(source_name, self.capture_pipeline.sink_name)
            self.null_sink_manager.start_watcher(source_name, self.capture_pipeline.sink_name)
            self._sync_device_menu(get_bt_devices(), preferred_mac=selected_device.get('device_mac'))
            self.status_label.config(text=f"Switched to {self.device_var.get()}", foreground="green")
            self._log_runtime_snapshot(f"SWITCH_SUCCESS {self.device_var.get()}")
        else:
            error = self.capture_pipeline.last_error or f"Could not switch to {choice}."
            self.status_label.config(text=f"Error: {error}", foreground="red")
            self._enforce_exclusive_source(self.capture_pipeline.source_name, self.capture_pipeline.sink_name)
            self.null_sink_manager.start_watcher(
                self.capture_pipeline.source_name,
                self.capture_pipeline.sink_name,
            )
            self._log_runtime_snapshot(f"SWITCH_FAILURE {choice}")

    def on_sink_select(self, event=None):
        """Placeholder for switching output sink if needed in the future."""
        choice = self.speaker_var.get()
        self.status_label.config(text=f"Output set to {choice}", foreground="cyan")


    def _speaker_macs(self):
        """Return the set of normalized MACs for all currently listed speaker sinks."""
        return {
            _normalize_mac(_extract_mac(s.get('name', '')))
            for s in self.speaker_sinks
            if s.get('name', '').startswith('bluez_output.')
        }

    def connect_pair(self):
        """Route the selected phone or laptop to the speaker exclusively."""
        if self.capture_pipeline and self.capture_pipeline.is_running():
            self.refresh_lists()
            if self.device_var.get() in ("No BT inputs found", "Select a device"):
                self.status_label.config(text="No Bluetooth input is available to switch.", foreground="orange")
                return
            self._log_runtime_snapshot("CONNECT_PAIR_SWITCH_REQUEST")
            self.on_source_select()
            return

        self._reset_session_log()
        self._cleanup_orphaned_routes()
        self.refresh_lists()
        self._log_runtime_snapshot("CONNECT_PAIR_REQUEST")
        self.start_hub()

    def _connect_pair_after_wake(self):
        self.refresh_lists()
        self.start_hub()

    def _cleanup_orphaned_routes(self):
        """Clear stale Bluetooth source links left behind by a previous app run."""
        if self.capture_pipeline:
            return
        cleanup_stale_bt_routes()

    def _try_auto_start(self):
        """Auto-start only when a source is already streaming at launch."""
        self._reset_session_log()
        self._cleanup_orphaned_routes()
        self.after(500, self._auto_start_after_wake)

    def _auto_start_after_wake(self):
        self.refresh_lists()
        if any(device.get('source_name') for device in self.bt_devices) and self.speaker_sinks:
            self.start_hub()
        else:
            self.status_label.config(
                text="Select a device, start playback, then press Connect Pair.",
                foreground="gray",
            )

    def start_hub(self):
        """Starts the exclusive capture mode."""
        self._cancel_pending_route(stop_idle_watcher=False)
        self.refresh_lists()
        
        source_choice = self.device_var.get()
        sink_choice = self.speaker_var.get()

        print(f"HUB_DEBUG: Source selected in dropdown: '{source_choice}'")
        print(f"HUB_DEBUG: Sink selected in dropdown: '{sink_choice}'")
        self._log_runtime_snapshot(f"START_HUB_REQUEST source={source_choice} sink={sink_choice}")

        initial_device = next((dev for dev in self.bt_devices if dev['description'] == source_choice), None)
        initial_sink = next((s for s in self.speaker_sinks if s['description'] == sink_choice), None)

        print(f"HUB_DEBUG: Found device object: {initial_device}")
        print(f"HUB_DEBUG: Found sink object: {initial_sink}")

        if not initial_device or not initial_sink:
            self.status_label.config(text="Error: Select a valid phone and speaker.", foreground="red")
            print("HUB_DEBUG: ERROR - Could not find initial device or sink object.")
            self._log_runtime_snapshot("START_HUB_SELECTION_INVALID")
            return

        initial_device = self._activate_selected_device(
            initial_device,
            f"Activating {initial_device['description']}...",
        )
        if initial_device is None:
            self.status_label.config(
                text=f"Could not activate {source_choice}. See pipeline_errors.log.",
                foreground="orange",
            )
            print(f"HUB_DEBUG: ERROR - Could not activate source for '{source_choice}'.")
            self._log_runtime_snapshot(f"START_HUB_ACTIVATION_FAILED {source_choice}")
            return
        if not initial_device.get('source_name'):
            self._queue_pending_route(initial_device, initial_sink, mode='start')
            print(f"HUB_DEBUG: Waiting for source node from '{initial_device['description']}'.")
            self._log_runtime_snapshot(f"START_HUB_WAITING_FOR_SOURCE {initial_device['description']}")
            return

        self.status_label.config(text="Starting hub...", foreground="yellow")
        self.update_idletasks()

        capture_source = initial_device['source_name']
        capture_sink = initial_sink['name']
        
        print(f"HUB_DEBUG: Starting capture with SOURCE: '{capture_source}' and SINK: '{capture_sink}'")

        # Restore speaker output in case the hub was previously paused.
        self.null_sink_manager.set_active_source(capture_source)
        subprocess.run(["pactl", "set-sink-mute", capture_sink, "0"],
                       capture_output=True, text=True)

        # Force A2DP on the speaker — recovers from HFP mode.
        if capture_sink.startswith("bluez_output."):
            mac_part = capture_sink[len("bluez_output."):].rsplit(".", 1)[0]
            ensure_a2dp_sink(f"bluez_card.{mac_part}")

        # Teardown any existing pipeline and create a fresh one.
        # If the phone stayed connected (Stop Hub kept links), pw-link returns
        # "file exists" which is treated as OK — minimal disruption.
        if self.capture_pipeline:
            self.capture_pipeline.teardown()
            self.capture_pipeline = None

        pipeline = CapturePipeline(capture_source, capture_sink)
        if not pipeline.is_running():
            error = pipeline.last_error or "Could not create PipeWire links."
            self.status_label.config(text=f"Error: {error}", foreground="red")
            print(f"HUB_DEBUG: ERROR - {error}")
            self._log_runtime_snapshot(f"START_HUB_PIPELINE_FAILED {capture_source}")
            return

        self.capture_pipeline = pipeline
        self.null_sink_manager.setup()
        self._enforce_exclusive_source(capture_source, capture_sink)
        null_sink_ready = True
        if null_sink_ready:
            self.null_sink_manager.start_watcher(
                initial_device.get('source_name'),
                initial_sink.get('name'),
            )

        self.status_label.config(
            text=f"Hub Active. Playing from {initial_device['description']}",
            foreground="green",
        )
        check_active_links(initial_sink['name'])
        self._log_runtime_snapshot(f"START_HUB_SUCCESS {initial_device['description']}")
        self._schedule_hub_refresh()
    
    def _schedule_hub_refresh(self):
        """Refresh the source dropdown every 5 s while the hub is running."""
        if not (self.capture_pipeline and self.capture_pipeline.is_running()):
            return
        new_devices = get_bt_devices()
        new_names = [dev['description'] for dev in new_devices]
        current_values = list(self.device_menu['values'])
        if new_names != current_values:
            self.bt_devices = new_devices
            self.device_menu['values'] = new_names
        self.after(5000, self._schedule_hub_refresh)

    def stop_hub(self):
        """Pause the hub. The speaker sink is muted instead of removing links.
        Keeping links alive prevents iOS/Android from dropping the A2DP Source
        connection when there is no active consumer.
        """
        self.null_sink_manager.stop_watcher()
        if self.capture_pipeline:
            sink_name = self.capture_pipeline.sink_name
            result = subprocess.run(
                ["pactl", "set-sink-mute", sink_name, "1"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"Sink {sink_name} muted — links kept alive.")
            else:
                print(f"Warning: could not mute sink {sink_name}: {result.stderr.strip()}")
            self.capture_pipeline.stop()  # logical stop; links remain
            # Keep self.capture_pipeline set so on_closing() can restore the sink.
        self.status_label.config(text="Status: Hub Paused.", foreground="gray")

    def on_closing(self):
        """Full cleanup on window close: remove links and restore neutral BT state."""
        self._shutdown_audio_state()
        self.destroy()

if __name__ == "__main__":
    app = MultiPhoneSwitcher()
    app.mainloop()