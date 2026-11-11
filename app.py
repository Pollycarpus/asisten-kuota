"""
File: app.py
Purpose: One start command. Runs two servers: the app (chat UI + /api) on APP_PORT
         and the mock ESB (/esb) on ESB_PORT, so the ESB behaves like a separate system.
D2 component: Deployment wiring (Chat UI + Conversation API + Mock ESB)
Depends on: flask, werkzeug, api/routes.py, mock_esb/server.py, core/config.py
"""

import logging
import threading

from flask import Flask, send_from_directory
from werkzeug.serving import make_server

from api.routes import api
from core.config import ROOT_DIR, SETTINGS
from mock_esb.server import create_esb_app

STATIC_DIR = ROOT_DIR / "static"


def create_app() -> Flask:
    """
    Create the main app: static chat UI + our backend API.

    Why: UI and API share one origin (no CORS); the ESB is reached only over HTTP.
    Args:
        None
    Returns:
        Flask: the main application.
    Raises / Errors:
        None
    Side effects:
        None
    """
    app = Flask("asisten_kuota", static_folder=str(STATIC_DIR), static_url_path="/static")
    app.register_blueprint(api)

    @app.get("/")
    def index():
        """
        Serve the chat UI.

        Why: simple web/chat UI (D4).
        Args:
            None
        Returns:
            index.html
        Raises / Errors:
            None
        Side effects:
            None
        """
        return send_from_directory(STATIC_DIR, "index.html")

    return app


def start_server(app: Flask, port: int, name: str):
    """
    Start a threaded WSGI server for one app in a background thread.

    Why: threaded servers let the adapter call the mock ESB while another request is
         in progress (needed for real timeouts and same-key retries).
    Args:
        app (Flask): application to serve.
        port (int): TCP port.
        name (str): label for logs.
    Returns:
        werkzeug server: the running server (call .shutdown() to stop).
    Raises / Errors:
        OSError: if the port is already in use.
    Side effects:
        Opens a listening socket; starts a daemon thread.
    """
    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, name=name, daemon=True).start()
    return server


def main() -> None:
    """
    Start the mock ESB and the app, then wait until Ctrl+C.

    Why: README quick start — one command, two ports.
    Args:
        None
    Returns:
        None
    Raises / Errors:
        OSError: port in use (see README troubleshooting).
    Side effects:
        Runs two servers until interrupted.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    esb_server = start_server(create_esb_app(), SETTINGS["esb_port"], "mock-esb")
    app_server = start_server(create_app(), SETTINGS["app_port"], "app")
    llm = SETTINGS["llm"]
    print(f"Mock ESB : http://localhost:{SETTINGS['esb_port']}/esb")
    print(f"Chat UI  : http://localhost:{SETTINGS['app_port']}   (LLM: {llm['provider']}/{llm['model']})")
    print("Press Ctrl+C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        app_server.shutdown()
        esb_server.shutdown()


if __name__ == "__main__":
    main()
