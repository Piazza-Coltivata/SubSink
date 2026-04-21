import tkinter as tk
from tkinter import ttk
import subprocess
import threading
import time
from audio_utils import list_devices, get_bt_devices
from capture import CapturePipeline, NullSinkManager

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
        self.refresh_btn.pack(pady=10)

        self.status_label = ttk.Label(self, text="Status: Initializing...", foreground="gray")
        self.status_label.pack(pady=10)

        # Main Action Button
        self.start_stop_btn = ttk.Button(self, text="Start Hub", command=self.toggle_hub)
        self.start_stop_btn.pack(pady=20)

        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.refresh_lists()
        self.status_label.config(text="Status: Ready. Press 'Start Hub'.")

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
        if self.capture_pipeline and self.capture_pipeline.is_running():
            selected_device = next((dev for dev in self.bt_devices if dev['description'] == choice), None)
            if selected_device and selected_device.get('source_name'):
                if self.capture_pipeline.switch_source(selected_device['source_name']):
                    self.null_sink_manager.set_active_source(selected_device['source_name'])
                    self.status_label.config(text=f"Switched to {choice}", foreground="green")
                else:
                    error = self.capture_pipeline.last_error or f"Could not switch to {choice}."
                    self.status_label.config(text=f"Error: {error}", foreground="red")
            elif selected_device:
                self.status_label.config(text=f"Error: {choice} is connected but not streaming audio.", foreground="orange")
            else:
                self.status_label.config(text=f"Error: Could not find device for {choice}", foreground="red")


    def on_sink_select(self, event=None):
        """Placeholder for switching output sink if needed in the future."""
        choice = self.speaker_var.get()
        self.status_label.config(text=f"Output set to {choice}", foreground="cyan")


    def toggle_hub(self):
        if self.capture_pipeline and self.capture_pipeline.is_running():
            self.stop_hub()
        else:
            self.start_hub()

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
            self.status_label.config(text=f"Error: {initial_device['description']} is connected but not streaming audio.", foreground="red")
            print(f"HUB_DEBUG: ERROR - Device '{initial_device['description']}' has no active source node.")
            return

        self.status_label.config(text="Starting hub...", foreground="yellow")
        self.update_idletasks()

        capture_source = initial_device['source_name']
        capture_sink = initial_sink['name']
        
        print(f"HUB_DEBUG: Starting capture with SOURCE: '{capture_source}' and SINK: '{capture_sink}'")

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

        self.start_stop_btn.config(text="Stop Hub")
        self.status_label.config(
            text=f"Hub Active. Playing from {initial_device['description']}",
            foreground="green",
        )
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
        """Stops the capture and cleans up."""
        if self.capture_pipeline:
            self.capture_pipeline.stop()
            self.capture_pipeline = None
        self.null_sink_manager.teardown()
        self.start_stop_btn.config(text="Start Hub")
        self.status_label.config(text="Status: Hub Stopped.", foreground="gray")

    def on_closing(self):
        """Ensure cleanup when the window is closed."""
        self.stop_hub()
        self.destroy()

if __name__ == "__main__":
    app = MultiPhoneSwitcher()
    app.mainloop()