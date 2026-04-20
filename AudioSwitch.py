import customtkinter as ctk
import subprocess
import threading
import time
from audio_utils import list_devices, get_bt_devices
from capture import CapturePipeline, NullSinkManager

ctk.set_appearance_mode("dark")

class MultiPhoneSwitcher(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Pi 5 Audio Hub")
        self.geometry("450x350")

        # Audio backend
        self.null_sink_manager = NullSinkManager()
        self.capture_pipeline = None
        self.speaker_sinks = []
        self.bt_devices = []

        # UI Layout
        self.label = ctk.CTkLabel(self, text="Select Active Phone", font=("Roboto", 20))
        self.label.pack(pady=10)

        # Dropdown for Paired Devices
        self.device_var = ctk.StringVar(value="Select a device")
        self.device_menu = ctk.CTkComboBox(self, values=[], variable=self.device_var, width=300, command=self.on_source_select)
        self.device_menu.pack(pady=5)

        # Dropdown for Speaker Output
        self.speaker_var = ctk.StringVar(value="Select a speaker")
        self.speaker_menu = ctk.CTkComboBox(self, values=[], variable=self.speaker_var, width=300, command=self.on_sink_select)
        self.speaker_menu.pack(pady=5)

        # Refresh Button
        self.refresh_btn = ctk.CTkButton(self, text="Refresh Lists", fg_color="transparent", border_width=1, command=self.refresh_lists)
        self.refresh_btn.pack(pady=10)

        self.status_label = ctk.CTkLabel(self, text="Status: Initializing...", text_color="gray")
        self.status_label.pack(pady=10)

        # Main Action Button
        self.start_stop_btn = ctk.CTkButton(self, text="Start Hub", command=self.toggle_hub)
        self.start_stop_btn.pack(pady=20)

        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.refresh_lists()
        self.status_label.configure(text="Status: Ready. Press 'Start Hub'.")

    def refresh_lists(self):
        """Refreshes the list of available Bluetooth devices and speaker sinks."""
        self.bt_devices = get_bt_devices()
        self.speaker_sinks = [s for s in list_devices("sinks") if "bluez" not in s.get("name", "")]

        bt_names = [dev['description'] for dev in self.bt_devices]
        speaker_names = [f"{s['description']}" for s in self.speaker_sinks]

        self.device_menu.configure(values=bt_names if bt_names else ["No BT devices found"])
        self.speaker_menu.configure(values=speaker_names if speaker_names else ["No speakers found"])

        if not bt_names:
            self.device_var.set("No BT devices found")
        else:
            # If the current selection is no longer valid, reset it
            if self.device_var.get() not in bt_names:
                self.device_var.set(bt_names[0])

        if not speaker_names:
            self.speaker_var.set("No speakers found")
        else:
            if self.speaker_var.get() not in speaker_names:
                self.speaker_var.set(speaker_names[0])

        self.status_label.configure(text="Lists Refreshed", text_color="gray")

    def on_source_select(self, choice):
        """Callback when a new source is selected from the dropdown."""
        if self.capture_pipeline and self.capture_pipeline._running:
            selected_device = next((dev for dev in self.bt_devices if dev['description'] == choice), None)
            if selected_device and selected_device['monitor_source_name']:
                self.capture_pipeline.switch_source(selected_device['monitor_source_name'])
                self.status_label.configure(text=f"Switched to {choice}", text_color="green")
            elif selected_device:
                self.status_label.configure(text=f"Error: {choice} is not playing audio.", text_color="orange")
            else:
                self.status_label.configure(text=f"Error: Could not find device for {choice}", text_color="red")


    def on_sink_select(self, choice):
        """Placeholder for switching output sink if needed in the future."""
        self.status_label.configure(text=f"Output set to {choice}", text_color="cyan")


    def toggle_hub(self):
        if self.capture_pipeline and self.capture_pipeline._running:
            self.stop_hub()
        else:
            self.start_hub()

    def start_hub(self):
        """Starts the exclusive capture mode."""
        self.refresh_lists()
        
        source_choice = self.device_var.get()
        sink_choice = self.speaker_var.get()

        initial_device = next((dev for dev in self.bt_devices if dev['description'] == source_choice), None)
        initial_sink = next((s for s in self.speaker_sinks if s['description'] == sink_choice), None)

        if not initial_device or not initial_sink:
            self.status_label.configure(text="Error: Select a valid phone and speaker.", text_color="red")
            return
            
        if not initial_device['monitor_source_name']:
            self.status_label.configure(text=f"Error: {initial_device['description']} is not playing audio.", text_color="red")
            return

        self.status_label.configure(text="Starting exclusive mode...", text_color="yellow")
        self.null_sink_manager.setup()

        self.capture_pipeline = CapturePipeline(initial_device['monitor_source_name'], initial_sink['name'])
        self.capture_pipeline.start()

        self.start_stop_btn.configure(text="Stop Hub")
        self.status_label.configure(text=f"Hub Active. Playing from {initial_device['description']}", text_color="green")

    def stop_hub(self):
        """Stops the capture and cleans up."""
        if self.capture_pipeline:
            self.capture_pipeline.stop()
            self.capture_pipeline = None
        self.null_sink_manager.teardown()
        self.start_stop_btn.configure(text="Start Hub")
        self.status_label.configure(text="Status: Hub Stopped.", text_color="gray")

    def on_closing(self):
        """Ensure cleanup when the window is closed."""
        self.stop_hub()
        self.destroy()

if __name__ == "__main__":
    app = MultiPhoneSwitcher()
    app.mainloop()