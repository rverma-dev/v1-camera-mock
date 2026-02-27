#!/usr/bin/env python3
"""camera-mock: Multi-stream ONVIF camera simulator with RTSP feeds.

Supports three modes of operation:
  1. Config file  — YAML with explicit streams + Jellyfin auto-discover
  2. Legacy env   — MP4FILE / INTERFACE env vars (backward compatible)
  3. Color bars   — default test pattern when no source is configured

Usage:
  python3 main.py [config.yaml] [interface] [directory]
  python3 main.py --config config.yaml
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer, GLib  # noqa: E402

log = logging.getLogger("camera-mock")

# ---------------------------------------------------------------------------
# Encoder detection
# ---------------------------------------------------------------------------

def _detect_encoder() -> str:
    """Detect the best available H264 encoder."""
    registry = Gst.Registry.get()
    if registry.find_feature("v4l2h264enc", Gst.ElementFactory.__gtype__):
        log.info("detected Pi hardware encoder (v4l2h264enc)")
        return "v4l2h264enc"
    log.info("using software encoder (x264enc)")
    return "x264enc tune=zerolatency speed-preset=ultrafast"


def _get_encoder(config: dict) -> str:
    """Return the encoder pipeline element string based on config."""
    choice = config.get("encoder", "auto")
    if choice == "auto":
        return _detect_encoder()
    if choice == "v4l2h264enc":
        return "v4l2h264enc"
    return "x264enc tune=zerolatency speed-preset=ultrafast"

# ---------------------------------------------------------------------------
# GStreamer pipeline builders
# ---------------------------------------------------------------------------

def _build_pipeline_string(source: str | None, encoder: str, loop: bool = True) -> str:
    """Build a GStreamer pipeline string for a given source.

    Supported sources:
      - None / empty      → color-bar test pattern
      - /path/to/file.mp4 → local file via filesrc
      - http(s)://...     → HTTP stream via souphttpsrc
    """
    pay = "rtph264pay name=pay0 config-interval=1 pt=96"

    if not source:
        return (
            "videotestsrc pattern=bar horizontal-speed=2 "
            "background-color=9228238 foreground-color=4080751 "
            f"! {encoder} ! queue ! {pay}"
        )

    if source.startswith("http://") or source.startswith("https://"):
        src = f'souphttpsrc location="{source}" is-live=false'
    else:
        src = f'filesrc location="{source}"'

    # Use decodebin to handle any container/codec (MKV/HEVC, MP4/H264, etc.)
    pipeline = (
        f"{src} ! decodebin ! queue ! videoconvert ! "
        f"{encoder} ! queue ! {pay}"
    )
    return pipeline


class StreamMediaFactory(GstRtspServer.RTSPMediaFactory):
    """RTSP media factory for a single stream source."""

    def __init__(self, source: str | None, encoder: str, loop: bool = True):
        super().__init__()
        self._source = source
        self._encoder = encoder
        self._loop = loop
        self._pipeline_str = _build_pipeline_string(source, encoder, loop)

    def do_create_element(self, url):
        log.info("launching pipeline: %s", self._pipeline_str)
        return Gst.parse_launch(self._pipeline_str)

# ---------------------------------------------------------------------------
# ONVIF / WS-Discovery helpers
# ---------------------------------------------------------------------------

def _kill_previous(name: str):
    """Kill any existing instances of a process by name."""
    subprocess.run(["pkill", "-9", name], capture_output=True)


def _get_ip4(interface: str) -> str:
    """Get the IPv4 address for the given network interface."""
    output = subprocess.check_output(
        ["/sbin/ip", "-o", "-4", "addr", "list", interface]
    ).decode()
    return output.split()[3].split("/")[0]


def _start_onvif_services(
    interface: str, ip4: str, port: int, directory: str,
    firmware_ver: str, mounts: list[str],
):
    """Start ONVIF and WS-Discovery daemons."""
    _kill_previous("onvif_srvd")
    _kill_previous("wsdd")

    onvif_srvd = os.path.join(directory, "onvif_srvd", "onvif_srvd")
    wsdd_bin = os.path.join(directory, "wsdd", "wsdd")

    if not os.path.isfile(onvif_srvd) or not os.path.isfile(wsdd_bin):
        log.warning("ONVIF binaries not found in %s — skipping ONVIF services", directory)
        return

    # Register the first stream as the ONVIF-advertised URL
    primary_mount = mounts[0] if mounts else "/stream1"
    rtsp_url = f"rtsp://{ip4}:{port}{primary_mount}"

    subprocess.Popen([
        onvif_srvd,
        "--ifs", interface,
        "--scope", "onvif://www.onvif.org/name/TestDev",
        "--scope", "onvif://www.onvif.org/Profile/S",
        "--name", "RTSP",
        "--width", "800", "--height", "600",
        "--url", rtsp_url,
        "--type", "MPEG4",
        "--firmware_ver", firmware_ver,
    ])

    subprocess.Popen([
        wsdd_bin,
        "--if_name", interface,
        "--type", "tdn:NetworkVideoTransmitter",
        "--xaddr", f"http://{ip4}:1000/onvif/device_service",
        "--scope",
        "onvif://www.onvif.org/name/Unknown "
        "onvif://www.onvif.org/Profile/Streaming",
    ])
    log.info("ONVIF services started (primary stream: %s)", rtsp_url)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config(path: str | None) -> dict:
    """Load YAML config or return defaults for legacy env-var mode."""
    if path and os.path.isfile(path):
        try:
            import yaml
        except ImportError:
            log.error("PyYAML required for config file mode: pip install pyyaml")
            sys.exit(1)
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        log.info("loaded config from %s", path)
        return cfg

    # Legacy env-var fallback
    cfg = {
        "interface": os.environ.get("INTERFACE", "eth0"),
        "port": 8554,
        "encoder": "auto",
        "onvif": {"enabled": True, "firmware_version": os.environ.get("FIRMWARE", "1.0")},
    }
    mp4 = os.environ.get("MP4FILE")
    if mp4:
        cfg["streams"] = [{"name": "default", "source": mp4, "mount": "/stream1"}]
    return cfg


def _resolve_streams(config: dict) -> list[dict]:
    """Merge explicit streams with Jellyfin auto-discovered streams."""
    streams = []

    # Auto-discover from Jellyfin
    jf_cfg = config.get("jellyfin")
    if jf_cfg:
        try:
            from jellyfin import discover_streams
            streams = discover_streams(config)
        except ImportError:
            log.error("jellyfin.py not found — cannot auto-discover")
        except Exception as e:
            log.error("jellyfin auto-discover failed: %s", e)

    # Merge explicit streams (explicit wins on mount collision)
    explicit = config.get("streams", [])
    used_mounts = {s["mount"] for s in streams}
    for s in explicit:
        mount = s.get("mount", f"/stream{len(streams) + 1}")
        s["mount"] = mount
        if mount in used_mounts:
            streams = [x for x in streams if x["mount"] != mount]
        streams.append(s)

    # Default: single color-bar stream
    if not streams:
        streams = [{"name": "test-pattern", "source": None, "mount": "/stream1"}]

    return streams

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="camera-mock multi-stream RTSP simulator")
    parser.add_argument("config", nargs="?", help="Path to config.yaml")
    parser.add_argument("interface", nargs="?", help="Network interface (legacy mode)")
    parser.add_argument("directory", nargs="?", help="Scripts directory (legacy mode)")
    parser.add_argument("--config-file", "-c", dest="config_file", help="Path to config.yaml")
    args = parser.parse_args()

    config_path = args.config_file or args.config
    # If first positional arg doesn't look like a YAML file, treat as interface (legacy)
    if config_path and not config_path.endswith((".yaml", ".yml")):
        args.interface = config_path
        config_path = None

    config = _load_config(config_path)

    # Override interface from args if provided
    interface = args.interface or config.get("interface")
    directory = args.directory or os.environ.get("DIRECTORY", "/onvif-camera-mock")
    port = config.get("port", 8554)

    Gst.init(None)
    encoder = _get_encoder(config)
    streams = _resolve_streams(config)

    # Start RTSP server with all streams
    server = GstRtspServer.RTSPServer()
    server.set_service(str(port))
    mount_points = server.get_mount_points()
    mount_names = []

    for stream in streams:
        name = stream.get("name", "unnamed")
        source = stream.get("source")
        mount = stream.get("mount", f"/stream{len(mount_names) + 1}")
        loop_playback = stream.get("loop", True)

        factory = StreamMediaFactory(source, encoder, loop_playback)
        factory.set_shared(True)
        mount_points.add_factory(mount, factory)
        mount_names.append(mount)

        src_label = source or "color-bars"
        log.info("  %-12s → %s  (%s)", mount, name, src_label)

    server.attach(None)
    log.info("RTSP server listening on port %d with %d stream(s)", port, len(streams))

    # Start ONVIF/WS-Discovery if enabled and interface is available
    onvif_cfg = config.get("onvif", {})
    onvif_enabled = onvif_cfg if isinstance(onvif_cfg, bool) else onvif_cfg.get("enabled", True)
    firmware_ver = "1.0"
    if isinstance(onvif_cfg, dict):
        firmware_ver = onvif_cfg.get("firmware_version", "1.0")

    if onvif_enabled and interface:
        try:
            ip4 = _get_ip4(interface)
            _start_onvif_services(interface, ip4, port, directory, firmware_ver, mount_names)
        except Exception as e:
            log.warning("ONVIF startup failed (non-fatal): %s", e)
    elif not interface:
        log.info("no network interface configured — ONVIF services disabled")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()