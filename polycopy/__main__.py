"""Command-line entry point: ``polycopy`` / ``python -m polycopy``.

Starts the trading server and the web terminal on http://127.0.0.1:8765 and
opens it in the default browser.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import socket
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _polycopy_running(port: int) -> bool:
    import httpx

    try:
        response = httpx.get(f"http://127.0.0.1:{port}/api/state", timeout=1.5)
        return response.status_code == 200 and "status" in response.json()
    except Exception:
        return False


def _setup_logging(level: str) -> None:
    from polycopy.paths import data_dir

    root = logging.getLogger()
    root.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = RotatingFileHandler(data_dir() / "polycopy.log", maxBytes=5_000_000,
                                       backupCount=3)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build(demo: bool):
    """Create the bot and the web app. Returns (bot, app, world)."""
    from polycopy.config import SettingsStore
    from polycopy.db import Database
    from polycopy.engine.bot import Bot
    from polycopy.paths import data_dir
    from polycopy.secrets_store import SecretStore
    from polycopy.web.app import create_app

    world = None
    if demo:
        from polycopy.gateway.simulated import SimAccount, SimData, SimStream, SimWorld

        world = SimWorld(seed=11, live_rate=1.6)
        data = SimData(world)
        stream = SimStream(world)
        demo_account = SimAccount(world)

        async def account_factory(**_kwargs):
            return demo_account, {"credentials": None, "builder_key": None}

        secrets = SecretStore(fallback_path=data_dir() / ".demo-credentials.json",
                              use_keyring=False)
    else:
        from polycopy.gateway.polymarket import PolymarketAccount, PolymarketData
        from polycopy.gateway.rtds import ActivityStream

        data = PolymarketData()
        stream = ActivityStream()
        account_factory = PolymarketAccount.login
        secrets = SecretStore()

    store = SettingsStore()
    db = Database(data_dir() / "polycopy.db")
    bot = Bot(store=store, db=db, secrets=secrets, data=data, account_factory=account_factory,
              stream=stream, demo=demo)
    return bot, create_app(bot), world


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="polycopy", description="Polymarket copy-trading server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("POLYCOPY_PORT", 8765)))
    parser.add_argument("--no-browser", action="store_true", help="do not open the web terminal")
    parser.add_argument("--demo", action="store_true",
                        help="run against a simulated market (no real money, no network)")
    parser.add_argument("--data-dir", help="where settings, the database and logs are stored")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    if args.data_dir:
        os.environ["POLYCOPY_HOME"] = args.data_dir
    if args.demo and not os.environ.get("POLYCOPY_HOME"):
        from polycopy.paths import data_dir

        os.environ["POLYCOPY_HOME"] = str(data_dir() / "demo")

    _setup_logging(args.log_level)
    log = logging.getLogger("polycopy")
    host = "127.0.0.1"
    port = args.port
    if not _port_free(host, port):
        if _polycopy_running(port):
            url = f"http://{host}:{port}/"
            print(f"PolyCopy is already running at {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return
        for candidate in range(port + 1, port + 20):
            if _port_free(host, candidate):
                port = candidate
                break
        else:
            sys.exit(f"Port {args.port} is busy; start with --port <number>")

    import uvicorn

    bot, app, world = build(args.demo)
    url = f"http://{host}:{port}/"

    async def serve() -> None:
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off",
                                ws_ping_interval=20)
        server = uvicorn.Server(config)
        app.state.shutdown = lambda: setattr(server, "should_exit", True)

        async def boot() -> None:
            while not server.started:
                await asyncio.sleep(0.05)
            banner = "DEMO MODE (simulated market)" if args.demo else "PolyCopy server"
            print("\n" + "=" * 64)
            print(f"  {banner} is running")
            print(f"  Web terminal: {url}")
            print("  Keep this window open while the bot trades. Ctrl+C to quit.")
            print("=" * 64 + "\n", flush=True)
            if not args.no_browser:
                threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
            if world is not None:
                world.start()
            try:
                await bot.auto_login()
                await bot.resume_from_saved_state()
            except Exception as error:
                log.warning("Startup resume failed: %s", error)

        boot_task = asyncio.create_task(boot())
        try:
            await server.serve()
        finally:
            boot_task.cancel()
            with contextlib.suppress(Exception):
                await bot.shutdown()
            if world is not None:
                await world.stop()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    print("PolyCopy stopped.")


if __name__ == "__main__":
    main()
