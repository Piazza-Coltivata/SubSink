"""
PulseAudio/PipeWire utility functions for listing and managing audio devices.
"""
import subprocess
import time

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

def _list_bt_cards():
    """Return Bluetooth card objects from pactl."""
    cards_result = _pactl("list", "cards")
    if not cards_result:
        return []

    cards = []
    all_card_names = []
    current_card = {}
    for raw_line in cards_result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("Card #"):
            if current_card:
                name = current_card.get("name", "")
                all_card_names.append(name)
                if "bluez_card" in name:
                    cards.append(current_card)
            current_card = {}
        elif line.startswith("Name:"):
            current_card["name"] = line.split("Name:", 1)[1].strip()
        elif line.startswith("Properties:"):
            current_card["properties"] = {}
        elif current_card and "properties" in current_card and "=" in line:
            key, val = line.split("=", 1)
            current_card["properties"][key.strip()] = val.strip().strip('"')

    if current_card:
        name = current_card.get("name", "")
        all_card_names.append(name)
        if "bluez_card" in name:
            cards.append(current_card)

    print(f"BT_CARDS: all cards found: {all_card_names}")
    return cards

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

def ensure_a2dp_sink(card_name):
    """
    Checks if a card has an 'a2dp-sink' profile and sets it if not active.
    Returns True if the card is ready, False otherwise.
    """
    card_info = _pactl("list", "cards")
    if not card_info:
        return False

    in_card_section = False
    active_profile = ""
    has_a2dp_sink = False

    for line in card_info.stdout.splitlines():
        line = line.strip()
        if f"Name: {card_name}" in line:
            in_card_section = True
            continue
        
        if in_card_section:
            if line.startswith("Card #"): # Reached the next card
                break
            if "a2dp-sink" in line:
                has_a2dp_sink = True
            if line.startswith("Active Profile:") and "a2dp-sink" in line:
                # Already in the correct mode
                return True
    
    if in_card_section and has_a2dp_sink:
        print(f"Card '{card_name}' is not in a2dp-sink mode. Attempting to set it...")
        result = _pactl("set-card-profile", card_name, "a2dp-sink")
        if result and result.returncode == 0:
            print("Successfully set profile to a2dp-sink.")
            time.sleep(1) # Give the system a moment to apply the change
            return True
        else:
            print(f"Failed to set profile for '{card_name}'.")
            return False
    
    return False


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
        source["source_name"] = source_name
        source["monitor_source_name"] = source_name
        source["description"] = _build_bt_description(source, card, device_mac)
        processed_devices.append(source)
        if device_mac:
            seen_macs.add(device_mac)

    for device_mac, card in cards_by_mac.items():
        if device_mac in seen_macs or device_mac in bt_output_sink_macs:
            continue
        idle_description = _build_bt_description({}, card, device_mac)
        processed_devices.append({
            "name": card.get("name"),
            "description": f"{idle_description} [idle]",
            "device_mac": device_mac,
            "is_active_source": False,
            "source_name": None,
            "monitor_source_name": None,
            "properties": card.get("properties", {}),
        })

    processed_devices.sort(
        key=lambda device: (
            device.get("source_name") is None,
            device.get("description", "").lower(),
        )
    )

    print(f"BT_DISCOVERY: sink MACs (excluded from sources): {bt_output_sink_macs}")
    print(f"BT_DISCOVERY: active input nodes: {sorted(bt_input_source_names)}")
    print(f"BT_DISCOVERY: cards by MAC: {list(cards_by_mac.keys())}")
    print(f"BT_DISCOVERY: final device list: {[d['description'] for d in processed_devices]}")

    return processed_devices

def activate_bt_source_cards(exclude_macs=None):
    """
    Set all BT phone cards to 'a2dp-source' profile, skipping any whose MAC
    is in exclude_macs (typically the speaker). Called on startup to wake up
    phones that were set to 'off' on the last close, and before hub start.
    Returns the count of cards successfully activated.
    """
    if exclude_macs is None:
        exclude_macs = set()
    exclude_macs = {_normalize_mac(m) for m in exclude_macs}

    activated = 0
    for card in _list_bt_cards():
        card_name = card.get("name", "")
        card_mac = _normalize_mac(
            card.get("properties", {}).get("device.string", "")
        ) or _extract_mac(card_name)
        if card_mac in exclude_macs:
            print(f"ACTIVATE: Skipping {card_name} (speaker)")
            continue
        result = _pactl("set-card-profile", card_name, "a2dp-source")
        if result and result.returncode == 0:
            print(f"ACTIVATE: {card_name} -> a2dp-source")
            activated += 1
        else:
            print(f"ACTIVATE: {card_name} could not be set to a2dp-source (may be unsupported or already active)")
    return activated


def deactivate_bt_source_cards(exclude_macs=None):
    """
    Set all BT phone cards to 'off' profile, skipping the speaker.
    Called on app close so WirePlumber cannot auto-route phones after exit.
    Uses _list_bt_cards() directly so it catches cards not currently streaming.
    """
    if exclude_macs is None:
        exclude_macs = set()
    exclude_macs = {_normalize_mac(m) for m in exclude_macs}

    for card in _list_bt_cards():
        card_name = card.get("name", "")
        card_mac = _normalize_mac(
            card.get("properties", {}).get("device.string", "")
        ) or _extract_mac(card_name)
        if card_mac in exclude_macs:
            continue
        _pactl("set-card-profile", card_name, "off")
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