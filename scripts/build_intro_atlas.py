from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter


GRID_SIZE = 4
FRAME_COUNT = GRID_SIZE * GRID_SIZE
FRAME_SIZE = (650, 330)
WINDOW_SURFACE_RECT = (8, 8, 634, 314)


def _finish_rgba(image: Image.Image) -> Image.Image:
    red, green, blue, alpha = image.convert("RGBA").split()
    rgb = Image.merge("RGB", (red, green, blue))
    rgb = ImageEnhance.Contrast(rgb).enhance(0.78)
    rgb = ImageEnhance.Color(rgb).enhance(0.84)
    rgb = rgb.filter(
        ImageFilter.UnsharpMask(radius=0.65, percent=45, threshold=5)
    )
    finished = Image.merge("RGBA", (*rgb.split(), alpha))
    neutralized: list[tuple[int, int, int, int]] = []
    for red, green, blue, opacity in finished.get_flattened_data():
        if (
            opacity > 16
            and red > 45
            and red > green * 1.25
            and blue > green * 1.10
        ):
            red = min(red, round(blue * 0.48))
            green = max(green, round(blue * 0.58))
            blue = round(blue * 0.84)
        neutralized.append((red, green, blue, opacity))
    finished.putdata(neutralized)
    return finished


def build_atlas(source_path: Path, output_path: Path) -> None:
    source = Image.open(source_path).convert("RGBA")
    source_width, source_height = source.size
    frame_width, frame_height = FRAME_SIZE
    surface_x, surface_y, surface_width, surface_height = WINDOW_SURFACE_RECT
    atlas = Image.new(
        "RGBA", (frame_width * GRID_SIZE, frame_height * GRID_SIZE), (0, 0, 0, 0)
    )

    for frame in range(FRAME_COUNT):
        column = frame % GRID_SIZE
        row = frame // GRID_SIZE
        left = round(column * source_width / GRID_SIZE)
        top = round(row * source_height / GRID_SIZE)
        right = round((column + 1) * source_width / GRID_SIZE)
        bottom = round((row + 1) * source_height / GRID_SIZE)
        cell = source.crop((left, top, right, bottom))
        bounds = cell.getchannel("A").point(lambda value: 255 if value > 16 else 0).getbbox()
        if not bounds:
            raise ValueError(f"No visible paper found in source frame {frame + 1}")
        paper = cell.crop(bounds)

        progress = frame / (FRAME_COUNT - 1)
        expansion = 1 - (1 - progress) ** 1.7
        target_width = round(96 + (surface_width - 96) * expansion)
        target_height = round(96 + (surface_height - 96) * expansion)
        paper = paper.resize((target_width, target_height), Image.Resampling.LANCZOS)
        paper = _finish_rgba(paper)

        settle_offset = 0
        if frame >= FRAME_COUNT - 5:
            settle_progress = (frame - (FRAME_COUNT - 5)) / 4
            settle_offset = round(-6 * (1 - settle_progress) ** 2)
        frame_image = Image.new("RGBA", FRAME_SIZE, (0, 0, 0, 0))
        frame_image.alpha_composite(
            paper,
            (
                surface_x + (surface_width - target_width) // 2,
                surface_y
                + (surface_height - target_height) // 2
                + settle_offset,
            ),
        )
        atlas.alpha_composite(frame_image, (column * frame_width, row * frame_height))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    atlas.save(output_path, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the native-resolution MIAP00 intro animation atlas."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build_atlas(args.source, args.output)


if __name__ == "__main__":
    main()
