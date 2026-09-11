"""Assemble read-only visual evidence, without repainting or generating app imagery."""
import csv
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/artifacts/video-gap-2026-09-11"
VIDEO = Path("/Users/gewenfei/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_fvstjji3yxsu22_7666/msg/video/2026-09/9564c9d12f2136a2908cd8310930ec41_raw.mp4")
FONT = ImageFont.truetype("/System/Library/Fonts/Monaco.ttf", 17)


def comparison():
    sheet = Image.new("RGB", (1800, 1240), "#171d21")
    draw = ImageDraw.Draw(sheet)
    heading_font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 23)
    entries = [
        ("参考视频 00:06.053  ·  办公室全景", OUT / "frames/0014.jpg"),
        ("当前 v0.15.6  ·  办公室全景", OUT / "current-overview.png"),
        ("参考视频 02:55.015  ·  选中员工，人物与面板对应", OUT / "frames/0352.jpg"),
        ("当前 v0.15.6  ·  选中 Nova，但人物已离开画面", OUT / "current-nova-focus.png"),
    ]
    for index, (title, path) in enumerate(entries):
        x, y = index % 2 * 900, index // 2 * 620
        draw.text((x + 12, y + 12), title, font=heading_font, fill="#e5eee9")
        picture = Image.open(path).convert("RGB")
        if path.name.startswith("current-"):
            # Native screenshot is 2x retina: exclude the macOS title bar only.
            picture = picture.crop((0, 62, picture.width, picture.height))
        picture = ImageOps.contain(picture, (884, 568), method=Image.Resampling.LANCZOS)
        sheet.paste(picture, (x + (900 - picture.width) // 2, y + 44 + (568 - picture.height) // 2))
    sheet.save(OUT / "comparison.jpg", quality=92)


def dense_motion():
    ranges = [(12.9, 13.9), (107.2, 108.2)]
    with (OUT / "all-frame-metrics.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    selected = [r for r in rows if any(start <= float(r["seconds"]) <= end for start, end in ranges)]
    dest = OUT / "dense-motion"
    dest.mkdir(exist_ok=True)
    expression = "+".join(f"between(t\\,{start}\\,{end})" for start, end in ranges)
    result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(VIDEO), "-an", "-vf",
                             f"select={expression},crop=1084:680:254:20", "-fps_mode", "passthrough",
                             "-enc_time_base", "1:90000", "-q:v", "3", str(dest / "%04d.jpg")], capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-2000:])
    pictures = sorted(dest.glob("*.jpg"))
    assert len(pictures) == len(selected), (len(pictures), len(selected))
    for batch_index in range(0, len(selected), 30):
        items = list(zip(selected, pictures))[batch_index:batch_index + 30]
        sheet = Image.new("RGB", (1600, 1366), "#171d21")
        draw = ImageDraw.Draw(sheet)
        draw.text((8, 8), "DENSE MOTION | every original frame in 12.9-13.9s / 107.2-108.2s", font=FONT, fill="white")
        for index, (row, path) in enumerate(items):
            x, y = index % 5 * 320, 34 + index // 5 * 222
            sheet.paste(Image.open(path).resize((320, 200)), (x, y))
            draw.text((x + 4, y + 201), f"{float(row['seconds']):.3f}s  n={row['frame']}", font=FONT, fill="#b8d2c8")
        sheet.save(OUT / f"dense-contact-{batch_index // 30 + 1:02}.jpg", quality=91)
    print(f"Dense review: {len(pictures)} consecutive original frames over {ranges}")


if __name__ == "__main__":
    comparison()
    dense_motion()
