import os
import sys
import signal
from gi.repository import GLib

from bluetooth_adapter import BluetoothAdapter
from audio_manager import AudioManager
from device_manager import DeviceManager

DEVICE_NAME = "raspberrypi"
USB_AUDIO_DEVICE = "Razer Kraken Kitty V2"
DISCOVERABLE = True
PAIRABLE = True


class BTAudioRouter:
    
    def __init__(self):
        self.bluetooth = BluetoothAdapter(DEVICE_NAME, DISCOVERABLE, PAIRABLE)
        self.audio = AudioManager(USB_AUDIO_DEVICE)
        self.devices = DeviceManager()
        
        self.mainloop = None
        
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
    
    def signal_handler(self, signum, frame):
        print("\nExiting the D-Bus service...")
        self.stop()
        sys.exit(0)
    
    def start(self, test_cycle=False, cycle_interval=5, gui=False,
              capture=False, capture_source=None, capture_sink=None,
              capture_buffer=64, force_a2dp=False, exclusive=False):
        print("=" * 60)
        print("Bluetooth Audio Router for Raspberry Pi 5")
        print("=" * 60)
        print()

        if os.getuid() != 0:
            print("ERROR: This script must be run as root. Please run with sudo.")
            sys.exit(1)

        if not self.bluetooth.setup():
            print("ERROR: Bluetooth setup failed. Exiting.")
            sys.exit(1)
        
        print()

        if force_a2dp:
            from force_a2dp import force_a2dp_all
            print("Forcing A2DP profile on all Bluetooth cards...")
            force_a2dp_all()
            print()
        
        if not self.audio.setup():
            print("ERROR: Audio setup failed. Exiting.")
            sys.exit(1)
            
        print()
        print("=" * 60)
        print("Bluetooth Audio Router is RUNNING")
        if test_cycle:
            print("MODE: Source cycle test")
        if gui:
            print("MODE: GUI")
        if capture:
            print("MODE: Capture pipeline (PCM through Python)")
        print("=" * 60)
        print()
        print("Instructions:")
        print(f"1. On your phone, go to Bluetooth Settings")
        print(f"2. Look for '{DEVICE_NAME}'")
        print(f"3. Tap to connect")
        print(f"4. Play audio on your phone - it will stream to USB device!")
        print()
        print("Press Ctrl+C to stop the service.")
        print()

        if test_cycle:
            self.audio.start_cycle_test(interval=cycle_interval)

        if capture or exclusive:
            self._start_capture_mode(capture_source, capture_sink, capture_buffer, gui, exclusive)
            return

        if gui:
            from gui import AudioRouterGUI
            self.gui = AudioRouterGUI(self.audio)
            try:
                self.gui.run()
            finally:
                self.stop()
        else:
            try:
                self.mainloop = GLib.MainLoop()
                self.mainloop.run()
            except KeyboardInterrupt:
                print("\nExiting the D-Bus service...")
                self.stop()
                sys.exit(0)

    def _start_capture_mode(self, source, sink, buffer_chunks, gui, exclusive):
        from capture_pipeline import CapturePipeline, NullSinkManager, list_sources, list_sinks, interactive_select

        null_mgr = None

        if exclusive:
            null_mgr = NullSinkManager()
            null_sink_name = null_mgr.setup()
            if not null_sink_name:
                print("ERROR: Could not create null sink. Exiting.")
                sys.exit(1)
            null_mgr.move_bt_streams_to_null()
            null_mgr.start_monitoring(interval=2)

            # For source, pick from BT monitors (or wait for one)
            if not source:
                bt_sources = null_mgr.get_bt_monitor_sources()
                if not bt_sources:
                    print("\nWaiting for Bluetooth devices...")
                    print("Connect your phones now.\n")
                    import time as _time
                    while not bt_sources:
                        _time.sleep(2)
                        bt_sources = null_mgr.get_bt_monitor_sources()
                source = interactive_select(bt_sources, "BT source")

            # For sink, exclude the null sink
            if not sink:
                real_sinks = [s for s in list_sinks() if null_sink_name not in s.get("name", "")]
                sink = interactive_select(real_sinks, "output sink")
        else:
            source = source or interactive_select(list_sources(), "source")
            sink = sink or interactive_select(list_sinks(), "sink")

        self.capture_pipeline = CapturePipeline(
            source_name=source,
            sink_name=sink,
            buffer_chunks=buffer_chunks,
        )
        self.capture_pipeline.start(print_stats=True)

        if gui:
            from gui import AudioRouterGUI
            self.gui = AudioRouterGUI(self.audio, capture_pipeline=self.capture_pipeline, null_sink_mgr=null_mgr)
            try:
                self.gui.run()
            finally:
                self.capture_pipeline.stop()
                if null_mgr:
                    null_mgr.stop_monitoring()
                    null_mgr.teardown()
                self.stop()
        else:
            try:
                self.mainloop = GLib.MainLoop()
                self.mainloop.run()
            except KeyboardInterrupt:
                self.capture_pipeline.stop()
                if null_mgr:
                    null_mgr.stop_monitoring()
                    null_mgr.teardown()
                self.stop()
                sys.exit(0)
    
    def stop(self):
        self.audio.stop_cycle_test()
        if self.mainloop:
            self.mainloop.quit()
        print("Bluetooth Audio Router stopped.")
