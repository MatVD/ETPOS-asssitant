from __future__ import annotations

import inspect
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from uvicorn import Config
from uvicorn.protocols.websockets.websockets_sansio_impl import (
    WebSocketsSansIOProtocol,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def test_uvicorn_rollout_explicitly_selects_sansio_protocol_with_bounded_messages():
    config = Config(
        "etpos_assistant.main:app",
        ws="websockets-sansio",
        ws_max_size=16 * 1024,
    )
    config.load()

    assert config.ws_protocol_class is WebSocketsSansIOProtocol
    assert config.ws_max_size == 16 * 1024

    dispatch_source = inspect.getsource(WebSocketsSansIOProtocol.send_receive_event_to_app)
    receive_source = inspect.getsource(WebSocketsSansIOProtocol.receive)
    assert "pause_reading" in dispatch_source
    assert "resume_reading" in receive_source


def test_service_and_dev_commands_keep_one_worker_and_explicit_websocket_limits():
    service = _read("deploy/systemd/etpos-assistant.service.example")
    makefile = _read("Makefile")

    assert "--workers 1" in service
    assert "--ws websockets-sansio" in service
    assert "--ws-max-size 16384" in service
    assert "--ws-max-queue" not in service

    dev_line = next(
        line for line in makefile.splitlines() if "uvicorn etpos_assistant.main:app" in line
    )
    assert "--ws websockets-sansio" in dev_line
    assert "--ws-max-size 16384" in dev_line
    assert "--ws-max-queue" not in dev_line


def test_nginx_has_dedicated_websocket_location_without_changing_root_sse_proxy():
    nginx = _read("deploy/nginx/agent.matblock.com.example")
    websocket_start = nginx.index("location = /api/transcribe/stream")
    root_start = nginx.index("location / {")

    assert websocket_start < root_start
    websocket_block = nginx[websocket_start:root_start]
    root_block = nginx[root_start:]

    assert "proxy_pass http://127.0.0.1:8787;" in websocket_block
    assert "proxy_http_version 1.1;" in websocket_block
    assert "proxy_set_header Upgrade $http_upgrade;" in websocket_block
    assert 'proxy_set_header Connection "upgrade";' in websocket_block
    assert "proxy_buffering off;" in websocket_block

    assert "proxy_buffering off;" in root_block
    assert "proxy_set_header Upgrade" not in root_block
    assert 'proxy_set_header Connection "upgrade"' not in root_block


def test_streaming_and_pause_finalization_default_off_without_local_env(tmp_path):
    env = os.environ.copy()
    env.pop("ETPOS_WHISPER_STREAMING_ENABLED", None)
    env.pop("ETPOS_WHISPER_STREAM_PAUSE_FINALIZATION_ENABLED", None)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from etpos_assistant.config import settings; "
                "assert settings.whisper_streaming_enabled is False; "
                "assert settings.whisper_stream_pause_finalization_enabled is False"
            ),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_voice_assets_are_declared_as_package_data_and_present():
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    package_data = pyproject["tool"]["setuptools"]["package-data"]["etpos_assistant"]
    assert "static/js/*.js" in package_data

    static_js = PROJECT_ROOT / "src" / "etpos_assistant" / "static" / "js"
    assert (static_js / "voice-stream.js").is_file()
    assert (static_js / "voice-worklet.js").is_file()
