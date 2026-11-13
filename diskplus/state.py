"""Общее состояние приложения: диски, результаты SMART, связь с помощником."""
from __future__ import annotations

from gi.repository import GLib

from . import devices, smart
from .helperclient import HelperClient


class Store:
    def __init__(self):
        self.disks: list[devices.Disk] = []
        self.smart: dict[str, smart.SmartInfo] = {}
        self.health: dict[str, smart.Health] = {}
        self.scan_result: dict[str, dict] = {}
        self.helper = HelperClient(on_closed=self._helper_closed)
        self.helper_ready = False
        self._listeners = []
        self._smart_queue: list[str] = []
        self._smart_busy = False

    # --- события -----------------------------------------------------------------
    def connect(self, fn):
        self._listeners.append(fn)

    def emit(self, event, payload=None):
        for fn in list(self._listeners):
            fn(event, payload)

    # --- диски -------------------------------------------------------------------
    def refresh_disks(self):
        self.disks = devices.list_disks()
        self.emit("disks")
        if self.helper_ready:
            self.load_all_smart()

    def disk_by_path(self, path):
        return next((d for d in self.disks if d.path == path), None)

    # --- помощник ----------------------------------------------------------------
    def ensure_helper(self, on_ready, on_fail):
        if self.helper.running and self.helper_ready:
            on_ready()
            return
        err = self.helper.start()
        if err:
            on_fail(err)
            return

        def hello(msg):
            if msg.get("ev") == "res":
                self.helper_ready = True
                self.emit("helper", True)
                on_ready()
                self.load_all_smart()
            else:
                self.helper_ready = False
                on_fail("Доступ к дискам не разрешён" if "потеряна" in msg.get("msg", "") else msg.get("msg", "Ошибка"))

        self.helper.request("hello", hello)

    def _helper_closed(self, _code):
        self.helper_ready = False
        self.emit("helper", False)

    # --- SMART -------------------------------------------------------------------
    def load_all_smart(self):
        self._smart_queue = [d.path for d in self.disks if not d.virtual and d.path not in self.smart]
        self._next_smart()

    def reload_smart(self, path):
        self.smart.pop(path, None)
        if path not in self._smart_queue:
            self._smart_queue.append(path)
        self._next_smart()

    def _next_smart(self):
        if self._smart_busy or not self._smart_queue or not self.helper_ready:
            return
        path = self._smart_queue.pop(0)
        self._smart_busy = True

        def done(msg):
            self._smart_busy = False
            if msg.get("ev") == "res":
                d = msg["data"]
                info = smart.parse(d.get("smart", {}), d.get("dtype"), d.get("unavailable", False))
            else:
                info = smart.SmartInfo(ok=False, message=msg.get("msg", "Ошибка чтения SMART"))
            self.smart[path] = info
            self.recompute_health(path)
            self.emit("smart", path)
            GLib.idle_add(self._next_smart)

        self.helper.request("smart", done, dev=path)

    def recompute_health(self, path):
        info = self.smart.get(path)
        if info is None:
            return
        self.health[path] = smart.health(info, self.scan_result.get(path))
