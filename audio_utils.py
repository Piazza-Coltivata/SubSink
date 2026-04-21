"""
PulseAudio/PipeWire utility functions for listing and managing audio devices.
"""
import subprocess
import time


# Keep the best user-facing label seen for each device MAC so transient
# bluetoothctl renames do not churn the UI while a device is reconnecting.
_BT_DEVICE_LABEL_CACHE = {}


def _run_bluetoothctl(*args, timeout=10):
    """Run bluetoothctl and return stdout when available."""
    try:
        result = subprocess.run(
            ["bluetoothctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if result.returncode != 0 and not result.stdout:
        return None
    return result.stdout

def _pactl(*args):
    """Run a pactl command and return the result, with enhanced logging."""
    command = ["pactl"] + list(args)
    print(f"DEBUG: Running command -> {' '.join(command)}")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
        )
        # Decode with replacement so non-UTF-8 device names don't crash the app.
        result_text = type('R', (), {
            'stdout': result.stdout.decode('utf-8', errors='replace'),
            'stderr': result.stderr.decode('utf-8', errors='replace'),
            'returncode': result.returncode,
        })()
        return result_text
    except FileNotFoundError:
        print("DEBUG: ERROR - 'pactl' command not found. Is it installed and in your PATH?")
        return None
    except Exception as e:
        print(f"DEBUG: An unexpected error occurred with pactl: {e}")
        return None

def list_devices(dev_type="sinks"):
    """List sinks or sources."""
    result = _pactl("list", dev_type)
    if not result:
        return []

    devices = []
    current = {}
    key_map = {
        "Sink #": "index",
        "Source #": "index",
        "Name:": "name",
        "Description:": "description",
    }

    for line in result.stdout.splitlines():
        line = line.strip()
        for key, new_key in key_map.items():
            if line.startswith(key):
                if new_key == "index" and current:
                    devices.append(current)
                current = current.copy() if new_key != "index" else {}
                current[new_key] = line.split(key, 1)[1].strip()
                break
    if current:
        devices.append(current)
    return devices

def _normalize_mac(value):
    """Normalize Bluetooth MAC addresses to uppercase colon-separated form."""
    if not value:
        return ""

    mac = value.replace("_", ":").replace("-", ":").upper()
    parts = [part for part in mac.split(":") if part]
    if len(parts) >= 6 and all(len(part) <= 2 for part in parts[:6]):
        return ":".join(part.zfill(2) for part in parts[:6])
    return mac

def _extract_mac(name):
    """Extract a MAC address from a bluez_* PulseAudio/PipeWire object name."""
    if not name:
        return ""

    suffix = name
    for prefix in ("bluez_input.", "bluez_output.", "bluez_card.", "bluez_source."):
        if name.startswith(prefix):
            suffix = name[len(prefix):]
            break

    return _normalize_mac(suffix.split(".", 1)[0])


def _mac_to_bluez_card_name(device_mac):
    """Convert a MAC address to the matching bluez_card name."""
    normalized_mac = _normalize_mac(device_mac)
    if not normalized_mac:
        return ""
    return f"bluez_card.{normalized_mac.replace(':', '_')}"


def _strip_bt_status_suffix(description):
    """Remove UI-only Bluetooth status suffixes from a device label."""
    label = (description or "").strip()
    for suffix in (" [idle]", " [connected]"):
        if label.endswith(suffix):
            return label[:-len(suffix)].strip()
    return label


def _bt_label_score(description, device_mac=""):
    """Score a Bluetooth label; lower scores are better and more stable."""
    label = _strip_bt_status_suffix(description)
    if not label:
        return 100

    normalized_mac = _normalize_mac(device_mac)
    generic_labels = {
        "BT Device",
        "Bluetooth Audio",
        "Bluetooth Speaker",
        "Bluetooth Headset",
    }

    if normalized_mac and label == normalized_mac:
        return 95
    if label in generic_labels or label.startswith("BT Device "):
        return 80
    if label.isdigit():
        return 90

    alnum_count = sum(char.isalnum() for char in label)
    if alnum_count <= 1:
        return 85
    if not any(char.isalpha() for char in label):
        return 70
    return 10


def _choose_bt_label(device_mac, *candidates):
    """Choose the best available label for one Bluetooth device."""
    best_label = ""
    best_score = None

    for candidate in candidates:
        label = _strip_bt_status_suffix(candidate)
        if not label:
            continue

        score = _bt_label_score(label, device_mac)
        if best_score is None or score < best_score:
            best_label = label
            best_score = score

    return best_label


def _best_known_bt_label(device_mac, *candidates):
    """Prefer the best label seen so far for a MAC over weaker live fallbacks."""
    normalized_mac = _normalize_mac(device_mac)
    cached_label = _BT_DEVICE_LABEL_CACHE.get(normalized_mac, "") if normalized_mac else ""
    preferred_label = _choose_bt_label(normalized_mac, cached_label, *candidates)

    if preferred_label and normalized_mac:
        _BT_DEVICE_LABEL_CACHE[normalized_mac] = preferred_label

    return preferred_label

def _normalize_profile_name(value):
    """Normalize PulseAudio/PipeWire profile names for comparison."""
    return (value or "").strip().replace("_", "-")

def _profile_aliases(profile_name):
    """Return equivalent profile names used by different BlueZ backends."""
    normalized_name = _normalize_profile_name(profile_name)
    aliases = {normalized_name}

    if normalized_name == "a2dp-source":
        aliases.add("audio-gateway")
    elif normalized_name == "audio-gateway":
        aliases.add("a2dp-source")

    return aliases

def _profile_matches(profile_name, desired_profile):
    """Return True when a profile matches a base role, including backend aliases."""
    normalized_profile = _normalize_profile_name(profile_name)
    desired_aliases = _profile_aliases(desired_profile)
    return any(
        normalized_profile == candidate
        or normalized_profile.startswith(f"{candidate}-")
        for candidate in desired_aliases
    )

def _choose_card_profile(card, desired_profile):
    """Pick the best available profile on a card for the requested BT role."""
    active_profile = card.get("active_profile", "")
    if _profile_matches(active_profile, desired_profile):
        return active_profile

    matching_profiles = [
        profile for profile in card.get("profiles", [])
        if _profile_matches(profile.get("name", ""), desired_profile)
    ]
    if not matching_profiles:
        return None

    def _profile_rank(profile):
        availability = (profile.get("available") or "").lower()
        normalized_name = _normalize_profile_name(profile.get("name", ""))
        normalized_desired = _normalize_profile_name(desired_profile)
        desired_aliases = _profile_aliases(desired_profile)
        return (
            0 if availability == "yes" else 1 if availability in ("", "unknown") else 2,
            0 if normalized_name == normalized_desired else 1 if normalized_name in desired_aliases else 2,
            normalized_name,
        )

    return sorted(matching_profiles, key=_profile_rank)[0].get("name")

def _list_bt_cards():
    """Return Bluetooth card objects from pactl."""
    cards_result = _pactl("list", "cards")
    if not cards_result:
        return []

    cards = []
    all_card_names = []
    current_card = None
    current_section = None

    def finalize_current_card(card):
        if not card:
            return
        card.setdefault("properties", {})
        card.setdefault("profiles", [])
        card.setdefault("active_profile", "")
        name = card.get("name", "")
        all_card_names.append(name)
        if "bluez_card" in name:
            cards.append(card)

    for raw_line in cards_result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("Card #"):
            finalize_current_card(current_card)
            current_card = {}
            current_section = None
        elif line.startswith("Name:"):
            current_card["name"] = line.split("Name:", 1)[1].strip()
            current_section = None
        elif line.startswith("Properties:"):
            current_card["properties"] = {}
            current_section = "properties"
        elif line.startswith("Profiles:"):
            current_card["profiles"] = []
            current_section = "profiles"
        elif line.startswith("Active Profile:"):
            current_card["active_profile"] = line.split("Active Profile:", 1)[1].strip()
            current_section = None
        elif line.startswith(("Ports:", "Active Port:")):
            current_section = None
        elif current_section == "properties" and current_card and "=" in line:
            key, val = line.split("=", 1)
            current_card["properties"][key.strip()] = val.strip().strip('"')
        elif current_section == "profiles" and current_card and ":" in line:
            profile_name, profile_details = line.split(":", 1)
            profile_name = profile_name.strip()
            if profile_name:
                availability = ""
                if "available:" in profile_details:
                    availability = profile_details.rsplit("available:", 1)[1].rstrip(")").strip()
                current_card["profiles"].append({
                    "name": profile_name,
                    "available": availability,
                })

    finalize_current_card(current_card)

    print(f"BT_CARDS: all cards found: {all_card_names}")
    return cards


def _bluetoothctl_info(device_mac):
    """Return bluetoothctl info output for one device."""
    normalized_mac = _normalize_mac(device_mac)
    if not normalized_mac:
        return ""
    return _run_bluetoothctl("info", normalized_mac) or ""


def is_bt_device_connected(device_mac):
    """Return True when bluetoothctl reports the device is currently connected."""
    return "Connected: yes" in _bluetoothctl_info(device_mac)


def _list_connected_bluetoothctl_audio_devices():
    """Return connected Bluetooth audio-capable devices even when PipeWire lost their cards."""
    output = _run_bluetoothctl("devices", "Connected") or ""
    devices = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("Device "):
            continue

        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue

        device_mac = _normalize_mac(parts[1])
        if not device_mac:
            continue

        info_output = _bluetoothctl_info(device_mac)
        if "Connected: yes" not in info_output:
            continue
        if "UUID: Audio Source" not in info_output and "UUID: Handsfree Audio Gateway" not in info_output:
            continue

        info_description = None
        list_description = parts[2].strip()
        for info_line in info_output.splitlines():
            stripped = info_line.strip()
            if stripped.startswith("Alias:"):
                info_description = stripped.split("Alias:", 1)[1].strip()
                break
            if stripped.startswith("Name:") and not info_description:
                info_description = stripped.split("Name:", 1)[1].strip()

        description = _best_known_bt_label(device_mac, list_description, info_description) or device_mac

        devices.append({
            "device_mac": device_mac,
            "description": description,
        })

    return devices


def has_pipewire_bt_source_node(device_mac):
    """Return True when PipeWire exposes a live BT input node for the device."""
    normalized_mac = _normalize_mac(device_mac)
    if not normalized_mac:
        return False

    return any(
        _extract_mac(node_name) == normalized_mac
        for node_name in _list_pipewire_bluez_input_nodes()
    )


def has_pipewire_bt_audio_device(device_mac):
    """Return True when PipeWire still exposes a BT card or input node for the device."""
    normalized_mac = _normalize_mac(device_mac)
    if not normalized_mac:
        return False

    for card in _list_bt_cards():
        card_mac = _normalize_mac(card.get("properties", {}).get("device.string", "")) or _extract_mac(card.get("name", ""))
        if card_mac == normalized_mac:
            return True

    return has_pipewire_bt_source_node(normalized_mac)


def recover_bt_audio_device(device_mac, log_file=None, require_live_source=False):
    """Try to recover a connected BT device whose PipeWire audio endpoints disappeared."""
    normalized_mac = _normalize_mac(device_mac)
    if not normalized_mac:
        return False

    if require_live_source:
        if has_pipewire_bt_source_node(normalized_mac):
            return True
    elif has_pipewire_bt_audio_device(normalized_mac):
        return True

    commands = []
    if is_bt_device_connected(normalized_mac):
        commands.append(["bluetoothctl", "disconnect", normalized_mac])
    commands.append(["bluetoothctl", "connect", normalized_mac])

    recovery_reason = "no live PipeWire source" if require_live_source else "no PipeWire card/source"

    _append_to_log_file(
        log_file,
        f"RECOVER_BT_AUDIO: mac={normalized_mac} reason={recovery_reason}; attempting reconnect.\n",
    )

    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception as error:
            _append_to_log_file(
                log_file,
                f"  Command: {' '.join(command)}\n  Exception: {error}\n",
            )
            return False

        _append_to_log_file(
            log_file,
            f"  Command: {' '.join(command)}\n"
            f"  Return Code: {result.returncode}\n"
            f"  Stdout: {result.stdout.strip()}\n"
            f"  Stderr: {result.stderr.strip()}\n",
        )

    time.sleep(2)
    recovered = (
        has_pipewire_bt_source_node(normalized_mac)
        if require_live_source
        else has_pipewire_bt_audio_device(normalized_mac)
    )
    _append_to_log_file(
        log_file,
        f"  Result: {'recovered' if recovered else 'still missing'}\n\n",
    )
    return recovered

def _list_pipewire_bluez_input_nodes():
    """Return active Bluetooth input/source nodes discovered from pw-link."""
    try:
        result = subprocess.run(
            ["pw-link", "-iol"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    node_ports = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("|") or " (" in line or ":" not in line:
            continue

        node_name, port_name = line.rsplit(":", 1)
        node_ports.setdefault(node_name, set()).add(port_name)

    active_nodes = []
    for node_name, ports in node_ports.items():
        if not node_name.startswith(("bluez_input.", "bluez_source.")):
            continue
        if any(port.startswith(("output_", "monitor_", "capture_")) for port in ports):
            active_nodes.append(node_name)

    return sorted(active_nodes)

def _build_bt_description(source, card, device_mac):
    """Choose the best available user-facing label for a Bluetooth source."""
    card_properties = card.get("properties", {}) if card else {}
    generic_labels = {
        "BT Device",
        "Bluetooth Audio",
        "Bluetooth Speaker",
        "Bluetooth Headset",
    }

    candidates = [
        card_properties.get("device.alias"),
        card_properties.get("device.description"),
        card_properties.get("device.product.name"),
        source.get("description"),
    ]

    fallback = None
    for candidate in candidates:
        if not candidate:
            continue
        if fallback is None:
            fallback = candidate
        if candidate not in generic_labels:
            return f"{candidate} ({device_mac})" if device_mac else candidate

    if fallback:
        return f"{fallback} ({device_mac})" if device_mac else fallback
    return f"BT Device {device_mac}" if device_mac else "BT Device"

def _format_card_profiles(card):
    """Render a card's available profile names and availability for logs."""
    profiles = card.get("profiles", [])
    if not profiles:
        return "[none]"
    return ", ".join(
        f"{profile.get('name')} [{profile.get('available') or 'unknown'}]"
        for profile in profiles
        if profile.get("name")
    )

def _append_to_log_file(log_file, content):
    """Append debug information to the shared pipeline log."""
    if not log_file or not content:
        return
    try:
        with open(log_file, "a") as log_handle:
            log_handle.write(content)
    except IOError as error:
        print(f"Error writing to log file {log_file}: {error}")

def _ensure_card_profile(card, desired_profile, log_file=None, action_label="PROFILE"):
    """Ensure a BT card is using the best matching available profile."""
    card_name = card.get("name", "")
    active_profile = card.get("active_profile", "")
    target_profile = _choose_card_profile(card, desired_profile)
    desired_label = _normalize_profile_name(desired_profile)
    log_lines = [
        f"{action_label}: {card_name}\n",
        f"  Active Profile: {active_profile or '[none]'}\n",
        f"  Available Profiles: {_format_card_profiles(card)}\n",
        f"  Selected Target Profile: {target_profile or '[none]'}\n",
    ]

    if not target_profile:
        log_lines.append(f"  Result: no matching {desired_label} profile was found.\n\n")
        _append_to_log_file(log_file, "".join(log_lines))
        print(
            f"{action_label}: {card_name} has no matching {desired_label} profile. "
            f"Profiles: {_format_card_profiles(card)}"
        )
        return False

    if _profile_matches(active_profile, desired_profile):
        log_lines.append("  Result: already active.\n\n")
        _append_to_log_file(log_file, "".join(log_lines))
        print(f"{action_label}: {card_name} already using {active_profile}.")
        return True

    command = ["pactl", "set-card-profile", card_name, target_profile]
    log_lines.append(f"  Command: {' '.join(command)}\n")
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        log_lines.append("  Result: command timed out.\n\n")
        _append_to_log_file(log_file, "".join(log_lines))
        print(f"{action_label}: Timed out while setting {card_name} to {target_profile}.")
        return False
    except Exception as error:
        log_lines.append(f"  Exception: {error}\n  Result: failed.\n\n")
        _append_to_log_file(log_file, "".join(log_lines))
        print(f"{action_label}: Exception while setting {card_name}: {error}")
        return False

    log_lines.extend([
        f"  Return Code: {result.returncode}\n",
        f"  Stdout: {result.stdout.strip()}\n",
        f"  Stderr: {result.stderr.strip()}\n",
        "  Result: success.\n\n" if result.returncode == 0 else "  Result: failed.\n\n",
    ])
    _append_to_log_file(log_file, "".join(log_lines))

    if result.returncode == 0:
        print(f"{action_label}: Successfully set {card_name} to {target_profile}.")
        return True

    print(f"{action_label}: Failed to set {card_name} to {target_profile}.")
    return False

def ensure_a2dp_sink(card_name):
    """
    Checks if a card has an A2DP sink profile and sets the best match if needed.
    Returns True if the card is ready, False otherwise.
    """
    card = next((card for card in _list_bt_cards() if card.get("name") == card_name), None)
    if not card:
        print(f"Card '{card_name}' was not found in 'pactl list cards'.")
        return False

    if _ensure_card_profile(card, "a2dp-sink", action_label="ENSURE_A2DP_SINK"):
        time.sleep(1)
        return True
    return False

def ensure_a2dp_source(card_name, log_file=None):
    """Ensure a BT source card is using its best available A2DP source profile."""
    card = next((card for card in _list_bt_cards() if card.get("name") == card_name), None)
    if not card:
        _append_to_log_file(
            log_file,
            f"ENSURE_A2DP_SOURCE: {card_name}\n  Result: card not found in pactl list cards.\n\n",
        )
        print(f"ENSURE_A2DP_SOURCE: Card '{card_name}' was not found.")
        return False

    return _ensure_card_profile(
        card,
        "a2dp-source",
        log_file=log_file,
        action_label="ENSURE_A2DP_SOURCE",
    )


def get_bt_devices():
    """
    Return Bluetooth audio inputs that can be routed by the hub.

    Active phone streams appear as bluez_input/bluez_source objects. We also keep
    Bluetooth cards around for friendly names, and include card-only devices that
    do not look like speaker outputs so the UI can still show an idle phone.
    """
    all_sources = list_devices("sources")
    all_sinks = list_devices("sinks")
    sources_by_name = {
        source.get("name"): dict(source)
        for source in all_sources
        if source.get("name")
    }
    bt_input_source_names = {
        source_name for source_name in sources_by_name
        if source_name.startswith(("bluez_input.", "bluez_source."))
    }
    bt_input_source_names.update(_list_pipewire_bluez_input_nodes())
    bt_output_sink_macs = {
        _extract_mac(sink.get("name", ""))
        for sink in all_sinks
        if sink.get("name", "").startswith("bluez_output.")
    }

    # Exclude any BT input whose MAC matches a BT output sink — those are the
    # speaker's own HFP/SCO microphone, not a phone source.
    bt_input_source_names = {
        name for name in bt_input_source_names
        if _extract_mac(name) not in bt_output_sink_macs
    }

    cards_by_mac = {}
    for card in _list_bt_cards():
        card_mac = _normalize_mac(card.get("properties", {}).get("device.string", "")) or _extract_mac(card.get("name", ""))
        if card_mac:
            cards_by_mac[card_mac] = card

    processed_devices = []
    seen_macs = set()

    for source_name in sorted(bt_input_source_names):
        source = dict(sources_by_name.get(source_name, {"name": source_name, "description": source_name}))
        device_mac = _extract_mac(source_name)
        card = cards_by_mac.get(device_mac)
        source["device_mac"] = device_mac
        source["is_active_source"] = True
        source["audio_profile_ready"] = True
        source["source_name"] = source_name
        source["monitor_source_name"] = source_name
        source["pipewire_card_present"] = bool(card)
        source_description = _build_bt_description(source, card, device_mac)
        source["description"] = _best_known_bt_label(device_mac, source_description) or source_description
        processed_devices.append(source)
        if device_mac:
            seen_macs.add(device_mac)

    for device_mac, card in cards_by_mac.items():
        if device_mac in seen_macs or device_mac in bt_output_sink_macs:
            continue
        profile_ready = bool(_choose_card_profile(card, "a2dp-source"))
        idle_description = _build_bt_description({}, card, device_mac)
        idle_description = _best_known_bt_label(device_mac, idle_description) or idle_description
        processed_devices.append({
            "name": card.get("name"),
            "description": (
                f"{idle_description} [idle]"
                if profile_ready else f"{idle_description} [connected]"
            ),
            "device_mac": device_mac,
            "is_active_source": False,
            "audio_profile_ready": profile_ready,
            "source_name": None,
            "monitor_source_name": None,
            "pipewire_card_present": True,
            "properties": card.get("properties", {}),
        })
        seen_macs.add(device_mac)

    for device in _list_connected_bluetoothctl_audio_devices():
        device_mac = device.get("device_mac")
        if device_mac in seen_macs or device_mac in bt_output_sink_macs:
            continue

        fallback_description = _best_known_bt_label(device_mac, device.get("description")) or device_mac

        processed_devices.append({
            "name": _mac_to_bluez_card_name(device_mac),
            "description": f"{fallback_description} [connected]",
            "device_mac": device_mac,
            "is_active_source": False,
            "audio_profile_ready": False,
            "source_name": None,
            "monitor_source_name": None,
            "pipewire_card_present": False,
            "properties": {},
        })

    processed_devices.sort(
        key=lambda device: (
            device.get("source_name") is None,
            not device.get("audio_profile_ready", True),
            device.get("description", "").lower(),
        )
    )

    print(f"BT_DISCOVERY: sink MACs (excluded from sources): {bt_output_sink_macs}")
    print(f"BT_DISCOVERY: active input nodes: {sorted(bt_input_source_names)}")
    print(f"BT_DISCOVERY: cards by MAC: {list(cards_by_mac.keys())}")
    print(f"BT_DISCOVERY: final device list: {[d['description'] for d in processed_devices]}")

    return processed_devices

def activate_bt_source_cards(exclude_macs=None, log_file=None):
    """
    Set all BT phone cards to their best matching A2DP source profile,
    skipping any whose MAC is in exclude_macs (typically the speaker).
    Called on startup to wake up phones that were set to 'off' on the last
    close, and before hub start. Returns the count of cards that are ready.
    """
    if exclude_macs is None:
        exclude_macs = set()
    exclude_macs = {_normalize_mac(m) for m in exclude_macs}

    activated = 0
    _append_to_log_file(log_file, "--- Activating BT Source Cards ---\n")

    for card in _list_bt_cards():
        card_name = card.get("name", "")
        card_mac = _normalize_mac(
            card.get("properties", {}).get("device.string", "")
        ) or _extract_mac(card_name)
        
        if card_mac in exclude_macs:
            _append_to_log_file(
                log_file,
                f"ACTIVATE: Skipping {card_name} (identified as speaker)\n",
            )
            continue
        _append_to_log_file(
            log_file,
            f"Attempted to activate card: {card_name} (MAC: {card_mac})\n",
        )
        if _ensure_card_profile(card, "a2dp-source", log_file=log_file, action_label="ACTIVATE"):
            activated += 1
            
    return activated


def deactivate_bt_source_cards(exclude_macs=None, log_file=None):
    """
    Set all BT phone cards to 'off' profile, skipping the speaker.
    Called on app close so WirePlumber cannot auto-route phones after exit.
    Uses _list_bt_cards() directly so it catches cards not currently streaming.
    """
    if exclude_macs is None:
        exclude_macs = set()
    exclude_macs = {_normalize_mac(m) for m in exclude_macs}

    _append_to_log_file(log_file, "--- Deactivating BT Source Cards ---\n")

    for card in _list_bt_cards():
        card_name = card.get("name", "")
        card_mac = _normalize_mac(
            card.get("properties", {}).get("device.string", "")
        ) or _extract_mac(card_name)
        if card_mac in exclude_macs:
            _append_to_log_file(
                log_file,
                f"DEACTIVATE: Skipping {card_name} (excluded MAC {card_mac})\n",
            )
            continue
        result = _pactl("set-card-profile", card_name, "off")
        if result:
            _append_to_log_file(
                log_file,
                f"DEACTIVATE: {card_name} -> off\n"
                f"  Return Code: {result.returncode}\n"
                f"  Stdout: {result.stdout.strip()}\n"
                f"  Stderr: {result.stderr.strip()}\n",
            )
        print(f"DEACTIVATE: {card_name} -> off")


def debug_print_all_audio():
    print("\n=== DEBUG: pactl list sources short ===")
    subprocess.run(["pactl", "list", "sources", "short"])
    print("\n=== DEBUG: pactl list sinks short ===")
    subprocess.run(["pactl", "list", "sinks", "short"])
    print("\n=== DEBUG: pactl list cards short ===")
    subprocess.run(["pactl", "list", "cards", "short"])
    print("\n=== DEBUG: active Bluetooth input nodes from pw-link ===")
    for node_name in _list_pipewire_bluez_input_nodes():
        print(node_name)

# Call this at the top-level when the module is run
if __name__ == "__main__":
    debug_print_all_audio()