"""
Capture-based audio pipeline.

Instead of using PulseAudio module-loopback to route audio at the server
level, this module reads raw PCM data from a source into Python and writes
it out to a sink.  This gives full programmatic access to the audio stream
for inspection, logging, or processing.

Uses parec/paplay subprocess pipes so no extra Python audio libraries are
needed — only PulseAudio CLI tools (available under PipeWire too).

Usage (standalone):
    sudo python3 capture_pipeline.py --source <source_name> --sink <sink_name>
    sudo python3 capture_pipeline.py --list          # list sources/sinks

    # Exclusive mode — mutes all BT devices, only selected one plays through Python:
    python3 capture_pipeline.py --exclusive
    python3 capture_pipeline.py --exclusive --sink <real_output>

Or via main.py:
    sudo python3 main.py --capture
    sudo python3 main.py --capture --capture-source <name> --capture-sink <name>
"""

import subprocess
import threading
import time
import collections
import struct
import sys
import argparse
import signal


# Default PCM format — matches PulseAudio/PipeWire defaults
DEFAULT_FORMAT = "s16le"       # signed 16-bit little-endian
DEFAULT_RATE = 48000
DEFAULT_CHANNELS = 2
BYTES_PER_SAMPLE = 2           # 16-bit = 2 bytes
CHUNK_FRAMES = 1024            # frames per read chunk
CHUNK_BYTES = CHUNK_FRAMES * DEFAULT_CHANNELS * BYTES_PER_SAMPLE  # 4096 bytes


class CaptureStats:
    """Running statistics on the captured audio stream."""

    def __init__(self):
        self.total_bytes = 0
        self.total_chunks = 0
        self.dropped_chunks = 0
        self.peak_amplitude = 0
        self.start_time = None
        self._lock = threading.Lock()

    def update(self, pcm_bytes):
        with self._lock:
            if self.start_time is None:
                self.start_time = time.time()
            self.total_bytes += len(pcm_bytes)
            self.total_chunks += 1

            # Compute peak amplitude of this chunk (16-bit signed samples)
            try:
                n_samples = len(pcm_bytes) // BYTES_PER_SAMPLE
                if n_samples > 0:
                    samples = struct.unpack(f"<{n_samples}h", pcm_bytes[:n_samples * BYTES_PER_SAMPLE])
                    chunk_peak = max(abs(s) for s in samples)
                    if chunk_peak > self.peak_amplitude:
                        self.peak_amplitude = chunk_peak
            except Exception:
                pass

    def record_drop(self):
        with self._lock:
            self.dropped_chunks += 1

    def snapshot(self):
        with self._lock:
            elapsed = time.time() - self.start_time if self.start_time else 0
            return {
                "total_bytes": self.total_bytes,
                "total_chunks": self.total_chunks,
                "dropped_chunks": self.dropped_chunks,
                "peak_amplitude": self.peak_amplitude,
                "elapsed_sec": round(elapsed, 1),
                "bitrate_kbps": round((self.total_bytes * 8 / 1000) / elapsed, 1) if elapsed > 0 else 0,
            }


class RingBuffer:
    """Thread-safe fixed-size ring buffer for PCM chunks."""

    def __init__(self, max_chunks=64):
        self._buf = collections.deque(maxlen=max_chunks)
        self._lock = threading.Lock()
        self._event = threading.Event()

    def put(self, data):
        with self._lock:
            dropped = len(self._buf) == self._buf.maxlen
            self._buf.append(data)
            self._event.set()
            return dropped

    def get(self, timeout=1.0):
        """Block until data is available or timeout."""
        if not self._event.wait(timeout=timeout):
            return None
        with self._lock:
            if self._buf:
                data = self._buf.popleft()
                if not self._buf:
                    self._event.clear()
                return data
            self._event.clear()
            return None

    def clear(self):
        with self._lock:
            self._buf.clear()
            self._event.clear()

    def __len__(self):
        with self._lock:
            return len(self._buf)


class NullSinkManager:
    """Creates a null sink (audio black hole) and redirects all Bluetooth
    sink-inputs to it so they don't play through the real output.

    The selected BT device's audio is captured through its .monitor source
    by the CapturePipeline and forwarded to the real output — giving Python
    exclusive control over which phone you hear.

    Flow:
        Phone A ──BT──> bluez_sink_A ──(moved to)──> null_sink  (silent)
        Phone B ──BT──> bluez_sink_B ──(moved to)──> null_sink  (silent)

        CapturePipeline:
            parec reads from bluez_sink_A.monitor ──> Python ──> paplay to real speaker

        User switches to Phone B:
            parec now reads from bluez_sink_B.monitor  (instant)
    """

    NULL_SINK_NAME = "bt_capture_null"
    NULL_SINK_DESC = "BT Capture Null Sink"

    def __init__(self):
        self._null_module_id = None
        self._null_sink_name = None
        self._monitor_thread = None
        self._monitoring = False
        self._pw_native = False

    def setup(self):
        """Create the null sink and return its name."""
        # Remove any stale null sink from a previous run
        self.teardown()

        # Try PipeWire-native null sink first (pipewire-pulse often refuses
        # the PulseAudio module-null-sink with "Refused")
        result = _pactl_internal(
            "load-module", "module-null-sink",
            f"sink_name={self.NULL_SINK_NAME}",
            f"sink_properties=device.description=\"{self.NULL_SINK_DESC}\""
        )

        if result.returncode != 0:
            print(f"module-null-sink failed ({result.stderr.strip()}), trying pw-cli...")
            # Fallback: create a null sink via pw-loopback or pw-cli
            result = subprocess.run(
                [
                    "pw-cli", "create-node", "adapter",
                    "{factory.name=support.null-audio-sink "
                    f"node.name={self.NULL_SINK_NAME} "
                    f"node.description=\"{self.NULL_SINK_DESC}\" "
                    "media.class=Audio/Sink "
                    "audio.position=[FL FR] "
                    "monitor.channel-volumes=true}"
                ],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"ERROR: Failed to create null sink via pw-cli: {result.stderr.strip()}")
                print("Hint: ensure pipewire is running, or install pulseaudio module-null-sink")
                return None
            # pw-cli returns the object id
            self._null_module_id = result.stdout.strip().split()[-1] if result.stdout.strip() else None
            self._pw_native = True
        else:
            self._null_module_id = result.stdout.strip()
            self._pw_native = False

        self._null_sink_name = self.NULL_SINK_NAME
        print(f"Null sink created: {self._null_sink_name} (id {self._null_module_id})")
        return self._null_sink_name

    def teardown(self):
        """Remove the null sink."""
        if self._null_module_id:
            if self._pw_native:
                subprocess.run(["pw-cli", "destroy", self._null_module_id],
                               capture_output=True, text=True)
            else:
                _pactl_internal("unload-module", self._null_module_id)
            print(f"Null sink removed (id {self._null_module_id})")
            self._null_module_id = None
            self._null_sink_name = None
            self._pw_native = False

    def move_bt_streams_to_null(self):
        """Move all Bluetooth sink-inputs to the null sink so they go silent."""
        if not self._null_sink_name:
            return 0
        moved = 0
        result = _pactl_internal("list", "short", "sink-inputs")
        if result.returncode != 0:
            return 0
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                stream_id = parts[0]
                # Move every sink-input to the null sink
                r = _pactl_internal("move-sink-input", stream_id, self._null_sink_name)
                if r.returncode == 0:
                    moved += 1
        if moved:
            print(f"Moved {moved} stream(s) to null sink")
        return moved

    def get_bt_monitor_sources(self):
        """Return a list of .monitor sources belonging to Bluetooth sinks."""
        sources = list_sources()
        return [s for s in sources if "bluez" in s.get("name", "") and ".monitor" in s.get("name", "")]

    def start_monitoring(self, interval=2):
        """Background thread that watches for new BT connections and moves
        their sink-inputs to the null sink automatically."""
        if self._monitoring:
            return
        self._monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, args=(interval,), daemon=True)
        self._monitor_thread.start()
        print(f"BT stream monitor started (checking every {interval}s)")

    def stop_monitoring(self):
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
            self._monitor_thread = None

    def _monitor_loop(self, interval):
        while self._monitoring:
            self.move_bt_streams_to_null()
            time.sleep(interval)


def _pactl_internal(*args):
    """Internal pactl wrapper used by NullSinkManager (avoids circular dep with module-level _pactl)."""
    return subprocess.run(["pactl"] + list(args), capture_output=True, text=True)


class CapturePipeline:
    """Captures PCM from a PulseAudio source and plays it to a sink,
    with the audio data passing through Python."""

    def __init__(self, source_name, sink_name,
                 rate=DEFAULT_RATE, channels=DEFAULT_CHANNELS,
                 fmt=DEFAULT_FORMAT, buffer_chunks=64):
        self.source_name = source_name
        self.sink_name = sink_name
        self.rate = rate
        self.channels = channels
        self.fmt = fmt

        self.buffer = RingBuffer(max_chunks=buffer_chunks)
        self.stats = CaptureStats()

        self._running = False
        self._record_proc = None
        self._play_proc = None
        self._reader_thread = None
        self._writer_thread = None
        self._stats_thread = None

        self.chunk_bytes = CHUNK_FRAMES * channels * BYTES_PER_SAMPLE

        # Optional callback — called with each PCM chunk (bytes).
        # Runs on the reader thread, so keep it fast.
        self.on_chunk = None

    def start(self, print_stats=True):
        """Start the capture pipeline."""
        if self._running:
            print("Pipeline already running.")
            return

        self._running = True
        self.buffer.clear()

        # Launch parec (record from source)
        self._record_proc = subprocess.Popen(
            [
                "parec",
                "--format", self.fmt,
                "--rate", str(self.rate),
                "--channels", str(self.channels),
                "-d", self.source_name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Launch paplay (play to sink) reading from stdin
        self._play_proc = subprocess.Popen(
            [
                "paplay",
                "--format", self.fmt,
                "--rate", str(self.rate),
                "--channels", str(self.channels),
                "-d", self.sink_name,
                "--raw",
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._reader_thread.start()
        self._writer_thread.start()

        if print_stats:
            self._stats_thread = threading.Thread(target=self._stats_loop, daemon=True)
            self._stats_thread.start()

        print(f"Capture pipeline started:")
        print(f"  Source:   {self.source_name}")
        print(f"  Sink:     {self.sink_name}")
        print(f"  Format:   {self.fmt} / {self.rate} Hz / {self.channels} ch")
        print(f"  Buffer:   {self.buffer._buf.maxlen} chunks ({self.buffer._buf.maxlen * self.chunk_bytes} bytes)")
        print()

    def stop(self):
        """Stop the capture pipeline and clean up."""
        if not self._running:
            return
        self._running = False

        if self._record_proc:
            self._record_proc.terminate()
            try:
                self._record_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._record_proc.kill()
            self._record_proc = None

        if self._play_proc:
            try:
                self._play_proc.stdin.close()
            except Exception:
                pass
            self._play_proc.terminate()
            try:
                self._play_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._play_proc.kill()
            self._play_proc = None

        self.buffer.clear()
        print("\nCapture pipeline stopped.")
        snap = self.stats.snapshot()
        print(f"  Total: {snap['total_bytes']} bytes, {snap['total_chunks']} chunks, "
              f"{snap['dropped_chunks']} dropped, {snap['elapsed_sec']}s")

    def switch_source(self, new_source):
        """Switch to a new source by restarting the record side."""
        print(f"Switching source: {self.source_name} -> {new_source}")
        old_source = self.source_name
        self.source_name = new_source

        # Kill old recorder
        if self._record_proc:
            self._record_proc.terminate()
            try:
                self._record_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._record_proc.kill()

        # Start new recorder
        self._record_proc = subprocess.Popen(
            [
                "parec",
                "--format", self.fmt,
                "--rate", str(self.rate),
                "--channels", str(self.channels),
                "-d", new_source,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        print(f"Source switched to: {new_source}")

    def switch_sink(self, new_sink):
        """Switch to a new sink by restarting the play side."""
        print(f"Switching sink: {self.sink_name} -> {new_sink}")
        self.sink_name = new_sink

        # Kill old player
        if self._play_proc:
            try:
                self._play_proc.stdin.close()
            except Exception:
                pass
            self._play_proc.terminate()
            try:
                self._play_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._play_proc.kill()

        # Start new player
        self._play_proc = subprocess.Popen(
            [
                "paplay",
                "--format", self.fmt,
                "--rate", str(self.rate),
                "--channels", str(self.channels),
                "-d", new_sink,
                "--raw",
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        print(f"Sink switched to: {new_sink}")

    # --- internal loops ---

    def _reader_loop(self):
        """Read PCM chunks from parec and put them into the ring buffer."""
        while self._running:
            proc = self._record_proc
            if proc is None or proc.poll() is not None:
                # Process died — wait briefly and check if we're restarting
                time.sleep(0.1)
                continue
            try:
                data = proc.stdout.read(self.chunk_bytes)
                if not data:
                    time.sleep(0.05)
                    continue
                self.stats.update(data)
                if self.on_chunk:
                    self.on_chunk(data)
                dropped = self.buffer.put(data)
                if dropped:
                    self.stats.record_drop()
            except Exception as e:
                if self._running:
                    print(f"Reader error: {e}")
                break

    def _writer_loop(self):
        """Pull chunks from the ring buffer and write them to paplay."""
        while self._running:
            chunk = self.buffer.get(timeout=0.5)
            if chunk is None:
                continue
            proc = self._play_proc
            if proc is None or proc.poll() is not None:
                time.sleep(0.1)
                continue
            try:
                proc.stdin.write(chunk)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                if self._running:
                    print(f"Writer error: {e}")
                break

    def _stats_loop(self):
        """Print periodic stats to console."""
        while self._running:
            time.sleep(5)
            if not self._running:
                break
            snap = self.stats.snapshot()
            buf_fill = len(self.buffer)
            peak_pct = round(snap["peak_amplitude"] / 32768 * 100, 1) if snap["peak_amplitude"] else 0
            print(f"[capture] {snap['elapsed_sec']}s | "
                  f"{snap['bitrate_kbps']} kbps | "
                  f"buf {buf_fill}/{self.buffer._buf.maxlen} | "
                  f"peak {peak_pct}% | "
                  f"dropped {snap['dropped_chunks']}")


# ---------------------------------------------------------------------------
# Helpers for listing devices (so this file works standalone)
# ---------------------------------------------------------------------------

def _pactl(*args):
    result = subprocess.run(["pactl"] + list(args), capture_output=True, text=True)
    return result


def list_sources():
    result = _pactl("list", "sources")
    if result.returncode != 0:
        return []
    sources = []
    current = {}
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Source #"):
            if current:
                sources.append(current)
            current = {"index": stripped.split("#")[1]}
        elif stripped.startswith("Name:"):
            current["name"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Description:"):
            current["description"] = stripped.split(":", 1)[1].strip()
    if current:
        sources.append(current)
    return sources


def list_sinks():
    result = _pactl("list", "sinks")
    if result.returncode != 0:
        return []
    sinks = []
    current = {}
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Sink #"):
            if current:
                sinks.append(current)
            current = {"index": stripped.split("#")[1]}
        elif stripped.startswith("Name:"):
            current["name"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Description:"):
            current["description"] = stripped.split(":", 1)[1].strip()
    if current:
        sinks.append(current)
    return sinks


def interactive_select(items, label):
    """Let user pick from a list by number."""
    if not items:
        print(f"No {label} available.")
        sys.exit(1)
    print(f"\nAvailable {label}:")
    for i, item in enumerate(items):
        print(f"  [{i}] {item.get('description', item['name'])}  ({item['name']})")
    while True:
        try:
            choice = int(input(f"Select {label} [0-{len(items)-1}]: "))
            if 0 <= choice < len(items):
                return items[choice]["name"]
        except (ValueError, EOFError):
            pass
        print("Invalid choice, try again.")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Capture-mode audio pipeline")
    parser.add_argument("--source", type=str, default=None,
                        help="PulseAudio source name (or interactive if omitted)")
    parser.add_argument("--sink", type=str, default=None,
                        help="PulseAudio sink name (or interactive if omitted)")
    parser.add_argument("--list", action="store_true",
                        help="Just list sources and sinks, then exit")
    parser.add_argument("--exclusive", action="store_true",
                        help="Exclusive mode: mute all BT devices, only forward selected one through Python")
    parser.add_argument("--buffer-chunks", type=int, default=64,
                        help="Ring buffer size in chunks (default: 64)")
    parser.add_argument("--rate", type=int, default=DEFAULT_RATE,
                        help=f"Sample rate (default: {DEFAULT_RATE})")
    parser.add_argument("--channels", type=int, default=DEFAULT_CHANNELS,
                        help=f"Channel count (default: {DEFAULT_CHANNELS})")
    args = parser.parse_args()

    if args.list:
        print("Sources:")
        for s in list_sources():
            print(f"  {s.get('description', '')}  ->  {s['name']}")
        print("\nSinks:")
        for s in list_sinks():
            print(f"  {s.get('description', '')}  ->  {s['name']}")
        sys.exit(0)

    null_mgr = None

    if args.exclusive:
        print("=" * 50)
        print("EXCLUSIVE CAPTURE MODE")
        print("=" * 50)
        print()
        print("All BT audio will be silenced. Only the device you")
        print("select will play through Python -> your real output.")
        print()

        null_mgr = NullSinkManager()
        null_sink_name = null_mgr.setup()
        if not null_sink_name:
            print("ERROR: Could not create null sink. Exiting.")
            sys.exit(1)

        # Move existing BT streams to null
        null_mgr.move_bt_streams_to_null()

        # Start watching for new BT connections
        null_mgr.start_monitoring(interval=2)

        # Pick source from BT monitors only
        print("\nWaiting for Bluetooth devices...")
        print("Connect your phones now. They will appear below.")
        print("(Press Ctrl+C to cancel)\n")

        bt_sources = null_mgr.get_bt_monitor_sources()
        while not bt_sources:
            try:
                time.sleep(2)
                bt_sources = null_mgr.get_bt_monitor_sources()
                if bt_sources:
                    break
                # Also show any regular sources that appeared
                all_sources = list_sources()
                if len(all_sources) > 0:
                    print(f"  {len(all_sources)} source(s) available, {len(bt_sources)} are BT monitors...")
            except KeyboardInterrupt:
                null_mgr.stop_monitoring()
                null_mgr.teardown()
                sys.exit(0)

        source = interactive_select(bt_sources, "BT source")

        # For sink, pick the REAL output (exclude null sink)
        real_sinks = [s for s in list_sinks() if null_sink_name not in s.get("name", "")]
        sink = args.sink or interactive_select(real_sinks, "output sink")
    else:
        source = args.source or interactive_select(list_sources(), "source")
        sink = args.sink or interactive_select(list_sinks(), "sink")

    pipeline = CapturePipeline(
        source_name=source,
        sink_name=sink,
        rate=args.rate,
        channels=args.channels,
        buffer_chunks=args.buffer_chunks,
    )

    def on_exit(signum, frame):
        pipeline.stop()
        if null_mgr:
            null_mgr.stop_monitoring()
            null_mgr.teardown()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    pipeline.start(print_stats=True)

    if args.exclusive:
        print("=" * 50)
        print("EXCLUSIVE MODE ACTIVE")
        print(f"  Capturing: {source}")
        print(f"  Output:    {sink}")
        print()
        print("To switch to a different phone, press Ctrl+C and restart,")
        print("or use --gui mode for live switching.")
        print("=" * 50)

    # Block main thread until stopped
    try:
        while pipeline._running:
            time.sleep(1)
    except KeyboardInterrupt:
        pipeline.stop()
        if null_mgr:
            null_mgr.stop_monitoring()
            null_mgr.teardown()
