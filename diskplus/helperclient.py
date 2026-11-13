"""Клиент привилегированного помощника: запускает его через pkexec и разбирает JSON-строки."""
from __future__ import annotations

import json
import os
import subprocess
import threading

from gi.repository import GLib

HELPER_PATHS = [os.environ.get("DISKPLUS_HELPER", ""), "/usr/lib/disk-plus/diskplus-helper"]


def find_helper() -> str | None:
    for p in HELPER_PATHS:
        if p and os.path.exists(p):
            return p
    return None


def elevate_command(helper: str) -> list[str]:
    if os.geteuid() == 0:
        return [helper]
    # Скрытый режим для автотестов разработчика: вместо polkit-окна используется sudo -A
    if os.environ.get("DISKPLUS_ELEVATE") == "sudo-askpass":
        return ["sudo", "-A", helper]
    return ["pkexec", helper]


class HelperClient:
    """Один долгоживущий процесс помощника. Все колбэки вызываются в главном потоке GLib."""

    def __init__(self, on_closed=None):
        self.proc: subprocess.Popen | None = None
        self._next = 1
        self._handlers: dict[int, callable] = {}
        self._lock = threading.Lock()
        self._on_closed = on_closed

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> str | None:
        """Возвращает текст ошибки или None."""
        if self.running:
            return None
        helper = find_helper()
        if not helper:
            return "Не найден помощник Disk++ (программа установлена не полностью)"
        try:
            self.proc = subprocess.Popen(elevate_command(helper), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as e:
            return f"Не удалось запустить помощника: {e.strerror}"
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        return None

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                GLib.idle_add(self._dispatch, msg)
        except (OSError, ValueError):
            pass
        code = proc.wait()
        GLib.idle_add(self._closed, code)

    def _dispatch(self, msg):
        h = self._handlers.get(msg.get("id"))
        if h:
            h(msg)
            if msg.get("ev") in ("res", "err", "done"):
                self._handlers.pop(msg.get("id"), None)
        return False

    def _closed(self, code):
        pending = list(self._handlers.items())
        self._handlers.clear()
        for _rid, h in pending:
            h({"ev": "err", "msg": "Связь с помощником потеряна"})
        if self._on_closed:
            self._on_closed(code)
        return False

    def request(self, cmd: str, handler=None, **params) -> int:
        with self._lock:
            rid = self._next
            self._next += 1
        if handler:
            self._handlers[rid] = handler
        try:
            self.proc.stdin.write(json.dumps({"id": rid, "cmd": cmd, **params}) + "\n")
            self.proc.stdin.flush()
        except (OSError, AttributeError, ValueError):
            if handler:
                handler({"ev": "err", "msg": "Помощник не отвечает"})
                self._handlers.pop(rid, None)
        return rid

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
