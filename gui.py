import tkinter as tk


class AudioRouterGUI:

    REFRESH_INTERVAL_MS = 3000

    def __init__(self, audio_manager):
        self.audio = audio_manager
        self.current_source = None
        self.current_sink = None

        self.root = tk.Tk()
        self.root.title("BT Audio Router")
        self.root.geometry("640x520")
        self.root.resizable(True, True)

        self._running = True
        self._sources = []
        self._sinks = []
        self._build_ui()
        self._refresh_all()

    def _build_ui(self):
        header = tk.Label(
            self.root, text="Bluetooth Audio Router",
            font=("Helvetica", 16, "bold"), pady=10
        )
        header.pack(fill=tk.X)

        active_frame = tk.Frame(self.root)
        active_frame.pack(fill=tk.X, padx=15, pady=(0, 5))
        tk.Label(active_frame, text="Active input:", font=("Helvetica", 10)).pack(side=tk.LEFT)
        self.active_source_label = tk.Label(
            active_frame, text="—", font=("Helvetica", 10, "bold"), fg="green"
        )
        self.active_source_label.pack(side=tk.LEFT, padx=5)
        tk.Label(active_frame, text="   Active output:", font=("Helvetica", 10)).pack(side=tk.LEFT)
        self.active_sink_label = tk.Label(
            active_frame, text="—", font=("Helvetica", 10, "bold"), fg="blue"
        )
        self.active_sink_label.pack(side=tk.LEFT, padx=5)

        panels = tk.Frame(self.root)
        panels.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)
        panels.columnconfigure(0, weight=1)
        panels.columnconfigure(1, weight=1)

        source_frame = tk.LabelFrame(panels, text="Input Sources", padx=5, pady=5)
        source_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        source_scroll = tk.Scrollbar(source_frame)
        source_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.source_list = tk.Listbox(
            source_frame, font=("Courier", 10),
            selectmode=tk.SINGLE, yscrollcommand=source_scroll.set
        )
        self.source_list.pack(fill=tk.BOTH, expand=True)
        source_scroll.config(command=self.source_list.yview)

        self.switch_source_btn = tk.Button(
            source_frame, text="Set Input",
            font=("Helvetica", 10), command=self._on_switch_source
        )
        self.switch_source_btn.pack(fill=tk.X, pady=(5, 0))

        sink_frame = tk.LabelFrame(panels, text="Output Sinks", padx=5, pady=5)
        sink_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        sink_scroll = tk.Scrollbar(sink_frame)
        sink_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.sink_list = tk.Listbox(
            sink_frame, font=("Courier", 10),
            selectmode=tk.SINGLE, yscrollcommand=sink_scroll.set
        )
        self.sink_list.pack(fill=tk.BOTH, expand=True)
        sink_scroll.config(command=self.sink_list.yview)

        self.switch_sink_btn = tk.Button(
            sink_frame, text="Set Output",
            font=("Helvetica", 10), command=self._on_switch_sink
        )
        self.switch_sink_btn.pack(fill=tk.X, pady=(5, 0))

        bottom_frame = tk.Frame(self.root)
        bottom_frame.pack(fill=tk.X, padx=15, pady=(5, 10))

        loopback_frame = tk.Frame(bottom_frame)
        loopback_frame.pack(fill=tk.X, pady=(0, 5))

        self.loopback_btn = tk.Button(
            loopback_frame, text="Start Loopback (Input -> Output)",
            font=("Helvetica", 10), command=self._on_start_loopback
        )
        self.loopback_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 3))

        self.stop_loopback_btn = tk.Button(
            loopback_frame, text="Stop Loopback",
            font=("Helvetica", 10), command=self._on_stop_loopback
        )
        self.stop_loopback_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(3, 0))

        self.refresh_btn = tk.Button(
            bottom_frame, text="Refresh All",
            font=("Helvetica", 11), command=self._refresh_all
        )
        self.refresh_btn.pack(fill=tk.X)

        self.status_bar = tk.Label(
            self.root, text="Ready", bd=1, relief=tk.SUNKEN,
            anchor=tk.W, font=("Helvetica", 9)
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.root.protocol("WM_DELETE_WINDOW", self.stop)

    def _refresh_all(self):
        if not self._running:
            return
        self._refresh_sources()
        self._refresh_sinks()
        self.root.after(self.REFRESH_INTERVAL_MS, self._refresh_all)

    def _refresh_sources(self):
        sources = self.audio.get_sources()
        old_names = [s["name"] for s in self._sources]
        new_names = [s["name"] for s in sources]

        if old_names != new_names:
            selected_name = self._get_selected_name(self.source_list, self._sources)
            self._sources = sources
            self.source_list.delete(0, tk.END)
            for s in sources:
                self.source_list.insert(tk.END, s["description"])
            if selected_name and selected_name in new_names:
                self.source_list.selection_set(new_names.index(selected_name))
        else:
            self._sources = sources

        count = len(sources)
        sink_count = len(self._sinks)
        self.status_bar.config(text=f"{count} input(s), {sink_count} output(s) found")

    def _refresh_sinks(self):
        sinks = self.audio.get_sinks()
        old_names = [s["name"] for s in self._sinks]
        new_names = [s["name"] for s in sinks]

        if old_names != new_names:
            selected_name = self._get_selected_name(self.sink_list, self._sinks)
            self._sinks = sinks
            self.sink_list.delete(0, tk.END)
            for s in sinks:
                self.sink_list.insert(tk.END, s["description"])
            if selected_name and selected_name in new_names:
                self.sink_list.selection_set(new_names.index(selected_name))
        else:
            self._sinks = sinks

        count = len(self._sources)
        sink_count = len(sinks)
        self.status_bar.config(text=f"{count} input(s), {sink_count} output(s) found")

    def _get_selected_name(self, listbox, items):
        selection = listbox.curselection()
        if selection and items:
            return items[selection[0]]["name"]
        return None

    def _on_switch_source(self):
        selection = self.source_list.curselection()
        if not selection:
            self.status_bar.config(text="No input source selected")
            return

        source = self._sources[selection[0]]
        if self.audio.set_default_source(source["name"]):
            self.current_source = source["name"]
            self.active_source_label.config(text=source["description"])
            self.status_bar.config(text=f"Input set to: {source['description']}")
        else:
            self.status_bar.config(text=f"Failed to set input: {source['description']}")

    def _on_switch_sink(self):
        selection = self.sink_list.curselection()
        if not selection:
            self.status_bar.config(text="No output sink selected")
            return

        sink = self._sinks[selection[0]]
        if self.audio.set_default_sink_by_name(sink["name"]):
            self.current_sink = sink["name"]
            self.active_sink_label.config(text=sink["description"])
            self.status_bar.config(text=f"Output set to: {sink['description']}")
        else:
            self.status_bar.config(text=f"Failed to set output: {sink['description']}")

    def _on_start_loopback(self):
        source_sel = self.source_list.curselection()
        sink_sel = self.sink_list.curselection()
        if not source_sel:
            self.status_bar.config(text="Select an input source first")
            return
        if not sink_sel:
            self.status_bar.config(text="Select an output sink first")
            return
        source = self._sources[source_sel[0]]
        sink = self._sinks[sink_sel[0]]
        if self.audio.start_loopback(source["name"], sink["name"], latency_msec=200):
            self.status_bar.config(
                text=f"Loopback: {source['description']} -> {sink['description']} (200ms buffer)"
            )
        else:
            self.status_bar.config(text="Failed to start loopback")

    def _on_stop_loopback(self):
        self.audio.stop_loopback()
        self.status_bar.config(text="Loopback stopped")

    def run(self):
        self.root.mainloop()

    def stop(self):
        self._running = False
        self.audio.stop_loopback()
        self.root.destroy()
