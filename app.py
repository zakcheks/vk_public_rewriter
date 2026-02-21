"""
Flask API для VK Link Rewriter — запуск замены ссылок и потоковый лог.
"""
import sys
import threading
from queue import Queue, Empty

from flask import Flask, request, Response, render_template

import vk_link_rewriter as core

app = Flask(__name__)

# Состояние текущей задачи
_log_queue: Queue = Queue()
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None


class QueueWriter:
    """Перенаправляет print() в очередь для стриминга в браузер."""

    def __init__(self, queue: Queue):
        self.queue = queue

    def write(self, text: str) -> None:
        if text:
            self.queue.put(text)

    def flush(self) -> None:
        pass


def run_worker(token: str, old_link: str, new_link: str, communities: list[str]) -> None:
    global _stop_event
    logger = QueueWriter(_log_queue)
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = logger

    try:
        try:
            core.init_vk_api(token=token or None, ignore_env_token=True)
        except Exception as e:
            _log_queue.put(f"❌ Ошибка инициализации VK API: {e}\n")
            _log_queue.put("\x00")  # сигнал конца
            return

        if not communities:
            _log_queue.put("❌ Список сообществ пуст.\n")
            _log_queue.put("\x00")
            return

        _log_queue.put(f"\n🔍 Начинаем обработку {len(communities)} сообществ...\n")
        for comm in communities:
            if _stop_event.is_set():
                _log_queue.put("\n⏹ Операция остановлена пользователем.\n")
                break
            try:
                core.process_community(comm, old_link, new_link)
            except Exception as e:
                _log_queue.put(f"❌ Ошибка при обработке {comm}: {e}\n")
        _log_queue.put("\n🎉 Работа завершена!\n")
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        _log_queue.put("\x00")  # сигнал конца потока


@app.route("/api/run", methods=["POST"])
def api_run():
    """Запуск замены ссылок. Тело: JSON { token, old_link, new_link, communities }."""
    global _worker_thread, _log_queue, _stop_event

    if _worker_thread and _worker_thread.is_alive():
        return {"error": "Задача уже выполняется"}, 409

    data = request.get_json() or {}
    token = (data.get("token") or "").strip()
    old_link = (data.get("old_link") or "").strip()
    new_link = (data.get("new_link") or "").strip()
    communities = [line.strip() for line in (data.get("communities") or []) if line.strip()]

    if not token:
        return {"error": "Укажите VK токен"}, 400
    if not old_link or not new_link:
        return {"error": "Старая и новая ссылки не должны быть пустыми"}, 400
    if not communities:
        return {"error": "Укажите хотя бы одно сообщество"}, 400

    _stop_event.clear()
    _log_queue = Queue()
    _log_queue.put("🔄 Массовая замена ссылок в постах и комментариях ВК\n")
    _log_queue.put("Начало обработки...\n\n")

    _worker_thread = threading.Thread(
        target=run_worker,
        args=(token, old_link, new_link, communities),
        daemon=True,
    )
    _worker_thread.start()

    def generate():
        while True:
            try:
                chunk = _log_queue.get(timeout=30)
            except Empty:
                if _worker_thread and not _worker_thread.is_alive():
                    break
                continue
            if chunk == "\x00":
                break
            for line in chunk.replace("\r", "").split("\n"):
                yield f"data: {line}\n"
            yield "\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Запрос остановки текущей задачи (между сообществами)."""
    global _stop_event
    _stop_event.set()
    return {"ok": True}


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
