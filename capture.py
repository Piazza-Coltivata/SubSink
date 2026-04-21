"""
Manages the audio capture pipeline and the null sink for exclusive audio routing.
"""
import subprocess
import threading
import time
import collections
import audio_utils

# Open a file to capture stderr from subprocesses
# The 'w' mode means it's overwritten each time the app starts
error_log_file = open("pipeline_errors.log", "w")


def _source_name_to_card(source_name):
    """Convert 'bluez_input.10_A2_D3_EE_BB_2A.2' -> 'bluez_card.10_A2_D3_EE_BB_2A'."""
    for prefix in ("bluez_input.", "bluez_source."):
        if source_name.startswith(prefix):
            suffix = source_name[len(prefix):]
            parts = suffix.rsplit(".", 1)
            mac = parts[0] if len(parts) == 2 and parts[1].isdigit() else suffix
            return f"bluez_card.{mac}"
    return None

def log_available_ports():
    try:
        result = subprocess.run(["pw-link", "-iol"], capture_output=True, text=True)
        error_log_file.write("\n--- Available PipeWire Ports (pw-link -iol) ---\n")
        error_log_file.write(result.stdout)
        error_log_file.write("\n--- End of Ports ---\n")
        error_log_file.flush()
    except Exception as e:
        error_log_file.write(f"\nERROR: Could not run pw-link -iol: {e}\n")
        error_log_file.flush()

def check_active_links(sink_name):
    """Print the current PipeWire inputs connected to the given sink."""
    try:
        result = subprocess.run(["pw-link", "-iol"], capture_output=True, text=True, check=True)
    except Exception:
        print("LINKS: Could not read pw-link -iol")
        return

    print(f"LINKS: Current connections to {sink_name}:")
    found_any = False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{sink_name}:playback"):
            print(f"  {stripped}")
            found_any = True
        elif found_any and stripped.startswith("|<-"):
            print(f"    {stripped}")
        elif found_any and not stripped.startswith("|<-"):
            found_any = False


class CapturePipeline:
    """
    Manages a PipeWire link between a source and a sink using pw-link.
    """
    STEREO_SOURCE_PORTS = (
        ("output_FL", "output_FR"),
        ("monitor_FL", "monitor_FR"),
        ("capture_FL", "capture_FR"),
    )
    MONO_SOURCE_PORTS = ("output_MONO", "monitor_MONO", "capture_MONO")

    def __init__(self, source_name, sink_name):
        """
        Creates links between the given source and sink using the ports that exist.
        """
        self.source_name = source_name
        self.sink_name = sink_name
        self.link_ports = []
        self.created_link_ports = []
        self._running = False
        self.last_error = None

        if not self.source_name or not self.sink_name:
            self.last_error = "Missing source or sink selection."
            print(f"ERROR: {self.last_error}")
            return

        if self.source_name == self.sink_name:
            self.last_error = "Source and sink cannot be the same node."
            print(f"ERROR: {self.last_error}")
            return

        self._running = self._link_source_to_sink(self.source_name, self.sink_name)

    def _get_available_ports(self):
        try:
            result = subprocess.run(["pw-link", "-iol"], capture_output=True, text=True, check=True)
        except Exception as error:
            self.last_error = f"Could not inspect PipeWire ports: {error}"
            error_log_file.write(f"ERROR: {self.last_error}\n")
            error_log_file.flush()
            return set()

        error_log_file.write("\n--- Available PipeWire Ports (pw-link -iol) ---\n")
        error_log_file.write(result.stdout)
        error_log_file.write("\n--- End of Ports ---\n")
        error_log_file.flush()

        ports = set()
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("|") or " (" in line:
                continue
            ports.add(line)
        return ports

    def _resolve_link_ports(self, source_name, sink_name, ports):
        sink_left = f"{sink_name}:playback_FL"
        sink_right = f"{sink_name}:playback_FR"
        if sink_left not in ports or sink_right not in ports:
            self.last_error = f"Speaker ports were not found for {sink_name}."
            return []

        for left_suffix, right_suffix in self.STEREO_SOURCE_PORTS:
            source_left = f"{source_name}:{left_suffix}"
            source_right = f"{source_name}:{right_suffix}"
            if source_left in ports and source_right in ports:
                return [(source_left, sink_left), (source_right, sink_right)]

        for mono_suffix in self.MONO_SOURCE_PORTS:
            source_mono = f"{source_name}:{mono_suffix}"
            if source_mono in ports:
                return [(source_mono, sink_left), (source_mono, sink_right)]

        self.last_error = f"No compatible source ports were found for {source_name}."
        return []

    def _run_link_command(self, source_port, sink_port, disconnect=False):
        action = "unlink" if disconnect else "link"
        print(f"DEBUG: Attempting to {action} {source_port} -> {sink_port}")
        error_log_file.write(f"Attempting to {action} {source_port} -> {sink_port}\n")
        error_log_file.flush()

        command = ["pw-link"]
        if disconnect:
            command.append("-d")
        command.extend([source_port, sink_port])

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            error_log_file.write(result.stdout)
        if result.stderr:
            error_log_file.write(result.stderr)
        error_log_file.flush()

        if result.returncode == 0:
            return "created"

        stderr_text = (result.stderr or "").lower()
        if not disconnect and "file exists" in stderr_text:
            return "exists"
        if disconnect and ("no such file" in stderr_text or "not linked" in stderr_text or "does not exist" in stderr_text):
            return "missing"
        return "error"

    def _unlink_links(self, links):
        for source_port, sink_port in links:
            outcome = self._run_link_command(source_port, sink_port, disconnect=True)
            if outcome == "error":
                print(f"ERROR: pw-link -d failed for {source_port} -> {sink_port}. Check pipeline_errors.log")

    def _link_source_to_sink(self, source_name, sink_name):
        ports = self._get_available_ports()
        if not ports:
            if not self.last_error:
                self.last_error = "No PipeWire ports were available."
            return False

        requested_links = self._resolve_link_ports(source_name, sink_name, ports)
        if not requested_links:
            error_log_file.write(f"ERROR: {self.last_error}\n")
            error_log_file.flush()
            return False

        active_links = []
        created_links = []
        for source_port, sink_port in requested_links:
            outcome = self._run_link_command(source_port, sink_port)
            if outcome in ("created", "exists"):
                active_links.append((source_port, sink_port))
                if outcome == "created":
                    created_links.append((source_port, sink_port))
                continue

            self.last_error = f"Failed to link {source_port} -> {sink_port}."
            self._unlink_links(created_links)
            self.link_ports = []
            self.created_link_ports = []
            return False

        self.source_name = source_name
        self.sink_name = sink_name
        self.link_ports = active_links
        self.created_link_ports = created_links
        self.last_error = None
        return True

    def switch_source(self, source_name):
        if not source_name:
            self.last_error = "The selected phone is not currently streaming audio."
            return False
        if not self._running:
            self.last_error = "The hub is not running."
            return False
        if source_name == self.source_name:
            return True

        previous_source = self.source_name
        previous_links = list(self.link_ports)
        previous_created_links = list(self.created_link_ports)
        self._unlink_links(previous_links)
        self.link_ports = []
        self.created_link_ports = []

        if self._link_source_to_sink(source_name, self.sink_name):
            self._running = True
            return True

        failed_error = self.last_error
        if previous_links:
            self._link_source_to_sink(previous_source, self.sink_name)
            if previous_created_links and not self.created_link_ports:
                self.created_link_ports = previous_created_links
        self._running = bool(self.link_ports)
        self.last_error = failed_error
        return False

    def stop(self):
        # Keep PipeWire links alive so phones maintain their A2DP Source
        # connection (iOS/Android drop A2DP when no consumer exists).
        # The caller mutes the speaker SINK instead to silence audio.
        self._running = False
        print("Hub paused (PipeWire links kept alive).")

    def teardown(self):
        """Actually remove all PipeWire links. Call on device switch or app exit."""
        self._unlink_links(self.link_ports)
        self.link_ports = []
        self.created_link_ports = []
        self._running = False
        print("PipeWire links removed.")

    def is_running(self):
        return self._running

class NullSinkManager:
    """
    Prevents non-active Bluetooth sources from playing through the speaker
    by breaking any pw-link connections they have to the sink each watcher cycle.
    Card profiles are never touched, so all devices stay connected and ready
    to switch to instantly.
    NULL_SINK_NAME kept as a class attribute so AudioSwitch can filter it from
    the speaker list (in case a stale module exists from a previous run).
    """
    NULL_SINK_NAME = "party_mode_null_sink"

    def __init__(self):
        self._active_source_name = None
        self._active_sink_name = None
        self._watching = False
        self._watcher_thread = None

    def set_active_source(self, source_name):
        """Update the active source the watcher should protect."""
        self._active_source_name = source_name

    def start_watcher(self, active_source_name, active_sink_name):
        """Start background thread that keeps non-active BT sources muted."""
        self._active_source_name = active_source_name
        self._active_sink_name = active_sink_name
        self._watching = True
        self._watcher_thread = threading.Thread(
            target=self._watcher_loop, daemon=True, name="SourceMuteWatcher"
        )
        self._watcher_thread.start()

    def stop_watcher(self):
        self._watching = False
        if self._watcher_thread:
            self._watcher_thread.join(timeout=3)
            self._watcher_thread = None

    def _watcher_loop(self):
        import audio_utils as _au
        while self._watching:
            try:
                result = subprocess.run(
                    ["pw-link", "-iol"],
                    capture_output=True, text=True, check=True,
                )
                if not self._active_sink_name:
                    time.sleep(2)
                    continue

                sink_mac = _au._extract_mac(self._active_sink_name)

                # Walk pw-link output: find any non-active BT source that is
                # connected to a playback port of our sink and break the link.
                # Format:
                #   sink_name:playback_FL
                #     |<- source_name:output_FL
                current_sink_port = None
                for line in result.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(f"{self._active_sink_name}:playback"):
                        current_sink_port = stripped
                    elif current_sink_port and stripped.startswith("|<-"):
                        source_port = stripped[4:].strip()  # drop "|<- "
                        source_node = source_port.rsplit(":", 1)[0] if ":" in source_port else ""
                        # Skip the active source — it should stay linked.
                        if source_node == self._active_source_name:
                            current_sink_port = None
                            continue
                        # Skip the speaker's own HFP mic.
                        if sink_mac and _au._extract_mac(source_node) == sink_mac:
                            current_sink_port = None
                            continue
                        # Break any other BT source linked to our sink.
                        if source_node.startswith(("bluez_input.", "bluez_source.")):
                            print(f"WATCHER: Breaking link {source_port} -> {current_sink_port}")
                            subprocess.run(
                                ["pw-link", "-d", source_port, current_sink_port],
                                capture_output=True, text=True,
                            )
                        current_sink_port = None
                    else:
                        current_sink_port = None

                check_active_links(self._active_sink_name)
            except Exception as exc:
                print(f"WATCHER: exception: {exc}")
            time.sleep(2)

    def setup(self):
        """Cleans up any stale null sink from a previous crash, then returns True."""
        # Remove any leftover null sink module so it doesn't appear in speaker list.
        sinks_result = subprocess.run(
            ["pactl", "list", "short", "modules"],
            capture_output=True, text=True,
        )
        for line in sinks_result.stdout.splitlines():
            if self.NULL_SINK_NAME in line:
                mod_id = line.split()[0]
                subprocess.run(["pactl", "unload-module", mod_id],
                               capture_output=True, text=True)
                print(f"Removed stale null sink (module {mod_id}).")
        return True

    def teardown(self):
        """Stop the watcher. Card profiles are never touched so nothing to restore."""
        self.stop_watcher()
