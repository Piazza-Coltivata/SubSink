import time
import threading
import subprocess


class AudioManager:

    def __init__(self, usb_audio_device):
        self.usb_audio_device = usb_audio_device
        self._cycling = False
        self._cycle_thread = None
        self._loopback_module_id = None

    def _pactl(self, *args):
        """Run a pactl command and return stdout."""
        result = subprocess.run(
            ["pactl"] + list(args),
            capture_output=True, text=True
        )
        return result

    def _parse_sinks(self):
        """Parse pactl list sinks into a list of dicts with index, name, description."""
        result = self._pactl("list", "sinks")
        if result.returncode != 0:
            return []
        sinks = []
        current = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("Sink #"):
                if current:
                    sinks.append(current)
                current = {"index": line.split("#")[1]}
            elif line.startswith("Name:"):
                current["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Description:"):
                current["description"] = line.split(":", 1)[1].strip()
        if current:
            sinks.append(current)
        return sinks

    def _parse_sources(self):
        """Parse pactl list sources into a list of dicts with index, name, description."""
        result = self._pactl("list", "sources")
        if result.returncode != 0:
            return []
        sources = []
        current = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("Source #"):
                if current:
                    sources.append(current)
                current = {"index": line.split("#")[1]}
            elif line.startswith("Name:"):
                current["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Description:"):
                current["description"] = line.split(":", 1)[1].strip()
        if current:
            sources.append(current)
        return sources

    def setup(self):
        print("Setting up audio routing...")

        print("   Restarting PulseAudio to apply changes...")
        subprocess.run(["pulseaudio", "-k"], capture_output=True)
        time.sleep(1)
        subprocess.run(["pulseaudio", "--start"], capture_output=True)
        time.sleep(2)

        print("   Loading Bluetooth audio modules...")
        for module in ["module-bluetooth-discover", "module-bluetooth-policy", "module-switch-on-connect"]:
            self._pactl("load-module", module)

        self._set_default_sink()
        self._print_available_devices()
        return True

    def _set_default_sink(self):
        if self.usb_audio_device != "default":
            try:
                for sink in self._parse_sinks():
                    desc = sink.get("description", "")
                    name = sink.get("name", "")
                    if self.usb_audio_device in desc or self.usb_audio_device == name:
                        self._pactl("set-default-sink", name)
                        print(f"Audio routing configured successfully. Default sink set to: '{name}'")
                        return
                print(f"ERROR: No sink matching '{self.usb_audio_device}' found.")
            except Exception as e:
                print(f"ERROR: Failed to set default audio sink: {e}")
        else:
            print("Using default audio sink.")

    def _print_available_devices(self):
        print("\nAvailable audio devices:")
        try:
            for sink in self._parse_sinks():
                print(f"   [sink] {sink.get('name', '?')} — {sink.get('description', '?')}")
            for source in self._parse_sources():
                print(f"   [source] {source.get('name', '?')} — {source.get('description', '?')}")
        except Exception as e:
            print(f"   Could not list devices: {e}")

    def get_sources(self):
        try:
            return [{"id": s["index"], "name": s["name"], "description": s.get("description", s["name"])} for s in self._parse_sources()]
        except Exception:
            return []

    def set_default_source(self, source_name):
        try:
            result = self._pactl("set-default-source", source_name)
            if result.returncode != 0:
                return False
            self._move_source_outputs(source_name)
            return True
        except Exception:
            return False

    def _move_source_outputs(self, source_name):
        """Move all existing source-outputs (recording streams) to the given source."""
        result = self._pactl("list", "short", "source-outputs")
        if result.returncode != 0:
            return
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts:
                stream_id = parts[0]
                self._pactl("move-source-output", stream_id, source_name)

    def get_sinks(self):
        try:
            return [{"id": s["index"], "name": s["name"], "description": s.get("description", s["name"])} for s in self._parse_sinks()]
        except Exception:
            return []

    def set_default_sink_by_name(self, sink_name):
        try:
            result = self._pactl("set-default-sink", sink_name)
            if result.returncode != 0:
                return False
            self._move_sink_inputs(sink_name)
            return True
        except Exception:
            return False

    def _move_sink_inputs(self, sink_name):
        """Move all existing sink-inputs (playback streams) to the given sink."""
        result = self._pactl("list", "short", "sink-inputs")
        if result.returncode != 0:
            return
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts:
                stream_id = parts[0]
                self._pactl("move-sink-input", stream_id, sink_name)

    def start_loopback(self, source_name, sink_name, latency_msec=200):
        """Load a PulseAudio module-loopback to bridge a source to a sink with buffering."""
        self.stop_loopback()
        result = self._pactl(
            "load-module", "module-loopback",
            f"source={source_name}",
            f"sink={sink_name}",
            f"latency_msec={latency_msec}",
            "adjust_time=3"
        )
        if result.returncode == 0:
            module_id = result.stdout.strip()
            self._loopback_module_id = module_id
            print(f"Loopback started (module {module_id}): {source_name} -> {sink_name}, buffer {latency_msec}ms")
            return True
        else:
            print(f"ERROR: Failed to start loopback: {result.stderr.strip()}")
            return False

    def stop_loopback(self):
        """Unload the current loopback module if one is running."""
        if self._loopback_module_id:
            self._pactl("unload-module", self._loopback_module_id)
            print(f"Loopback stopped (module {self._loopback_module_id})")
            self._loopback_module_id = None

    def start_cycle_test(self, interval=2):
        sources = self.get_sources()
        if len(sources) < 2:
            print(f"Only {len(sources)} source(s) available — need at least 2 to cycle.")
            for s in sources:
                print(f"   {s['id']}: {s['name']}")
            return

        print(f"\nStarting source cycle test ({interval}s per source).")
        print(f"Found {len(sources)} sources:")
        for s in sources:
            print(f"   {s['id']}: {s['name']}")
        print("Press Ctrl+C to stop.\n")

        self._cycling = True
        self._cycle_thread = threading.Thread(
            target=self._cycle_loop, args=(interval,), daemon=True
        )
        self._cycle_thread.start()

    def stop_cycle_test(self):
        self._cycling = False
        if self._cycle_thread:
            self._cycle_thread.join(timeout=5)
            self._cycle_thread = None

    def _cycle_loop(self, interval):
        idx = 0
        while self._cycling:
            sources = self.get_sources()
            if not sources:
                print("No sources available, stopping cycle.")
                break
            source = sources[idx % len(sources)]
            if self.set_default_source(source["name"]):
                print(f"[cycle] Switched to source: {source['name']}")
            else:
                print(f"[cycle] Failed to switch to source: {source['name']}")
            idx += 1
            time.sleep(interval)
    
    # ----------------------------------------------------------------
    # Bluetooth output (A2DP sink) — uncomment when ready to test
    # ----------------------------------------------------------------
    # def setup_bluetooth_output(self, bt_device_address):
    #     """Route audio output to a paired Bluetooth speaker/headphones.
    #     bt_device_address should be in the form 'XX:XX:XX:XX:XX:XX'.
    #     BlueZ registers the device as a PulseAudio sink automatically
    #     once connected via A2DP. This method finds that sink and sets
    #     it as the default output."""
    #     formatted = bt_device_address.replace(":", "_")
    #     try:
    #         for sink in self._parse_sinks():
    #             if formatted in sink.get("name", ""):
    #                 self._pactl("set-default-sink", sink["name"])
    #                 print(f"Bluetooth output set to: {sink['name']}")
    #                 return True
    #         print(f"ERROR: No sink found for Bluetooth device {bt_device_address}")
    #         return False
    #     except Exception as e:
    #         print(f"ERROR: Failed to set Bluetooth output: {e}")
    #         return False
