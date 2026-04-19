import argparse
from bt_audio_router import BTAudioRouter


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bluetooth Audio Router")
    parser.add_argument(
        "--test-cycle", action="store_true",
        help="Enable source cycle test mode"
    )
    parser.add_argument(
        "--cycle-interval", type=int, default=5,
        help="Seconds between source switches (default: 5)"
    )
    parser.add_argument(
        "--gui", action="store_true",
        help="Launch the GUI for manual source switching"
    )
    parser.add_argument(
        "--capture", action="store_true",
        help="Use capture-based pipeline (reads PCM through Python instead of module-loopback)"
    )
    parser.add_argument(
        "--capture-source", type=str, default=None,
        help="PulseAudio source name for capture mode (interactive if omitted)"
    )
    parser.add_argument(
        "--capture-sink", type=str, default=None,
        help="PulseAudio sink name for capture mode (interactive if omitted)"
    )
    parser.add_argument(
        "--capture-buffer", type=int, default=64,
        help="Ring buffer size in chunks for capture mode (default: 64)"
    )
    parser.add_argument(
        "--force-a2dp", action="store_true",
        help="Force A2DP profile on all Bluetooth cards at startup"
    )
    parser.add_argument(
        "--exclusive", action="store_true",
        help="Exclusive capture mode: mute all BT, only forward selected device through Python"
    )
    args = parser.parse_args()

    router = BTAudioRouter()
    router.start(
        test_cycle=args.test_cycle,
        cycle_interval=args.cycle_interval,
        gui=args.gui,
        capture=args.capture,
        capture_source=args.capture_source,
        capture_sink=args.capture_sink,
        capture_buffer=args.capture_buffer,
        force_a2dp=args.force_a2dp,
        exclusive=args.exclusive,
    )