"""CLI entry point for glados-spoke."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from glados_spoke.client import SpokeClient
from glados_spoke.config import SpokeConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GLaDOS Spoke Client")
    subparsers = parser.add_subparsers(dest="command")

    # Main run command
    run_parser = subparsers.add_parser("run", help="Run the spoke client")
    run_parser.add_argument("--hub", type=str, default=None, help="Hub hostname or IP")
    run_parser.add_argument("--port", type=int, default=None, help="Hub WebSocket port (default: 5051)")
    run_parser.add_argument("--config", type=str, default=None, help="Path to spoke_config.yaml")
    run_parser.add_argument("--aec", action="store_true", help="Enable acoustic echo cancellation")
    run_parser.add_argument("--mic-device", type=str, default=None, help="Mic device index")
    run_parser.add_argument("--speaker-device", type=int, default=None, help="Speaker device index")
    run_parser.add_argument("--log-level", type=str, default="INFO", help="Logging level")

    # Setup command
    setup_parser = subparsers.add_parser("setup", help="Auto-detect and configure audio devices")
    setup_parser.add_argument("--auto", action="store_true", help="Auto-select best devices (USB preferred)")
    setup_parser.add_argument("--manual", action="store_true", help="Manually select devices")
    setup_parser.add_argument("--list", action="store_true", help="List devices only")
    setup_parser.add_argument("--config", type=str, default="spoke_config.yaml", help="Path to config file")

    return parser.parse_args()


def run_setup(args: argparse.Namespace) -> None:
    """Run the audio device setup script."""
    setup_dir = Path(__file__).resolve().parent.parent.parent
    setup_script = setup_dir / "setup_audio.py"

    if not setup_script.exists():
        print(f"ERROR: Setup script not found at {setup_script}")
        print("Run this manually: python setup_audio.py")
        sys.exit(1)

    cmd = ["python", str(setup_script)]
    if args.auto:
        cmd.append("--auto")
    elif args.manual:
        cmd.append("--manual")
    elif args.list:
        cmd.append("--list")
    else:
        cmd.append("--manual")  # default to interactive
    cmd.append("--config")
    cmd.append(args.config)

    import subprocess
    result = subprocess.run(cmd, cwd=str(setup_dir))
    sys.exit(result.returncode)


def main() -> None:
    args = parse_args()

    # Handle setup command
    if args.command == "setup":
        run_setup(args)
        return

    # Default to 'run' if no command specified
    if not args.hub and not args.port and not args.config and not args.aec:
        # Try to run setup if no config and no args
        import subprocess
        setup_dir = Path(__file__).resolve().parent.parent.parent
        setup_script = setup_dir / "setup_audio.py"
        if setup_script.exists():
            print("No configuration found. Running audio device setup...")
            result = subprocess.run(["python", str(setup_script), "--auto"],
                                    cwd=str(setup_dir))
            if result.returncode != 0:
                print("Setup failed. You can run it manually with: glados-spoke setup")
                sys.exit(result.returncode)

    # Load config
    config_path = args.config or "spoke_config.yaml"
    config = SpokeConfig.from_file(config_path)

    # Apply CLI overrides
    if args.hub:
        config.hub_host = args.hub
    if args.port:
        config.hub_port = args.port
    if args.aec:
        config.echo_aec_enabled = True
    if args.mic_device is not None:
        config.mic_device = args.mic_device
    if args.speaker_device is not None:
        config.spk_device = args.speaker_device

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger = logging.getLogger("glados_spoke")
    logger.info("Starting spoke client -> %s:%d", config.hub_host, config.hub_port)

    # Run the client
    client = SpokeClient(config)
    try:
        async def _run():
            await client.start()
            await client._wait_for_shutdown()
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        asyncio.run(client.stop())


if __name__ == "__main__":
    main()
