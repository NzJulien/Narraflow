"""Image generation behind a provider interface.

  ImageGenerationProvider
    - generate_scene / regenerate_scene / generate_character_reference /
      generate_location_reference   (all return PNG bytes)

Providers:
  fireworks   - Fireworks-hosted FLUX (the image model NarraFlow already used).
                Real illustrations. Needs FIREWORKS_API_KEY.
  illustrator - local, deterministic storybook illustrator built on Pillow.
                No network, no key. It reads the same structured story facts
                (character colours, setting, mood, revisions), so continuity and
                voice edits are visible and testable without any credentials.
                It is a development/demo fallback, NOT a substitute for real art.

Choose with IMAGE_PROVIDER=fireworks|illustrator (default: illustrator).
"""

from __future__ import annotations

import io
import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter

from .models import Character

log = logging.getLogger("narraflow.studio.images")

SIZE = (1280, 720)
FLUX_SIZE = (1024, 576)


class ImageError(RuntimeError):
    """Human-readable failure from an image provider."""


@dataclass
class SceneRequest:
    prompt: str
    seed: int = 0
    # Structured facts (used by the local illustrator; the prompt carries the
    # same information for real providers).
    text: str = ""  # lower-cased blob of setting/summary/visual prompt/revision note
    characters: List[Character] = field(default_factory=list)
    mood_hint: str = ""


class ImageGenerationProvider:
    name = "base"

    def generate_scene(self, req: SceneRequest) -> bytes:
        raise NotImplementedError

    def regenerate_scene(self, req: SceneRequest) -> bytes:
        # A regeneration is a new draw of the same (now edited) request.
        return self.generate_scene(SceneRequest(**{**req.__dict__, "seed": req.seed + 101}))

    def generate_character_reference(self, req: SceneRequest) -> bytes:
        return self.generate_scene(req)

    def generate_location_reference(self, req: SceneRequest) -> bytes:
        return self.generate_scene(req)


# ---------------------------------------------------------------------------
# Fireworks FLUX
# ---------------------------------------------------------------------------
class FireworksFluxProvider(ImageGenerationProvider):
    name = "fireworks"

    def __init__(self) -> None:
        self.key = os.environ.get("FIREWORKS_API_KEY", "").strip()
        self.model = os.environ.get("FIREWORKS_IMAGE_MODEL", "accounts/fireworks/models/flux-1-schnell-fp8")
        self.base = os.environ.get("FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1")
        if not self.key:
            raise ImageError("FIREWORKS_API_KEY is not set, so real illustrations are unavailable.")

    def generate_scene(self, req: SceneRequest) -> bytes:
        import requests

        url = f"{self.base}/workflows/{self.model}/text_to_image"
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                         "Accept": "image/png"},
                json={"prompt": req.prompt, "width": FLUX_SIZE[0], "height": FLUX_SIZE[1],
                      "steps": 4, "seed": req.seed % (2**31)},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise ImageError("The illustration service could not be reached. Try again in a moment.") from exc
        if resp.status_code == 429:
            raise ImageError("The illustration service is rate limited right now. Try again shortly.")
        if resp.status_code in (401, 403):
            raise ImageError("The illustration service rejected the API key.")
        if not resp.ok:
            log.warning("fireworks image error status=%s", resp.status_code)
            raise ImageError("The illustration service returned an error. Try again.")
        return resp.content


# ---------------------------------------------------------------------------
# Local storybook illustrator
# ---------------------------------------------------------------------------
COLORS = {
    "red": (200, 60, 55), "crimson": (170, 30, 60), "orange": (235, 140, 50), "yellow": (245, 205, 70),
    "gold": (225, 185, 60), "golden": (225, 185, 60), "green": (70, 155, 90), "teal": (40, 150, 150),
    "turquoise": (60, 190, 190), "blue": (60, 110, 210), "indigo": (75, 70, 170), "purple": (130, 80, 180),
    "violet": (140, 90, 190), "pink": (235, 130, 170), "white": (245, 245, 245), "black": (35, 32, 40),
    "brown": (120, 80, 55), "grey": (140, 140, 150), "gray": (140, 140, 150), "silver": (190, 195, 205),
    "cyan": (70, 200, 230), "magenta": (200, 60, 170),
}
COLOR_RE = "|".join(COLORS)
SKIN = [("dark skin", (104, 68, 48)), ("brown skin", (150, 100, 70)), ("tan", (196, 146, 104)),
        ("light skin", (238, 200, 170)), ("pale", (243, 214, 190))]
DEFAULT_SKIN = (170, 116, 84)


def _near_color(text: str, noun: str, default: Tuple[int, int, int]) -> Tuple[int, int, int]:
    """Colour word within a few words of `noun` ('make the tree blue', 'a blue glowing tree')."""
    words = re.findall(r"[a-z']+", text.lower())
    best, dist = None, 99
    for i, w in enumerate(words):
        if w == noun or w == noun + "s":
            for j, c in enumerate(words):
                if c in COLORS and abs(i - j) <= 4 and abs(i - j) < dist:
                    best, dist = c, abs(i - j)
    return COLORS[best] if best else default


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _shade(c, f):
    return tuple(max(0, min(255, int(v * f))) for v in c)


def _palette(text: str) -> dict:
    t = text
    if re.search(r"\b(snow|snowy|winter|frozen|ice)\b", t):
        p = {"top": (170, 195, 230), "bottom": (235, 242, 250), "ground": (240, 244, 250), "sun": (255, 250, 235)}
        snow = True
    elif re.search(r"\b(night|moon|midnight|stars?|dark)\b", t):
        p = {"top": (18, 22, 60), "bottom": (60, 60, 120), "ground": (34, 46, 70), "sun": (240, 240, 210)}
        snow = False
    elif re.search(r"\b(sunset|dusk|evening)\b", t):
        p = {"top": (90, 70, 150), "bottom": (250, 150, 90), "ground": (90, 100, 70), "sun": (255, 210, 130)}
        snow = False
    else:
        p = {"top": (110, 170, 235), "bottom": (250, 235, 200), "ground": (98, 160, 92), "sun": (255, 232, 140)}
        snow = False
    p["snow"] = snow
    p["mysterious"] = bool(re.search(r"\b(mysterious|eerie|misty|fog|spooky|haunted)\b", t))
    p["happy"] = bool(re.search(r"\b(happy|happier|joyful|bright|celebrat\w*|cheerful|warm)\b", t))
    return p


def _draw_person(d: ImageDraw.ImageDraw, char: Character, cx: int, base_y: int, scale: float) -> None:
    text = " ".join([char.description, char.age, char.appearance, char.clothing]
                    + char.persistent_visual_traits).lower()
    skin = next((c for k, c in SKIN if k in text), DEFAULT_SKIN)
    hair = _near_color(text, "hair", _near_color(text, "braids", (40, 30, 30)))
    if "braids" in text and "black" not in text and not re.search(rf"({COLOR_RE})\s+braids", text):
        hair = (35, 28, 30)
    if re.search(r"\b(old|elder|grey|gray|white hair)\b", text) and "hair" in text:
        hair = (200, 200, 205)
    dress = _near_color(text, "dress", _near_color(text, "cloak", _near_color(text, "shirt", (200, 90, 90))))
    child = bool(re.search(r"\b(young|little|child|girl|boy|kid)\b", text))
    s = scale * (0.82 if child else 1.0)
    h = int(300 * s)
    head_r = int(34 * s)
    body_top = base_y - h + head_r * 2
    # legs + sandals
    for dx in (-14, 14):
        d.rectangle([cx + int(dx * s) - int(7 * s), base_y - int(60 * s), cx + int(dx * s) + int(7 * s), base_y],
                    fill=skin)
        d.ellipse([cx + int(dx * s) - int(13 * s), base_y - int(8 * s), cx + int(dx * s) + int(13 * s), base_y + int(6 * s)],
                  fill=(110, 75, 50))
    # dress (trapezoid)
    d.polygon([(cx - int(26 * s), body_top + int(10 * s)), (cx + int(26 * s), body_top + int(10 * s)),
               (cx + int(58 * s), base_y - int(50 * s)), (cx - int(58 * s), base_y - int(50 * s))], fill=dress)
    d.polygon([(cx - int(58 * s), base_y - int(50 * s)), (cx + int(58 * s), base_y - int(50 * s)),
               (cx + int(58 * s), base_y - int(42 * s)), (cx - int(58 * s), base_y - int(42 * s))], fill=_shade(dress, 0.8))
    # arms
    for side in (-1, 1):
        d.line([(cx + side * int(24 * s), body_top + int(24 * s)), (cx + side * int(52 * s), body_top + int(96 * s))],
               fill=skin, width=max(6, int(13 * s)))
    if "bracelet" in text:
        gold = _near_color(text, "bracelet", (235, 200, 70))
        d.ellipse([cx + int(44 * s), body_top + int(88 * s), cx + int(60 * s), body_top + int(100 * s)], fill=gold)
    # hair behind head
    d.ellipse([cx - head_r - int(6 * s), body_top - head_r * 2 - int(8 * s), cx + head_r + int(6 * s), body_top + int(2 * s)],
              fill=hair)
    if "braid" in text:
        for side in (-1, 1):
            for k in range(4):
                d.ellipse([cx + side * (head_r + int(6 * s)) - int(9 * s), body_top - int(20 * s) + k * int(30 * s),
                           cx + side * (head_r + int(6 * s)) + int(9 * s), body_top + int(8 * s) + k * int(30 * s)], fill=hair)
    # face
    d.ellipse([cx - head_r, body_top - head_r * 2, cx + head_r, body_top], fill=skin)
    eye_y = body_top - head_r - int(4 * s)
    for dx in (-12, 12):
        d.ellipse([cx + int(dx * s) - int(4 * s), eye_y - int(4 * s), cx + int(dx * s) + int(4 * s), eye_y + int(5 * s)],
                  fill=(35, 28, 30))
    d.arc([cx - int(12 * s), eye_y + int(6 * s), cx + int(12 * s), eye_y + int(24 * s)], 20, 160, fill=(120, 50, 50), width=max(2, int(3 * s)))


def _draw_tree(d, cx, base_y, color, glow: bool, layer: Image.Image, scale=1.0):
    trunk = (96, 66, 48)
    d.polygon([(cx - int(26 * scale), base_y), (cx + int(26 * scale), base_y), (cx + int(14 * scale), base_y - int(230 * scale)),
               (cx - int(14 * scale), base_y - int(230 * scale))], fill=trunk)
    for k, (dx, dy, r) in enumerate([(0, -300, 150), (-110, -240, 105), (110, -240, 105), (-55, -360, 95), (60, -350, 95)]):
        d.ellipse([cx + int(dx * scale) - int(r * scale), base_y + int(dy * scale) - int(r * scale),
                   cx + int(dx * scale) + int(r * scale), base_y + int(dy * scale) + int(r * scale)],
                  fill=_shade(color, 0.92 + 0.05 * (k % 3)))
    if glow:
        g = ImageDraw.Draw(layer)
        for r, a in ((330, 40), (250, 60), (170, 80)):
            g.ellipse([cx - int(r * scale), base_y - int(300 * scale) - int(r * scale),
                       cx + int(r * scale), base_y - int(300 * scale) + int(r * scale)], fill=color + (a,))


def _draw_dragon(d, cx, cy, color, s=1.0):
    d.ellipse([cx - 150 * s, cy - 55 * s, cx + 130 * s, cy + 55 * s], fill=color)
    d.polygon([(cx + 100 * s, cy - 20 * s), (cx + 210 * s, cy - 70 * s), (cx + 190 * s, cy + 10 * s)], fill=color)
    d.ellipse([cx + 170 * s, cy - 78 * s, cx + 236 * s, cy - 28 * s], fill=_shade(color, 1.1))
    d.polygon([(cx - 140 * s, cy), (cx - 260 * s, cy - 30 * s), (cx - 230 * s, cy + 25 * s)], fill=color)
    d.polygon([(cx - 40 * s, cy - 40 * s), (cx + 20 * s, cy - 190 * s), (cx + 80 * s, cy - 30 * s)], fill=_shade(color, 0.8))
    d.ellipse([cx + 205 * s, cy - 62 * s, cx + 217 * s, cy - 50 * s], fill=(250, 240, 120))


class StorybookIllustrator(ImageGenerationProvider):
    """Deterministic, offline. Same request -> same picture."""

    name = "illustrator"

    def _canvas(self, req: SceneRequest, size=SIZE, portrait_of: Optional[Character] = None) -> Image.Image:
        S = 2  # supersample for smooth edges
        w, h = size[0] * S, size[1] * S
        rng = random.Random(req.seed)
        text = (req.text or req.prompt).lower()
        pal = _palette(text)
        img = Image.new("RGB", (w, h))
        px = ImageDraw.Draw(img)
        horizon = int(h * 0.62)
        for y in range(h):
            t = min(1.0, y / horizon)
            px.line([(0, y), (w, y)], fill=_lerp(pal["top"], pal["bottom"], t) if y < horizon else pal["ground"])
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        if portrait_of is None:
            # sun / moon and stars
            night = pal["top"][2] < 90
            if night:
                for _ in range(70):
                    x, y = rng.randint(0, w), rng.randint(0, int(horizon * 0.8))
                    r = rng.randint(2, 5) * S
                    d.ellipse([x - r, y - r, x + r, y + r], fill=(250, 250, 225))
            sx, sy = int(w * (0.18 if rng.random() < 0.5 else 0.82)), int(h * 0.17)
            d.ellipse([sx - 60 * S, sy - 60 * S, sx + 60 * S, sy + 60 * S], fill=pal["sun"])

            if re.search(r"\b(mountains?|peaks?|ridge|hills?|village)\b", text):
                for k, base in enumerate((0.5, 0.56)):
                    pts, x = [(0, horizon)], 0
                    while x < w:
                        x += rng.randint(150, 260) * S
                        pts.append((x, int(h * base) - rng.randint(40, 190) * S))
                        x += rng.randint(150, 260) * S
                        pts.append((x, horizon))
                    d.polygon(pts + [(w, horizon)], fill=_shade((110, 120, 150) if not pal["snow"] else (200, 212, 232), 0.85 + 0.1 * k))
            if re.search(r"\b(sea|ocean|beach|harbou?r|lake|river|water)\b", text):
                d.rectangle([0, horizon, w, h], fill=(70, 130, 190) if not pal["snow"] else (150, 190, 225))
                for _ in range(24):
                    x, y = rng.randint(0, w), rng.randint(horizon + 20, h)
                    d.line([(x, y), (x + 90 * S, y)], fill=(230, 240, 250), width=3 * S)
            if re.search(r"\b(village|houses?|huts?|town|cottage)\b", text):
                for i in range(5):
                    x = int(w * (0.08 + i * 0.19)) + rng.randint(-30, 30) * S
                    bw, bh = rng.randint(120, 170) * S, rng.randint(80, 120) * S
                    y0 = horizon + rng.randint(-10, 40) * S
                    body = _shade((200, 160, 120), rng.uniform(0.85, 1.05)) if not pal["snow"] else (225, 215, 205)
                    d.rectangle([x, y0 - bh, x + bw, y0], fill=body)
                    d.polygon([(x - 14 * S, y0 - bh), (x + bw // 2, y0 - bh - 70 * S), (x + bw + 14 * S, y0 - bh)],
                              fill=(160, 70, 60) if not pal["snow"] else (250, 250, 252))
                    d.rectangle([x + bw // 2 - 12 * S, y0 - 46 * S, x + bw // 2 + 12 * S, y0], fill=(96, 66, 48))
            if re.search(r"\b(forest|woods?|trees)\b", text) and not re.search(r"\b(glowing|magical|magic) tree\b", text):
                for i in range(9):
                    x = int(w * (i / 8.0)) + rng.randint(-40, 40) * S
                    y0 = horizon + rng.randint(30, 150) * S
                    d.rectangle([x - 10 * S, y0 - 90 * S, x + 10 * S, y0], fill=(96, 66, 48))
                    gcol = (255, 255, 255) if pal["snow"] else (60 + rng.randint(0, 30), 130 + rng.randint(0, 40), 80)
                    d.polygon([(x - 70 * S, y0 - 70 * S), (x, y0 - 230 * S), (x + 70 * S, y0 - 70 * S)], fill=gcol)
            if "tree" in text and re.search(r"glow|magic|mystic|enchant|special|giant|big|ancient|old", text):
                tcol = _near_color(text, "tree", (80, 190, 130))
                _draw_tree(d, int(w * 0.5), horizon + 90 * S, tcol,
                           bool(re.search(r"glow|magic|enchant", text)), layer, scale=0.92 * S)
            if pal["snow"]:
                for _ in range(120):
                    x, y = rng.randint(0, w), rng.randint(0, h)
                    d.ellipse([x - 4 * S, y - 4 * S, x + 4 * S, y + 4 * S], fill=(255, 255, 255))
            if "dragon" in text:
                dcol = _near_color(text, "dragon", (170, 60, 60))
                _draw_dragon(d, int(w * 0.72), int(h * 0.3), dcol, s=1.5 * S)

            people = req.characters
            n = max(1, len(people))
            for i, ch in enumerate(people):
                cx = int(w * (i + 1) / (n + 1)) if n > 1 else int(w * 0.30)
                _draw_person(d, ch, cx, int(h * 0.90), 1.55 * S)
        else:
            d.rectangle([0, int(h * 0.82), w, h], fill=_shade(pal["ground"], 0.95))
            _draw_person(d, portrait_of, w // 2, int(h * 0.92), 2.05 * S)

        img = Image.alpha_composite(img.convert("RGBA"), layer.filter(ImageFilter.GaussianBlur(28 * S)))
        if pal["mysterious"]:
            veil = Image.new("RGBA", (w, h), (90, 60, 150, 70))
            img = Image.alpha_composite(img, veil)
        if pal["happy"]:
            img = Image.alpha_composite(img, Image.new("RGBA", (w, h), (255, 220, 140, 38)))
        # paper grain + vignette for a hand-painted feel
        grain = Image.effect_noise((w, h), 18).convert("L")
        img = Image.blend(img.convert("RGB"), Image.merge("RGB", (grain, grain, grain)), 0.045)
        vig = Image.new("L", (w, h), 0)
        vd = ImageDraw.Draw(vig)
        vd.ellipse([-w * 0.45, -h * 0.45, w * 1.45, h * 1.45], fill=255)
        vig = vig.filter(ImageFilter.GaussianBlur(140 * S))
        dark = Image.new("RGB", (w, h), (20, 16, 28))
        img = Image.composite(img, Image.blend(img, dark, 0.30), vig)
        return img.resize(size, Image.LANCZOS)

    @staticmethod
    def _png(img: Image.Image) -> bytes:
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=1)  # grain is incompressible; optimize=True costs ~7s
        return buf.getvalue()

    def generate_scene(self, req: SceneRequest) -> bytes:
        return self._png(self._canvas(req))

    def generate_character_reference(self, req: SceneRequest) -> bytes:
        char = req.characters[0]
        return self._png(self._canvas(req, size=(720, 720), portrait_of=char))


_provider: Optional[ImageGenerationProvider] = None


def get_provider(refresh: bool = False) -> ImageGenerationProvider:
    global _provider
    if _provider is None or refresh:
        choice = os.environ.get("IMAGE_PROVIDER", "illustrator").strip().lower()
        if choice == "fireworks":
            _provider = FireworksFluxProvider()  # raises ImageError with a clear message if the key is missing
        else:
            _provider = StorybookIllustrator()
    return _provider
