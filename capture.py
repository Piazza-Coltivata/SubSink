"""
Manages the audio capture pipeline and source muting for exclusive audio routing.
"""
import re
import subprocess
import threading
import time
import collections
import audio_utils

# Open a file to capture stderr from subprocesses.
# AudioSwitch clears the session log explicitly; append mode avoids later writes
# from this module clobbering activation diagnostics.
error_log_file = open("pipeline_errors.log", "a")

SHARED_STEREO_SOURCE_PORTS = (
    ("output_FL", "output_FR"),
    ("monitor_FL", "monitor_FR"),
    ("capture_FL", "capture_FR"),
)
SHARED_MONO_SOURCE_PORTS = ("output_MONO", "monitor_MONO", "capture_MONO")
WPCTL_STATUS_LINE = re.compile(r"^\s*[^\d]*(?P<id>\d+)\.\s+(?P<name>[^\s]+)")


def _inspect_pw_link_graph():
    """Return PipeWire ports and current connections from pw-link -iol."""
    try:
        result = subprocess.run(
            ["pw-link", "-iol"],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception as error:
        message = f"PW_GRAPH: Could not inspect pw-link graph: {error}"
        print(message)
        error_log_file.write(f"{message}\n")
        error_log_file.flush()
        return {
            "ports": set(),
            "incoming": collections.defaultdict(list),
            "outgoing": collections.defaultdict(list),
            "raw_output": "",
        }

    ports = set()
    incoming = collections.defaultdict(list)
    outgoing = collections.defaultdict(list)
    current_port = None

    for raw_line in result.stdout.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            current_port = None
            continue
        if stripped.startswith("|<-") and current_port:
            source_port = stripped[4:].strip()
            incoming[current_port].append(source_port)
            outgoing[source_port].append(current_port)
            continue
        if stripped.startswith("|->") and current_port:
            sink_port = stripped[4:].strip()
            outgoing[current_port].append(sink_port)
            incoming[sink_port].append(current_port)
            continue
        if stripped.startswith("|") or " (" in stripped or ":" not in stripped:
            current_port = None
            continue

        current_port = stripped
        ports.add(stripped)

    return {
        "ports": ports,
        "incoming": incoming,
        "outgoing": outgoing,
        "raw_output": result.stdout,
    }


def _source_name_to_card(source_name):
    """Convert 'bluez_input.10_A2_D3_EE_BB_2A.2' -> 'bluez_card.10_A2_D3_EE_BB_2A'."""
    for prefix in ("bluez_input.", "bluez_source."):
        if source_name.startswith(prefix):
            suffix = source_name[len(prefix):]
            parts = suffix.rsplit(".", 1)
            mac = parts[0] if len(parts) == 2 and parts[1].isdigit() else suffix
            return f"bluez_card.{mac}"
    return None


def _append_debug_line(message):
    """Write a single diagnostic line to the shared session log."""
    error_log_file.write(f"{message}\n")
    error_log_file.flush()

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

def unlink_non_active_bt_sources(active_source_name, sink_name):
    """Break any BT source links to the sink that do not belong to the active source."""
    if not sink_name:
        return []

    graph = _inspect_pw_link_graph()
    if not graph["ports"]:
        return []

    sink_mac = audio_utils._extract_mac(sink_name)
    removed_links = []

    for sink_port in (f"{sink_name}:playback_FL", f"{sink_name}:playback_FR"):
        for source_port in graph["incoming"].get(sink_port, []):
            source_node = source_port.rsplit(":", 1)[0] if ":" in source_port else ""

            if source_node == active_source_name:
                continue
            if sink_mac and audio_utils._extract_mac(source_node) == sink_mac:
                continue
            if source_node.startswith(("bluez_input.", "bluez_source.")):
                message = f"WATCHER: Breaking link {source_port} -> {sink_port}"
                print(message)
                error_log_file.write(f"{message}\n")
                outcome = _run_pw_link_action(source_port, sink_port, disconnect=True)
                removed_links.append((source_port, sink_port, outcome))
                error_log_file.flush()

    return removed_links

def _get_pw_ports():
    """Return the current PipeWire port names visible through pw-link."""
    return _inspect_pw_link_graph()["ports"]

def _resolve_source_sink_links(source_name, sink_name, ports):
    """Resolve matching source and sink ports for stereo or mono audio links."""
    sink_left = f"{sink_name}:playback_FL"
    sink_right = f"{sink_name}:playback_FR"
    if sink_left not in ports or sink_right not in ports:
        return []

    for left_suffix, right_suffix in SHARED_STEREO_SOURCE_PORTS:
        source_left = f"{source_name}:{left_suffix}"
        source_right = f"{source_name}:{right_suffix}"
        if source_left in ports and source_right in ports:
            return [(source_left, sink_left), (source_right, sink_right)]

    for mono_suffix in SHARED_MONO_SOURCE_PORTS:
        source_mono = f"{source_name}:{mono_suffix}"
        if source_mono in ports:
            return [(source_mono, sink_left), (source_mono, sink_right)]

    return []

def _run_pw_link_action(source_port, sink_port, disconnect=False):
    """Run a pw-link link or unlink command and log the result."""
    action = "unlink" if disconnect else "link"
    message = f"ROUTER: Attempting to {action} {source_port} -> {sink_port}"
    print(message)
    error_log_file.write(f"{message}\n")
    result = subprocess.run(
        ["pw-link", "-d", source_port, sink_port] if disconnect else ["pw-link", source_port, sink_port],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        error_log_file.write(result.stdout)
    if result.stderr:
        error_log_file.write(result.stderr)
    error_log_file.flush()

    stderr_text = (result.stderr or "").lower()
    if result.returncode == 0:
        return "ok"
    if not disconnect and "file exists" in stderr_text:
        return "exists"
    if disconnect and ("no such file" in stderr_text or "not linked" in stderr_text or "does not exist" in stderr_text):
        return "missing"
    return "error"

def _resolve_wpctl_node_id(node_name):
    """Resolve a PipeWire node.name to its current wpctl numeric ID."""
    if not node_name:
        return None

    try:
        result = subprocess.run(
            ["wpctl", "status", "-n"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as error:
        message = f"ROUTER: Could not query wpctl status for {node_name}: {error}"
        print(message)
        error_log_file.write(f"{message}\n")
        error_log_file.flush()
        return None

    if result.returncode != 0:
        message = (
            f"ROUTER: wpctl status failed while resolving {node_name}: "
            f"{result.stderr.strip()}"
        )
        print(message)
        error_log_file.write(f"{message}\n")
        error_log_file.flush()
        return None

    for raw_line in result.stdout.splitlines():
        match = WPCTL_STATUS_LINE.match(raw_line)
        if match and match.group("name") == node_name:
            return match.group("id")

    return None

def _set_source_mute(source_name, muted):
    """Mute or unmute a source node without removing its PipeWire links."""
    if not source_name:
        return "missing"

    mute_value = "1" if muted else "0"
    action = "mute" if muted else "unmute"
    wpctl_id = _resolve_wpctl_node_id(source_name)

    if not wpctl_id and source_name.startswith(("bluez_input.", "bluez_source.")):
        message = f"ROUTER: No live wpctl node was found for {source_name}"
        print(message)
        error_log_file.write(f"{message}\n")
        error_log_file.flush()
        return "missing"

    commands = []
    if wpctl_id:
        commands.append((
            ["wpctl", "set-mute", wpctl_id, mute_value],
            f"ROUTER: Attempting to {action} {source_name} via wpctl id {wpctl_id}",
        ))

    commands.append((
        ["pactl", "set-source-mute", source_name, mute_value],
        f"ROUTER: Attempting to {action} {source_name} via pactl",
    ))

    last_missing = False
    for command, message in commands:
        print(message)
        error_log_file.write(f"{message}\n")

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

        stderr_text = (result.stderr or "").lower()
        if result.returncode == 0:
            return "ok"
        if (
            "no such entity" in stderr_text
            or "failure: input/output error" in stderr_text
            or "object not found" in stderr_text
            or "unknown id" in stderr_text
        ):
            last_missing = True
            continue
        return "error"

    return "missing" if last_missing else "error"


def _set_source_mute_if_needed(source_name, muted, source_mute_states=None):
    """Avoid repeating the same mute command on stable source nodes."""
    if source_mute_states is not None and source_mute_states.get(source_name) is muted:
        return "unchanged"

    result = _set_source_mute(source_name, muted)
    if source_mute_states is not None:
        if result == "ok":
            source_mute_states[source_name] = muted
        elif result == "missing":
            source_mute_states.pop(source_name, None)
    return result


def _release_source_mute_if_needed(source_name, source_mute_states=None):
    """Unmute a source only when the app previously muted it."""
    if source_mute_states is None or source_mute_states.get(source_name) is not True:
        return "unchanged"
    return _set_source_mute_if_needed(source_name, False, source_mute_states)

def _unload_named_null_sink_modules(sink_name):
    """Remove leftover null-sink modules created by earlier app versions."""
    unloaded_modules = []
    result = subprocess.run(
        ["pactl", "list", "short", "modules"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return unloaded_modules

    for line in result.stdout.splitlines():
        if sink_name not in line:
            continue
        module_id = line.split()[0]
        unload_result = subprocess.run(
            ["pactl", "unload-module", module_id],
            capture_output=True,
            text=True,
        )
        if unload_result.returncode == 0:
            unloaded_modules.append(module_id)
            message = f"NULL_SINK: removed stale {sink_name} module {module_id}"
            print(message)
            error_log_file.write(f"{message}\n")
            error_log_file.flush()

    return unloaded_modules

def ensure_source_linked_to_sink(source_name, sink_name):
    """Ensure a Bluetooth source stays connected to a sink such as the null sink."""
    graph = _inspect_pw_link_graph()
    ports = graph["ports"]
    if not ports:
        return []

    link_ports = _resolve_source_sink_links(source_name, sink_name, ports)
    if not link_ports:
        return []

    outcomes = []
    for source_port, sink_port in link_ports:
        if sink_port in graph["outgoing"].get(source_port, []):
            outcomes.append((source_port, sink_port, "exists"))
            continue
        outcomes.append((source_port, sink_port, _run_pw_link_action(source_port, sink_port)))
    return outcomes


def _source_has_links_to_sink(source_name, sink_name, graph=None):
    """Return True when a source currently has any links to the given sink."""
    if graph is None:
        graph = _inspect_pw_link_graph()

    ports = graph["ports"]
    if not ports:
        return False

    link_ports = _resolve_source_sink_links(source_name, sink_name, ports)
    if not link_ports:
        return False

    return any(
        sink_port in graph["outgoing"].get(source_port, [])
        for source_port, sink_port in link_ports
    )

def disconnect_source_from_sink(source_name, sink_name):
    """Remove a source's links to a sink when they currently exist."""
    graph = _inspect_pw_link_graph()
    ports = graph["ports"]
    if not ports:
        return []

    link_ports = _resolve_source_sink_links(source_name, sink_name, ports)
    if not link_ports:
        return []

    outcomes = []
    for source_port, sink_port in link_ports:
        if sink_port not in graph["outgoing"].get(source_port, []):
            outcomes.append((source_port, sink_port, "missing"))
            continue
        outcomes.append((source_port, sink_port, _run_pw_link_action(source_port, sink_port, disconnect=True)))
    return outcomes

def route_non_active_bt_sources(
    active_source_name,
    speaker_sink_name,
    silent_sink_name,
    protected_device_mac=None,
    source_grace_deadlines=None,
    grace_period_seconds=0,
    source_mute_states=None,
    held_source_macs=None,
):
    """Keep only the active Bluetooth source linked to the speaker."""
    del silent_sink_name

    speaker_mac = audio_utils._extract_mac(speaker_sink_name)
    protected_device_mac = audio_utils._normalize_mac(protected_device_mac or "")
    now = time.time()
    graph = _inspect_pw_link_graph()
    speaker_links = []
    mute_changes = []
    removed_links = []
    observed_source_macs = set()
    observed_source_names = set()

    for source_name in audio_utils._list_pipewire_bluez_input_nodes():
        if speaker_mac and audio_utils._extract_mac(source_name) == speaker_mac:
            continue

        observed_source_names.add(source_name)
        source_has_speaker_links = _source_has_links_to_sink(
            source_name,
            speaker_sink_name,
            graph=graph,
        )

        source_mac = audio_utils._normalize_mac(audio_utils._extract_mac(source_name))
        if source_mac:
            observed_source_macs.add(source_mac)
            if source_grace_deadlines is not None and grace_period_seconds > 0:
                source_grace_deadlines.setdefault(source_mac, now + grace_period_seconds)

        if source_name == active_source_name:
            if source_mac and source_grace_deadlines is not None and grace_period_seconds > 0:
                source_grace_deadlines[source_mac] = now + grace_period_seconds
            if source_mac and held_source_macs is not None:
                held_source_macs.discard(source_mac)
            speaker_links.extend(ensure_source_linked_to_sink(source_name, speaker_sink_name))
            mute_result = _set_source_mute_if_needed(source_name, False, source_mute_states)
            if mute_result != "unchanged":
                mute_changes.append((source_name, "unmute", mute_result))
            continue

        if source_mac and held_source_macs is not None and source_mac in held_source_macs:
            mute_result = _set_source_mute_if_needed(source_name, True, source_mute_states)
            if mute_result != "unchanged":
                mute_changes.append((source_name, "hold-mute", mute_result))
            if source_has_speaker_links:
                removed_links.extend(disconnect_source_from_sink(source_name, speaker_sink_name))
            continue

        if protected_device_mac and source_mac == protected_device_mac:
            if source_mac and source_grace_deadlines is not None and grace_period_seconds > 0:
                source_grace_deadlines[source_mac] = now + grace_period_seconds
            if not active_source_name:
                mute_result = _release_source_mute_if_needed(source_name, source_mute_states)
                if mute_result != "unchanged":
                    mute_changes.append((source_name, "protect-pending-unmute", mute_result))
                continue
            if source_has_speaker_links:
                mute_result = _set_source_mute_if_needed(source_name, True, source_mute_states)
                if mute_result != "unchanged":
                    mute_changes.append((source_name, "protect-mute", mute_result))
                removed_links.extend(disconnect_source_from_sink(source_name, speaker_sink_name))
            continue

        grace_until = 0
        if source_mac and source_grace_deadlines is not None:
            grace_until = source_grace_deadlines.get(source_mac, 0)
        if grace_until > now:
            if source_has_speaker_links:
                mute_result = _set_source_mute_if_needed(source_name, True, source_mute_states)
                if mute_result != "unchanged":
                    mute_changes.append((source_name, "grace-mute", mute_result))
                removed_links.extend(disconnect_source_from_sink(source_name, speaker_sink_name))
            continue

        if source_has_speaker_links:
            mute_result = _set_source_mute_if_needed(source_name, True, source_mute_states)
            if mute_result != "unchanged":
                mute_changes.append((source_name, "mute", mute_result))
            removed_links.extend(disconnect_source_from_sink(source_name, speaker_sink_name))

    if source_grace_deadlines is not None:
        for source_mac, deadline in list(source_grace_deadlines.items()):
            if deadline <= now and source_mac not in observed_source_macs:
                source_grace_deadlines.pop(source_mac, None)

    if source_mute_states is not None:
        for source_name in list(source_mute_states.keys()):
            if source_name not in observed_source_names:
                source_mute_states.pop(source_name, None)

    return {
        "speaker_links": speaker_links,
        "mute_changes": mute_changes,
        "removed_links": removed_links,
    }


def _changes_include_meaningful_activity(changes):
    """Return True when watcher output reflects a real route or mute transition."""
    if any(outcome != "exists" for _, _, outcome in changes.get("speaker_links", [])):
        return True
    if changes.get("mute_changes"):
        return True
    if any(outcome != "missing" for _, _, outcome in changes.get("removed_links", [])):
        return True
    return False

def restore_bt_source_state(speaker_sink_name=None):
    """Return Bluetooth input nodes to a neutral state on app shutdown."""
    speaker_mac = audio_utils._normalize_mac(audio_utils._extract_mac(speaker_sink_name))
    excluded_macs = {
        audio_utils._normalize_mac(audio_utils._extract_mac(sink.get("name", "")))
        for sink in audio_utils.list_devices("sinks")
        if sink.get("name", "").startswith("bluez_output.")
    }
    if speaker_mac:
        excluded_macs.add(speaker_mac)
    unmuted_sources = []
    removed_links = []

    for source_name in audio_utils._list_pipewire_bluez_input_nodes():
        source_mac = audio_utils._normalize_mac(audio_utils._extract_mac(source_name))
        if source_mac in excluded_macs:
            continue

        unmuted_sources.append((source_name, _set_source_mute(source_name, False)))
        if speaker_sink_name:
            removed_links.extend(disconnect_source_from_sink(source_name, speaker_sink_name))

    return {
        "unmuted_sources": unmuted_sources,
        "removed_links": removed_links,
    }


def cleanup_stale_bt_routes():
    """Clear leftover Bluetooth source routes when a new app session starts cold."""
    speaker_sink_names = [
        sink.get("name")
        for sink in audio_utils.list_devices("sinks")
        if sink.get("name", "").startswith("bluez_output.")
    ]

    summaries = []
    if not speaker_sink_names:
        summary = restore_bt_source_state(None)
        summaries.append((None, summary))
        return summaries

    for sink_name in speaker_sink_names:
        summary = restore_bt_source_state(sink_name)
        if summary["unmuted_sources"] or summary["removed_links"]:
            message = f"STARTUP_CLEANUP: sink={sink_name} cleanup={summary}"
            print(message)
            error_log_file.write(f"{message}\n")
            error_log_file.flush()
        summaries.append((sink_name, summary))

    return summaries


class CapturePipeline:
    """
    Manages a PipeWire link between a source and a sink using pw-link.
    """
    STEREO_SOURCE_PORTS = SHARED_STEREO_SOURCE_PORTS
    MONO_SOURCE_PORTS = SHARED_MONO_SOURCE_PORTS

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

        if self._link_source_to_sink(source_name, self.sink_name):
            if previous_links:
                self._unlink_links(previous_links)
            self._running = True
            return True

        failed_error = self.last_error
        self.source_name = previous_source
        self.link_ports = previous_links
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
    Legacy wrapper around the background exclusivity watcher.
    Inactive Bluetooth sources are muted and removed from the real speaker path
    so only the active source stays linked during a handoff.
    NULL_SINK_NAME stays as a class attribute so AudioSwitch can still filter a
    stale sink from the speaker list if an older run left one behind.
    """
    NULL_SINK_NAME = "party_mode_null_sink"
    SOURCE_GRACE_SECONDS = 12
    WATCHER_POLL_SECONDS = 0.5

    def __init__(self):
        self._active_source_name = None
        self._active_sink_name = None
        self._protected_device_mac = None
        self._held_source_macs = set()
        self._source_grace_deadlines = {}
        self._source_mute_states = {}
        self._watching = False
        self._watcher_generation = 0
        self._watcher_thread = None
        self._null_sink_module_id = None

    def _log_watcher_event(self, event, **details):
        parts = [f"{key}={value}" for key, value in details.items()]
        suffix = f" {' '.join(parts)}" if parts else ""
        _append_debug_line(f"WATCHER_{event}:{suffix}")

    def set_active_source(self, source_name):
        """Update the active source the watcher should protect."""
        previous_source = self._active_source_name
        self._active_source_name = source_name
        source_mac = audio_utils._normalize_mac(audio_utils._extract_mac(source_name))
        if source_mac:
            self._held_source_macs.discard(source_mac)
            self._source_grace_deadlines[source_mac] = time.time() + self.SOURCE_GRACE_SECONDS
        if previous_source != source_name:
            self._log_watcher_event(
                "ACTIVE_SOURCE",
                generation=self._watcher_generation,
                old=previous_source or "[none]",
                new=source_name or "[none]",
                sink=self._active_sink_name or "[none]",
                watching=self._watching,
            )

    def set_protected_device(self, device_mac):
        """Protect a pending switch target from being torn down before handoff."""
        previous_mac = self._protected_device_mac
        self._protected_device_mac = audio_utils._normalize_mac(device_mac or "") or None
        if self._protected_device_mac:
            self._held_source_macs.discard(self._protected_device_mac)
            self._source_grace_deadlines[self._protected_device_mac] = time.time() + self.SOURCE_GRACE_SECONDS
        if previous_mac != self._protected_device_mac:
            self._log_watcher_event(
                "PROTECTED_DEVICE",
                generation=self._watcher_generation,
                old=previous_mac or "[none]",
                new=self._protected_device_mac or "[none]",
                active_source=self._active_source_name or "[none]",
            )

    def start_watcher(self, active_source_name, active_sink_name):
        """Start background thread that keeps only the active BT source on the speaker."""
        self._active_source_name = active_source_name
        self._active_sink_name = active_sink_name
        if self._watching and self._watcher_thread and self._watcher_thread.is_alive():
            self._log_watcher_event(
                "REUSE",
                generation=self._watcher_generation,
                active_source=active_source_name or "[none]",
                active_sink=active_sink_name or "[none]",
            )
            return
        self._watching = True
        self._watcher_generation += 1
        watcher_generation = self._watcher_generation
        self._log_watcher_event(
            "START",
            generation=watcher_generation,
            active_source=active_source_name or "[none]",
            active_sink=active_sink_name or "[none]",
        )
        self._watcher_thread = threading.Thread(
            target=self._watcher_loop,
            args=(watcher_generation,),
            daemon=True,
            name=f"SourceMuteWatcher-{watcher_generation}",
        )
        self._watcher_thread.start()

    def stop_watcher(self):
        self._watching = False
        previous_generation = self._watcher_generation
        if previous_generation:
            self._watcher_generation += 1
        self._log_watcher_event(
            "STOP",
            previous_generation=previous_generation,
            next_generation=self._watcher_generation,
            thread_alive=bool(self._watcher_thread and self._watcher_thread.is_alive()),
        )
        if self._watcher_thread:
            self._watcher_thread.join(timeout=3)
            self._log_watcher_event(
                "STOPPED",
                generation=previous_generation,
                thread_alive=bool(self._watcher_thread.is_alive()),
            )
            self._watcher_thread = None

    def _watcher_loop(self, watcher_generation):
        self._log_watcher_event(
            "LOOP_START",
            generation=watcher_generation,
            active_source=self._active_source_name or "[none]",
            active_sink=self._active_sink_name or "[none]",
        )
        while self._watching and watcher_generation == self._watcher_generation:
            try:
                if not self._active_sink_name:
                    time.sleep(self.WATCHER_POLL_SECONDS)
                    continue
                active_source_name = self._active_source_name
                active_sink_name = self._active_sink_name
                protected_device_mac = self._protected_device_mac
                changes = route_non_active_bt_sources(
                    active_source_name,
                    active_sink_name,
                    self.NULL_SINK_NAME,
                    protected_device_mac=protected_device_mac,
                    source_grace_deadlines=self._source_grace_deadlines,
                    grace_period_seconds=self.SOURCE_GRACE_SECONDS,
                    source_mute_states=self._source_mute_states,
                    held_source_macs=self._held_source_macs,
                )
                if _changes_include_meaningful_activity(changes):
                    self._log_watcher_event(
                        "CHANGES",
                        generation=watcher_generation,
                        active_source=active_source_name or "[none]",
                        active_sink=active_sink_name or "[none]",
                        protected_device=protected_device_mac or "[none]",
                        changes=repr(changes),
                    )
                    check_active_links(active_sink_name)
            except Exception as exc:
                self._log_watcher_event(
                    "EXCEPTION",
                    generation=watcher_generation,
                    error=repr(exc),
                )
                print(f"WATCHER: exception: {exc}")
            time.sleep(self.WATCHER_POLL_SECONDS)
        self._log_watcher_event(
            "LOOP_EXIT",
            generation=watcher_generation,
            watching=self._watching,
            current_generation=self._watcher_generation,
            active_source=self._active_source_name or "[none]",
            active_sink=self._active_sink_name or "[none]",
        )

    def setup(self):
        """Clean up any stale null sink from earlier versions; no new sink is needed."""
        removed_modules = _unload_named_null_sink_modules(self.NULL_SINK_NAME)
        if removed_modules:
            self._null_sink_module_id = None
        return True

    def sync_inactive_sources(self, active_source_name, active_sink_name):
        """Ensure inactive Bluetooth sources stay muted and off the speaker."""
        if not self.setup():
            return {
                "speaker_links": [],
                "mute_changes": [],
                "removed_links": [],
            }
        return route_non_active_bt_sources(
            active_source_name,
            active_sink_name,
            self.NULL_SINK_NAME,
            protected_device_mac=self._protected_device_mac,
            source_grace_deadlines=self._source_grace_deadlines,
            grace_period_seconds=self.SOURCE_GRACE_SECONDS,
            source_mute_states=self._source_mute_states,
            held_source_macs=self._held_source_macs,
        )

    def hold_source(self, source_name):
        """Keep the outgoing source muted until it becomes active again."""
        if not source_name:
            return []
        source_mac = audio_utils._normalize_mac(audio_utils._extract_mac(source_name))
        if source_mac:
            self._held_source_macs.add(source_mac)
            self._source_grace_deadlines[source_mac] = time.time() + self.SOURCE_GRACE_SECONDS
        self._log_watcher_event(
            "HOLD_SOURCE",
            generation=self._watcher_generation,
            source=source_name,
            source_mac=source_mac or "[none]",
            active_source=self._active_source_name or "[none]",
        )
        if source_name not in audio_utils._list_pipewire_bluez_input_nodes():
            self._source_mute_states.pop(source_name, None)
            return []
        mute_result = _set_source_mute_if_needed(source_name, True, self._source_mute_states)
        if mute_result == "unchanged":
            return []
        return [(source_name, "mute", mute_result)]

    def teardown(self):
        """Stop the watcher and remove any leftover null sink from older runs."""
        self.stop_watcher()
        restore_bt_source_state(self._active_sink_name)
        self._active_source_name = None
        self._active_sink_name = None
        self._protected_device_mac = None
        self._held_source_macs.clear()
        self._source_grace_deadlines.clear()
        self._source_mute_states.clear()
        if self._null_sink_module_id:
            subprocess.run(
                ["pactl", "unload-module", self._null_sink_module_id],
                capture_output=True,
                text=True,
            )
            print(f"NULL_SINK: removed {self.NULL_SINK_NAME} (module {self._null_sink_module_id})")
            self._null_sink_module_id = None
        _unload_named_null_sink_modules(self.NULL_SINK_NAME)
