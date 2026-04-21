import tkinter as tk
from tkinter import ttk
import subprocess
import threading
import time
from audio_utils import list_devices, get_bt_devices, ensure_a2dp_sink, activate_bt_source_cards, deactivate_bt_source_cards, _extract_mac, _normalize_mac
from capture import CapturePipeline, NullSinkManager, check_active_links

class MultiPhoneSwitcher(tk.Tk):
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

        # UI Layout
        self.label = ttk.Label(self, text="Select Active Phone", font=("Roboto", 20))
        self.label.pack(pady=10)

        # Dropdown for Paired Devices
        self.device_var = tk.StringVar(value="Select a device")
        self.device_menu = ttk.Combobox(self, textvariable=self.device_var, width=40)
        self.device_menu.pack(pady=5)
        self.device_menu.bind("<<ComboBoxSelected>>", self.on_source_select)


        # Dropdown for Speaker Output
        self.speaker_var = tk.StringVar(value="Select a speaker")
        self.speaker_menu = ttk.Combobox(self, textvariable=self.speaker_var, width=40)
        self.speaker_menu.pack(pady=5)
        self.speaker_menu.bind("<<ComboBoxSelected>>", self.on_sink_select)

        # Refresh Button
        self.refresh_btn = ttk.Button(self, text="Refresh Lists", command=self.refresh_lists)
        self.refresh_btn.pack(pady=5)

        # Single action button — refreshes lists then routes selected phone → speaker
        self.connect_btn = ttk.Button(self, text="Connect Pair", command=self.connect_pair)
        self.connect_btn.pack(pady=10)

        self.status_label = ttk.Label(self, text="Status: Initializing...", foreground="gray")
        self.status_label.pack(pady=10)

        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.refresh_lists()
        self.after(300, self._try_auto_start)

    def refresh_lists(self):
        """Refreshes the list of available Bluetooth devices and speaker sinks."""
        self.bt_devices = get_bt_devices()
        self.speaker_sinks = [
            sink for sink in list_devices("sinks")
            if sink.get("name") != self.null_sink_manager.NULL_SINK_NAME
        ]

        bt_names = [dev['description'] for dev in self.bt_devices]
        speaker_names = [f"{s['description']}" for s in self.speaker_sinks]

        self.device_menu['values'] = bt_names if bt_names else ["No BT inputs found"]
        self.speaker_menu['values'] = speaker_names if speaker_names else ["No speakers found"]

        if not bt_names:
            self.device_var.set("No BT inputs found")
        else:
            if self.device_var.get() not in bt_names:
                self.device_var.set(bt_names[0])

        if not speaker_names:
            self.speaker_var.set("No speakers found")
        else:
            if self.speaker_var.get() not in speaker_names:
                self.speaker_var.set(speaker_names[0])

        self.status_label.config(text="Lists Refreshed", foreground="gray")

    def on_source_select(self, event=None):
        """Callback when a new source is selected from the dropdown."""
        choice = self.device_var.get()
        if not (self.capture_pipeline and self.capture_pipeline.is_running()):
            self.start_hub()
            return

        selected_device = next((dev for dev in self.bt_devices if dev['description'] == choice), None)
        if not selected_device:
            self.status_label.config(text=f"Error: Could not find device for {choice}", foreground="red")
            return

        source_name = selected_device.get('source_name')

        # Device is paired but idle — activate its A2DP source profile (set to 'off'
        # on last close) then wait for WirePlumber to create the source node.
        if not source_name:
            card_name = selected_device.get('name', '')
            if card_name.startswith('bluez_card.'):
                subprocess.run(
                    ["pactl", "set-card-profile", card_name, "a2dp-source"],
                    capture_output=True, text=True,
                )
            self.status_label.config(text=f"Activating {choice}...", foreground="yellow")
            self.update_idletasks()
            for _ in range(5):
                time.sleep(1)
                fresh = get_bt_devices()
                found = next(
                    (d for d in fresh
                     if d['device_mac'] == selected_device['device_mac'] and d.get('source_name')),
                    None,
                )
                if found:
                    source_name = found['source_name']
                    selected_device = found
                    self.bt_devices = fresh
                    self.device_menu['values'] = [d['description'] for d in fresh]
                    break

        if not source_name:
            self.status_label.config(
                text=f"{choice} is not streaming — play audio on the device first.",
                foreground="orange",
            )
            return

        # Tell the watcher which source to protect before switching the link.
        self.null_sink_manager.set_active_source(source_name)

        if self.capture_pipeline.switch_source(source_name):
            self.status_label.config(text=f"Switched to {choice}", foreground="green")
        else:
            error = self.capture_pipeline.last_error or f"Could not switch to {choice}."
            self.status_label.config(text=f"Error: {error}", foreground="red")

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
        """Activate all phone cards, refresh lists, then route selected phone to speaker."""
        activate_bt_source_cards(exclude_macs=self._speaker_macs())
        self.after(1500, self._connect_pair_after_wake)
        self.status_label.config(text="Waking up Bluetooth sources...", foreground="yellow")

    def _connect_pair_after_wake(self):
        self.refresh_lists()
        self.start_hub()

    def _try_auto_start(self):
        """Activate all phone BT cards then auto-start the hub after a short delay."""
        # Speaker sinks may already be populated from __init__ refresh_lists().
        activate_bt_source_cards(exclude_macs=self._speaker_macs())
        self.status_label.config(text="Waking up Bluetooth sources...", foreground="yellow")
        self.after(2000, self._auto_start_after_wake)

    def _auto_start_after_wake(self):
        self.refresh_lists()
        if self.bt_devices and self.speaker_sinks:
            self.start_hub()
        else:
            self.status_label.config(
                text="No devices found. Connect a phone and press Connect Pair.",
                foreground="orange",
            )

    def start_hub(self):
        """Starts the exclusive capture mode."""
        self.refresh_lists()
        
        source_choice = self.device_var.get()
        sink_choice = self.speaker_var.get()

        print(f"HUB_DEBUG: Source selected in dropdown: '{source_choice}'")
        print(f"HUB_DEBUG: Sink selected in dropdown: '{sink_choice}'")

        initial_device = next((dev for dev in self.bt_devices if dev['description'] == source_choice), None)
        initial_sink = next((s for s in self.speaker_sinks if s['description'] == sink_choice), None)

        print(f"HUB_DEBUG: Found device object: {initial_device}")
        print(f"HUB_DEBUG: Found sink object: {initial_sink}")

        if not initial_device or not initial_sink:
            self.status_label.config(text="Error: Select a valid phone and speaker.", foreground="red")
            print("HUB_DEBUG: ERROR - Could not find initial device or sink object.")
            return
            
        if not initial_device.get('source_name'):
            card_name = initial_device.get('name', '')
            if card_name.startswith('bluez_card.'):
                self.status_label.config(text=f"Activating {initial_device['description']}...", foreground="yellow")
                self.update_idletasks()
                subprocess.run(
                    ["pactl", "set-card-profile", card_name, "a2dp-source"],
                    capture_output=True, text=True,
                )
                for _ in range(5):
                    time.sleep(1)
                    fresh = get_bt_devices()
                    found = next(
                        (d for d in fresh
                         if d['device_mac'] == initial_device['device_mac'] and d.get('source_name')),
                        None,
                    )
                    if found:
                        initial_device = found
                        self.bt_devices = fresh
                        self.device_menu['values'] = [d['description'] for d in fresh]
                        break
            if not initial_device.get('source_name'):
                self.status_label.config(
                    text=f"{initial_device['description']} — play audio on the device first.",
                    foreground="orange",
                )
                print(f"HUB_DEBUG: ERROR - Device '{initial_device['description']}' has no active source node.")
                return

        self.status_label.config(text="Starting hub...", foreground="yellow")
        self.update_idletasks()

        capture_source = initial_device['source_name']
        capture_sink = initial_sink['name']
        
        print(f"HUB_DEBUG: Starting capture with SOURCE: '{capture_source}' and SINK: '{capture_sink}'")

        # Re-enable source card profile if it was silenced by a previous watcher
        # cycle, then unmute the sink (our pause mechanism).
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
            return

        self.capture_pipeline = pipeline
        null_sink_ready = self.null_sink_manager.setup()
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
        """Full cleanup on window close: remove links, stop watcher.
        All phone BT cards are set to 'off' via _list_bt_cards() (catches
        even idle cards not in self.bt_devices) so WirePlumber cannot
        auto-route them after the app exits.
        """
        self.null_sink_manager.teardown()
        # Kill all phone card profiles BEFORE removing links so WirePlumber
        # has no source node to route when teardown() fires.
        deactivate_bt_source_cards(exclude_macs=self._speaker_macs())
        if self.capture_pipeline:
            subprocess.run(
                ["pactl", "set-sink-mute", self.capture_pipeline.sink_name, "0"],
                capture_output=True, text=True,
            )
            self.capture_pipeline.teardown()
            self.capture_pipeline = None
        self.destroy()

if __name__ == "__main__":
    app = MultiPhoneSwitcher()
    app.mainloop()