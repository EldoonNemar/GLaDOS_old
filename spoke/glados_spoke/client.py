"""Main spoke client: audio capture -> hub -> audio playback."""

from __future__ import annotations

import asyncio

import logging

import math

import signal

import sys

import threading

import numpy as np

from .audio import SpokeAudio

from .config import SpokeConfig


from .ws_client import MicClient, SpeakerClient, ReconnectingWS, _build_url



logger = logging.getLogger(__name__)





class SpokeClient:

    """Main spoke client."""



    def __init__(self, config: SpokeConfig | None = None) -> None:

        self.config = config or SpokeConfig()

        self._shutdown = threading.Event()

        self._audio = SpokeAudio(

            mic_sample_rate=self.config.mic_sample_rate,

            mic_channels=self.config.mic_channels,

            mic_chunk_frames=self.config.mic_chunk_frames,

            mic_device=self.config.mic_device,

            mic_volume=self.config.mic_volume,

            spk_sample_rate=self.config.spk_sample_rate,

            spk_device=self.config.spk_device,

            spk_volume=self.config.spk_volume,

            echo_aec_enabled=self.config.echo_aec_enabled,

            echo_aec_order=self.config.echo_aec_order,

        )
        self._mic_client = MicClient(

            url=_build_url(self.config.hub_host, self.config.hub_port, "/microphone"),

            on_capture=lambda samples: None,

            reconnect_base_delay=self.config.reconnect_base_delay,

            reconnect_max_delay=self.config.reconnect_max_delay,

            reconnect_jitter=self.config.reconnect_jitter,

            reconnect_attempts=self.config.reconnect_attempts,

            heartbeat_interval_s=self.config.heartbeat_interval_s,

        )

        self._speaker_client = SpeakerClient(

            url=_build_url(self.config.hub_host, self.config.hub_port, "/speaker"),

            spk_device=self.config.spk_device,

            reconnect_base_delay=self.config.reconnect_base_delay,

            reconnect_max_delay=self.config.reconnect_max_delay,

            reconnect_jitter=self.config.reconnect_jitter,

            reconnect_attempts=self.config.reconnect_attempts,

            heartbeat_interval_s=self.config.heartbeat_interval_s,

        )

        self._running = False



    async def start(self) -> None:

        logger.info("Starting GLaDOS spoke -> %s:%d", self.config.hub_host, self.config.hub_port)

        self._running = True

        self._audio.start_mic(self._mic_capture)

        self._audio.start_speaker()

        await self._mic_client.start()

        await self._speaker_client.start()
        logger.info("Spoke started. Waiting for shutdown...")

        await self._wait_for_shutdown()



    async def stop(self) -> None:

        logger.info("Stopping spoke...")

        self._running = False

        self._shutdown.set()

        await self._mic_client.stop()

        await self._speaker_client.stop()

        self._audio.shutdown()

        if hasattr(self, "_playback_thread") and self._playback_thread.is_alive():

            self._playback_thread.join(timeout=2.0)

        logger.info("Spoke stopped.")



    async def _wait_for_shutdown(self) -> None:

        loop = asyncio.get_running_loop()

        def _on_signal():

            asyncio.ensure_future(self.stop())

        for sig in (signal.SIGINT, signal.SIGTERM):

            try:

                loop.add_signal_handler(sig, _on_signal)

            except (NotImplementedError, OSError):

                pass

        while self._running and not self._shutdown.is_set():

            await asyncio.sleep(0.5)



    def _mic_capture(self, samples: np.ndarray) -> None:

        if not self._running:

            return

        try:

            self._mic_client.push_sample(np.ascontiguousarray(samples, dtype=np.float32))

        except Exception:

            pass






def main() -> None:

    """CLI entry point."""

    import argparse

    ap = argparse.ArgumentParser(description="GLaDOS Spoke Client")

    ap.add_argument("--hub", default=None)

    ap.add_argument("--port", type=int, default=None)

    ap.add_argument("--config", default=None)

    ap.add_argument("--log-level", default="INFO")

    ap.add_argument("--mic-device", type=str, default=None)

    ap.add_argument("--speaker-device", type=int, default=None)

    ap.add_argument("--aec", action="store_true")

    ap.add_argument("--aec-order", type=int, default=256)

    ap.add_argument("--reconnect-base", type=float, default=1.0)

    ap.add_argument("--reconnect-max", type=float, default=30.0)

    ap.add_argument("--jitter-min", type=int, default=80)

    ap.add_argument("--jitter-max", type=int, default=400)

    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),

                       format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    cfg = SpokeConfig()

    if args.config:

        import yaml

        with open(args.config) as f:

            data = yaml.safe_load(f)

        if data and "spoke" in data:

            cfg = SpokeConfig(**data["spoke"])

    if args.hub:

        cfg.hub_host = args.hub

    if args.port:

        cfg.hub_port = args.port

    if args.mic_device is not None:

        cfg.mic_device = args.mic_device

    if args.speaker_device is not None:

        cfg.spk_device = args.speaker_device

    if args.aec:

        cfg.echo_aec_enabled = True

    if args.aec_order:

        cfg.echo_aec_order = args.aec_order

    if args.reconnect_base:

        cfg.reconnect_base_delay = args.reconnect_base

    if args.reconnect_max:

        cfg.reconnect_max_delay = args.reconnect_max

    if args.jitter_min:

        cfg.jitter_buffer_min_ms = args.jitter_min

    if args.jitter_max:

        cfg.jitter_buffer_max_ms = args.jitter_max

    async def _run():

        client = SpokeClient(cfg)

        try:

            await client.start()

        except Exception:

            logger.exception("Spoke client crashed")

        finally:

            await client.stop()

    asyncio.run(_run())

