"""The README's art stays wired up: every image exists, every <picture> has a light and a dark variant,
and every asset drawn in docs/readme/assets.html is rendered and used."""
from __future__ import annotations

import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
ASSETS_HTML = (ROOT / "docs" / "readme" / "assets.html").read_text(encoding="utf-8")


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", head[16:24])


def local_images() -> list[str]:
    refs = re.findall(r'(?:src|srcset)="([^"]+)"', README) + re.findall(r"!\[[^\]]*\]\(([^)\s]+)", README)
    return [r for r in refs if not r.startswith(("http://", "https://"))]


def test_every_readme_image_exists():
    refs = local_images()
    assert len(refs) > 10
    assert [r for r in refs if not (ROOT / r).is_file()] == []


def test_every_picture_has_a_light_and_a_dark_variant_of_the_same_size():
    pictures = re.findall(r"<picture>(.*?)</picture>", README, re.S)
    assert pictures
    for block in pictures:
        dark = re.search(r'<source media="\(prefers-color-scheme: dark\)" srcset="([^"]+-dark\.png)">', block)
        light = re.search(r'<img src="([^"]+-light\.png)"[^>]* alt="[^"]+"', block)
        assert dark and light, block
        assert dark.group(1).replace("-dark.png", "-light.png") == light.group(1)
        assert png_size(ROOT / dark.group(1)) == png_size(ROOT / light.group(1))


def test_every_drawn_asset_is_rendered_and_used():
    drawn = re.findall(r'data-asset="([^"]+)"( data-single)?', ASSETS_HTML)
    assert drawn
    for name, single in drawn:
        files = [f"docs/readme/{name}.png"] if single else [f"docs/readme/{name}-{t}.png" for t in ("light", "dark")]
        for f in files:
            assert (ROOT / f).is_file(), f"{f} not rendered: run python docs/readme/build.py"
            assert f in README, f"{f} is rendered but the README does not use it"


def test_images_have_alt_text():
    imgs = re.findall(r"<img [^>]*>", README)
    assert imgs
    assert [i for i in imgs if not re.search(r'alt="[^"]+"', i)] == []
