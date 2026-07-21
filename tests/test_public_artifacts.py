from __future__ import annotations

import struct
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
BANNER = (
    "SYNTHETIC, NON-COMMERCIAL PORTFOLIO PROTOTYPE, learning/demo only, no customers, "
    "only synthetic data. Monetization content is analysis, not an offer."
)


def _jpeg_dimensions(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    assert payload[:2] == b"\xff\xd8"
    offset = 2
    start_of_frame = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset < len(payload) - 8:
        if payload[offset] != 0xFF:
            offset += 1
            continue
        marker = payload[offset + 1]
        offset += 2
        if marker in start_of_frame:
            height, width = struct.unpack(">HH", payload[offset + 3:offset + 7])
            return width, height
        if marker in {0xD8, 0xD9}:
            continue
        segment_length = struct.unpack(">H", payload[offset:offset + 2])[0]
        offset += segment_length
    raise AssertionError(f"JPEG dimensions not found: {path}")


def test_public_readme_preserves_banner_boundary_and_tour() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.splitlines()[0] == BANNER
    assert "NOT a money-recovery" in readme
    assert "Interview talking points" in readme
    assert readme.count("docs/screenshots/") == 8


def test_eight_real_screenshots_have_expected_viewports() -> None:
    screenshots = sorted((ROOT / "docs" / "screenshots").glob("*.jpg"))
    assert len(screenshots) == 8
    dimensions = [_jpeg_dimensions(path) for path in screenshots]
    assert all(width >= 1400 and height >= 950 for width, height in dimensions[:6])
    assert all(360 <= width <= 390 and height >= 800 for width, height in dimensions[6:])


def test_compose_and_ci_cover_runtime_and_required_checks() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["shadowsync"]
    assert service["pull_policy"] == "never"
    assert "8000:8000" in service["ports"]
    assert service["environment"]["SHADOWSYNC_DATA_DIR"] == "/data"
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in dockerfile
    assert "FROM node:22-alpine" in dockerfile
    assert "USER shadowsync" in dockerfile
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for required in (
        "python-version: \"3.12\"",
        "pnpm test",
        "pnpm build",
        "docker/build-push-action",
        "docker compose up --detach --no-build",
        "docker compose down --volumes",
        "/api/health",
    ):
        assert required in ci
