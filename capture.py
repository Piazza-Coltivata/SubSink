"""
Manages the audio capture pipeline and the null sink for exclusive audio routing.
"""
import subprocess
import threading
import time
import collections

class CapturePipeline:
    """
    Captures PCM audio from a PulseAudio source and plays it to a sink,
    passing the data through Python. This allows for exclusive routing.
    """
    def __init__(self, source_name, sink_name, rate=48000, fmt="s16le", channels=2):
        self.source_name = source_name
        self.sink_name = sink_name
        self.rate = rate
        self.channels = channels
        self.fmt = fmt
        self._running = False
        self._record_proc = None
        self._play_proc = None
        self._reader_thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._start_procs()
        print(f"Capture started: {self.source_name} -> {self.sink_name}")

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._stop_procs()
        print("Capture stopped.")

    def switch_source(self, new_source_name):
        print(f"Switching capture source to: {new_source_name}")
        self._stop_procs()
        self.source_name = new_source_name
        self._start_procs()
        print("Capture source switched.")

    def _start_procs(self):
        self._record_proc = subprocess.Popen(
            ["parec", "-d", self.source_name, "--format=s16le"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        self._play_proc = subprocess.Popen(
            ["paplay", "-d", self.sink_name, "--format=s16le"],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _stop_procs(self):
        if self._record_proc:
            self._record_proc.terminate()
        if self._play_proc:
            self._play_proc.terminate()
        if self._reader_thread:
            self._reader_thread.join(timeout=1)

    def _reader_loop(self):
        while self._running:
            try:
                chunk = self._record_proc.stdout.read(4096)
                if not chunk:
                    break
                self._play_proc.stdin.write(chunk)
            except (IOError, AttributeError):
                break

class NullSinkManager:
    """
    Creates and manages a 'null' sink to act as an audio black hole,
    allowing for one stream to be selectively captured while others are silenced.
    """
    NULL_SINK_NAME = "party_mode_null_sink"

    def __init__(self):
        self._module_id = None
        self._monitor_thread = None
        self._monitoring = False

    def setup(self):
        """Creates the null sink."""
        self.teardown() # Clean up any old instance
        result = subprocess.run(
            ["pactl", "load-module", "module-null-sink", f"sink_name={self.NULL_SINK_NAME}"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            self._module_id = result.stdout.strip()
            print(f"Null sink created (module {self._module_id}).")
            self.start_monitoring()
            return True
        print(f"Error creating null sink: {result.stderr}")
        return False

    def teardown(self):
        """Removes the null sink."""
        self.stop_monitoring()
        if self._module_id:
            subprocess.run(["pactl", "unload-module", self._module_id])
            print("Null sink removed.")
            self._module_id = None
        # Also find and remove by name if it's stuck
        sinks_result = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True)
        for line in sinks_result.stdout.splitlines():
            if self.NULL_SINK_NAME in line:
                mod_id = line.split()[0]
                subprocess.run(["pactl", "unload-module", mod_id])
                print(f"Found and removed stale null sink (module {mod_id}).")


    def start_monitoring(self):
        """Starts a thread to automatically move all BT streams to the null sink."""
        self._monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def stop_monitoring(self):
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2)

    def _monitor_loop(self):
        """Periodically moves any new Bluetooth audio streams to the null sink."""
        while self._monitoring:
            try:
                inputs_result = subprocess.run(["pactl", "list", "short", "sink-inputs"], capture_output=True, text=True, check=True)
                for line in inputs_result.stdout.splitlines():
                    parts = line.split()
                    stream_id = parts[0]
                    # Check if it's a bluetooth stream before moving
                    props_result = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True, text=True, check=True)
                    if f"Sink Input #{stream_id}" in props_result.stdout and "bluez" in props_result.stdout:
                         subprocess.run(["pactl", "move-sink-input", stream_id, self.NULL_SINK_NAME])
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass # pactl might fail if no streams exist
            time.sleep(2)
