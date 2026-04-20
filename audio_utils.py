"""
PulseAudio/PipeWire utility functions for listing and managing audio devices.
"""
import subprocess

def _pactl(*args):
    """Run a pactl command and return the result."""
    try:
        return subprocess.run(
            ["pactl"] + list(args),
            capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"pactl command failed: {e}")
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
    Return a list of all connected Bluetooth devices (cards), active or not.
    For each device, it also finds the corresponding .monitor source if it exists.
    """
    cards_result = _pactl("list", "cards")
    if not cards_result:
        return []

    devices = []
    current_card = {}
    for line in cards_result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Card #"):
            if current_card and "bluez_card" in current_card.get("name", ""):
                # Before adding, ensure it's in the right mode
                if ensure_a2dp_sink(current_card["name"]):
                    devices.append(current_card)
            current_card = {}
        elif line.startswith("Name:"):
            current_card["name"] = line.split("Name:", 1)[1].strip()
        elif line.startswith("Properties:"):
            current_card["properties"] = {}
        elif current_card and "properties" in current_card and "=" in line:
            key, val = line.split("=", 1)
            current_card["properties"][key.strip()] = val.strip().strip('"')

    if current_card and "bluez_card" in current_card.get("name", ""):
        if ensure_a2dp_sink(current_card["name"]):
            devices.append(current_card)

    # Now, find the monitor source for each device if it's active
    all_sources = list_devices("sources")
    for device in devices:
        device_mac = device.get("properties", {}).get("device.string", "").replace("_", ":")
        device["description"] = device.get("properties", {}).get("device.alias", f"BT Device {device_mac}")
        
        # Find the corresponding monitor source
        monitor_source = next(
            (s for s in all_sources if device_mac in s.get("name", "") and ".monitor" in s.get("name", "")),
            None
        )
        device["monitor_source_name"] = monitor_source["name"] if monitor_source else None

    return devices
