"""
Дублирующий сервер. Раздаёт те же MJPEG-потоки на отдельном порту.
Может требовать аутентификацию (те же логины/пароли из основного конфига).
"""
import logging
import threading
from flask import Flask, Response, request, redirect, url_for, session

logger = logging.getLogger(__name__)


class StreamDuplicator:
    def __init__(self, cameras: dict, dup_cfg: dict, app_config: dict):
        self.cameras = cameras
        self.port = int(dup_cfg.get("port", 8080))
        self.host = dup_cfg.get("host", "0.0.0.0")
        self.require_auth = dup_cfg.get("require_auth", True)
        self.app_config = app_config
        self._app: Flask | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    def _check_auth(self) -> bool:
        if not self.require_auth:
            return True
        security = self.app_config.get("security", {})
        if not security.get("auth_enabled", True):
            return True
        token = request.headers.get("X-API-Token") or request.args.get("token")
        if token and token == security.get("api_token"):
            return True
        return bool(session.get("user"))

    def _create_app(self) -> Flask:
        app = Flask(
            f"duplicator_{self.port}",
            template_folder="templates",
        )
        app.secret_key = self.app_config.get("server", {}).get("secret_key", "dup-key")

        @app.before_request
        def _auth_guard():
            # login-роут открыт, остальные требуют авторизации
            if request.endpoint == "login":
                return
            if not self._check_auth():
                return redirect(url_for("login", next=request.path))

        @app.route("/login", methods=["GET", "POST"])
        def login():
            if request.method == "POST":
                u = request.form.get("username", "")
                p = request.form.get("password", "")
                for user in self.app_config.get("security", {}).get("users", []):
                    if user["username"] == u and user["password"] == p:
                        session["user"] = {"username": u, "role": user.get("role", "viewer")}
                        return redirect(request.args.get("next") or url_for("index"))
                return "Неверные креды", 401
            return """
            <!doctype html><html><body style='font-family:sans-serif;background:#1a1a1a;color:#eee;padding:40px'>
            <h2>Дублирующий сервер — вход</h2>
            <form method='post'>
              <p><input name='username' placeholder='Логин' style='padding:6px'></p>
              <p><input name='password' type='password' placeholder='Пароль' style='padding:6px'></p>
              <p><button type='submit' style='padding:6px 14px'>Войти</button></p>
            </form></body></html>"""

        @app.route("/")
        def index():
            links = "".join(
                f'<li><a href="/video_feed/{cid}" style="color:#6cf">{cam.name}</a></li>'
                for cid, cam in self.cameras.items()
            )
            return (
                f"<html><body style='font-family:sans-serif;background:#1a1a1a;color:#eee;padding:30px'>"
                f"<h1>Дублирующие потоки (порт {self.port})</h1>"
                f"<ul>{links}</ul></body></html>"
            )

        @app.route("/video_feed/<camera_id>")
        def video_feed(camera_id: str):
            cam = self.cameras.get(camera_id)
            if not cam:
                return "Camera not found", 404
            return Response(
                cam.mjpeg_generator(),
                mimetype="multipart/x-mixed-replace; boundary=frame",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

        return app

    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            logger.warning("Дублирующий сервер уже запущен")
            return
        self._app = self._create_app()

        def run():
            self._app.run(
                host=self.host,
                port=self.port,
                threaded=True,
                use_reloader=False,
            )

        self._thread = threading.Thread(
            target=run, name=f"duplicator-{self.port}", daemon=True
        )
        self._thread.start()
        logger.info("Дублирующий сервер запущен: %s:%s", self.host, self.port)

    def stop(self):
        logger.info("Дублирующий сервер будет остановлен вместе с основным процессом")