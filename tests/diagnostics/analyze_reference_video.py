"""Read-only source-video analysis; generated evidence goes to a supplied directory.

Decode every video frame, retain frame timestamps and visual-change measurements,
then extract the most-changing frame in each half-second window for visual review.
These measurements are not a beauty score or a substitute for visual inspection.
"""
import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reuse-metrics", action="store_true")
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    frames_dir = output / "frames"
    frames_dir.mkdir(exist_ok=True)
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
        "-of", "json", str(args.video),
    ]))
    timestamps = [float(f["best_effort_timestamp_time"]) for f in probe["frames"]]
    width, height = 320, 200
    crop = "crop=1084:680:254:20"
    command = ["ffmpeg", "-v", "error", "-i", str(args.video), "-an",
               "-vf", f"{crop},scale={width}:{height}", "-fps_mode", "passthrough",
               "-enc_time_base", "1:90000", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
    decoder = None if args.reuse_metrics else subprocess.Popen(command, stdout=subprocess.PIPE)
    previous = None
    rows, buckets, hashes = [], {}, set()
    frame_index = 0
    while decoder is not None:
        raw = decoder.stdout.read(width * height * 3)
        if not raw:
            break
        if len(raw) != width * height * 3:
            raise RuntimeError("Incomplete decoded frame")
        current = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
        diff = np.zeros((height, width)) if previous is None else np.abs(
            current.astype(np.int16) - previous.astype(np.int16)).mean(axis=2)
        timestamp = timestamps[frame_index]
        row = {"frame": frame_index, "seconds": timestamp,
               "mean_change_0_255": round(float(diff.mean()), 5),
               "changed_pixel_fraction_gt12": round(float((diff > 12).mean()), 5),
               "center_change": round(float(diff[20:165, 70:245].mean()), 5),
               "left_panel_change": round(float(diff[20:180, :70].mean()), 5),
               "right_panel_change": round(float(diff[20:170, 245:].mean()), 5)}
        rows.append(row)
        bucket = int(timestamp * 2)
        if bucket not in buckets or row["mean_change_0_255"] > buckets[bucket]["mean_change_0_255"]:
            buckets[bucket] = row
        hashes.add(hashlib.sha256(raw).digest())
        previous = current.copy()
        frame_index += 1
    if args.reuse_metrics:
        with (output / "all-frame-metrics.csv").open() as handle:
            rows = [{key: int(value) if key == "frame" else float(value)
                     for key, value in row.items()} for row in csv.DictReader(handle)]
        if len(rows) != len(timestamps) or any(row["frame"] != i or row["seconds"] != timestamps[i] for i, row in enumerate(rows)):
            raise RuntimeError("Cached metrics do not match video frame timestamps")
        frame_index = len(rows)
        for row in rows:
            bucket = int(row["seconds"] * 2)
            if bucket not in buckets or row["mean_change_0_255"] > buckets[bucket]["mean_change_0_255"]:
                buckets[bucket] = row
    if (decoder is not None and decoder.wait() != 0) or frame_index != len(timestamps):
        raise RuntimeError(f"Decode/timestamp mismatch: {frame_index} / {len(timestamps)}")
    with (output / "all-frame-metrics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    selected = sorted({0, len(rows) - 1, *(r["frame"] for r in buckets.values())})
    def balanced_sum(items):
        if len(items) == 1:
            return items[0]
        middle = len(items) // 2
        return f"({balanced_sum(items[:middle])}+{balanced_sum(items[middle:])})"
    expression = balanced_sum([f"eq(n\\,{index})" for index in selected])
    extraction = subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(args.video), "-an",
        "-vf", f"select={expression},{crop}", "-fps_mode", "vfr", "-q:v", "3",
        "-enc_time_base", "1:90000",
        str(frames_dir / "%04d.jpg"),
    ], capture_output=True)
    if extraction.returncode:
        message = extraction.stderr.decode(errors="replace")
        raise RuntimeError(message[:1200] + "\n…\n" + message[-500:])
    images = sorted(frames_dir.glob("*.jpg"))
    if len(images) != len(selected):
        raise RuntimeError(f"Extraction mismatch: {len(images)} / {len(selected)}")
    manifest = []
    for index, filename in zip(selected, images):
        manifest.append({**rows[index], "image": str(filename.resolve())})
    (output / "selected-frames.json").write_text(json.dumps(manifest, indent=2))
    font = ImageFont.truetype("/System/Library/Fonts/Monaco.ttf", 15)
    for sheet_index in range(0, len(manifest), 30):
        batch = manifest[sheet_index:sheet_index + 30]
        sheet = Image.new("RGB", (1600, 6 * 222 + 34), "#16191d")
        draw = ImageDraw.Draw(sheet)
        draw.text((8, 7), f"REFERENCE | {batch[0]['seconds']:.3f}s - {batch[-1]['seconds']:.3f}s | representative of every 0.5s window", font=font, fill="white")
        for slot, item in enumerate(batch):
            x, y = (slot % 5) * 320, 34 + (slot // 5) * 222
            thumb = Image.open(item["image"]).resize((320, 200))
            sheet.paste(thumb, (x, y))
            seconds = item["seconds"]
            label = f"{int(seconds)//60:02}:{seconds%60:06.3f}  frame {item['frame']}"
            draw.text((x + 4, y + 201), label, font=font, fill="#c8ddd1")
        sheet.save(output / f"contact-{sheet_index//30 + 1:02d}.jpg", quality=90)
    summary = {"source": str(args.video), "decoded_frames": len(rows),
               "first_timestamp": timestamps[0], "last_timestamp": timestamps[-1],
               "unique_downsampled_frame_hashes": None if args.reuse_metrics else len(hashes),
               "crop_xywh": [254, 20, 1084, 680], "metrics_resolution": [width, height],
               "selected_frames": len(manifest), "contact_sheets": (len(manifest) + 29) // 30,
               "strongest_adjacent_frame_changes": sorted(rows[1:], key=lambda r: r["mean_change_0_255"], reverse=True)[:40]}
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({key: value for key, value in summary.items() if key != "strongest_adjacent_frame_changes"}, indent=2))


if __name__ == "__main__":
    main()
