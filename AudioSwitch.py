import customtkinter as ctk
import subprocess
import time

ctk.set_appearance_mode("dark")

class MultiPhoneSwitcher(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Pi 5 Audio Hub")
        self.geometry("450x300")

        # UI Layout
        self.label = ctk.CTkLabel(self, text="Select Device to Route", font=("Roboto", 20))
        self.label.pack(pady=15)

        # Dropdown for Paired Devices
        self.device_var = ctk.StringVar(value="Select a device")
        self.device_menu = ctk.CTkComboBox(self, values=self.get_paired_devices(), variable=self.device_var, width=300)
        self.device_menu.pack(pady=10)

        # Refresh Button
        self.refresh_btn = ctk.CTkButton(self, text="Refresh Device List", fg_color="transparent", border_width=1, command=self.refresh_list)
        self.refresh_btn.pack(pady=5)

        self.status_label = ctk.CTkLabel(self, text="Status: Ready", text_color="gray")
        self.status_label.pack(pady=10)

        # Main Action Button
        self.route_btn = ctk.CTkButton(self, text="Route Audio to Speaker", command=self.route_audio)
        self.route_btn.pack(pady=20)

    def get_paired_devices(self):
        """Returns a list of 'Name (MAC)' for all paired devices."""
        devices = []
        result = subprocess.run(["bluetoothctl", "paired-devices"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            # Line format: 'Device XX:XX:XX:XX:XX:XX Name'
            parts = line.split(" ", 2)
            if len(parts) > 2:
                devices.append(f"{parts[2]} ({parts[1]})")
        return devices

    def refresh_list(self):
        self.device_menu.configure(values=self.get_paired_devices())
        self.status_label.configure(text="List Refreshed", text_color="gray")

    def route_audio(self):
        selection = self.device_var.get()
        if "(" not in selection:
            return

        # Extract MAC address from the string "Name (MAC)"
        mac = selection.split("(")[-1].replace(")", "")
        self.status_label.configure(text=f"Connecting to {selection[:15]}...", text_color="yellow")
        self.update()

        try:
            # 1. Ensure the phone is connected
            subprocess.run(["bluetoothctl", "connect", mac], check=True)
            time.sleep(1)

            # 2. Get the Speaker Sink Name (automatically finds the default speaker)
            default_sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True).stdout.strip()

            # 3. Move all inputs (the phone's audio stream) to the default speaker
            streams = subprocess.run(["pactl", "list", "short", "sink-inputs"], capture_output=True, text=True).stdout
            for line in streams.splitlines():
                stream_id = line.split()[0]
                subprocess.run(["pactl", "move-sink-input", stream_id, default_sink])

            self.status_label.configure(text="Audio Routed Successfully!", text_color="green")
        except Exception as e:
            self.status_label.configure(text="Routing Failed", text_color="red")

if __name__ == "__main__":
    app = MultiPhoneSwitcher()
    app.mainloop()