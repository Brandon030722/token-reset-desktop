#!/usr/bin/env python3
"""Rebuild original Tibo desktop icons with macOS AppKit, no Python packages.

Run on macOS: python3 desktop/assets/generate_icons.py
The SVG is editable geometry; no game artwork, fonts or remote assets are used.
All tools run at build time. The application only loads its packaged icon.
"""
from pathlib import Path
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
  <title>Token重置</title>
  <desc>黑色圆角方形，白色几何字母 T 与感叹号。</desc>
  <rect x="48" y="48" width="416" height="416" rx="100" fill="#080808"/>
  <path d="M139 168H285V216H237V344H187V216H139Z" fill="#fff"/>
  <path d="M316 168H365L358 284H323Z" fill="#fff"/>
  <circle cx="340.5" cy="325" r="23" fill="#fff"/>
</svg>
"""

SWIFT = r"""
import AppKit
import Foundation

let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let ink = CGColor(gray: 1, alpha: 1)
let black = CGColor(gray: 8/255, alpha: 1)

func render(_ size: Int, _ filename: String) throws {
    guard let context = CGContext(
        data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { fatalError("Cannot create bitmap") }
    context.setAllowsAntialiasing(true)
    context.setShouldAntialias(true)
    context.translateBy(x: 0, y: CGFloat(size))
    context.scaleBy(x: CGFloat(size)/512, y: -CGFloat(size)/512)

    func rect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat,
              _ radius: CGFloat, _ fill: CGColor, _ line: CGFloat = 0) {
        let path = CGPath(roundedRect: CGRect(x: x, y: y, width: w, height: h),
                          cornerWidth: radius, cornerHeight: radius, transform: nil)
        context.addPath(path)
        context.setFillColor(fill)
        context.setStrokeColor(ink)
        context.setLineWidth(line)
        context.drawPath(using: line > 0 ? .fillStroke : .fill)
    }
    func polygon(_ points: [(CGFloat, CGFloat)]) {
        context.beginPath()
        context.move(to: CGPoint(x: points[0].0, y: points[0].1))
        for p in points.dropFirst() { context.addLine(to: CGPoint(x: p.0, y: p.1)) }
        context.closePath()
        context.setFillColor(ink)
        context.fillPath()
    }
    rect(48, 48, 416, 416, 100, black)
    polygon([(139,168),(285,168),(285,216),(237,216),(237,344),
             (187,344),(187,216),(139,216)])
    polygon([(316,168),(365,168),(358,284),(323,284)])
    context.setFillColor(ink)
    context.fillEllipse(in: CGRect(x: 317.5, y: 302, width: 46, height: 46))
    guard let image = context.makeImage(),
          let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])
    else { fatalError("Cannot encode PNG") }
    try png.write(to: output.appendingPathComponent(filename))
}

for size in [16, 32, 48, 64, 128, 256, 512, 1024] {
    try render(size, "\(size).png")
}
"""


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "app-icon.svg").write_text(SVG, encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="tibo-icons-") as tmp:
        workspace = Path(tmp)
        source = workspace / "render.swift"
        source.write_text(SWIFT, encoding="utf-8")
        subprocess.run(["swift", str(source), str(workspace)], check=True)
        iconset = workspace / "AppIcon.iconset"
        iconset.mkdir()
        for base in [16, 32, 128, 256, 512]:
            for scale in [1, 2]:
                name = f"icon_{base}x{base}" + ("@2x" if scale == 2 else "") + ".png"
                (iconset / name).write_bytes((workspace / f"{base * scale}.png").read_bytes())
        subprocess.run(["iconutil", "-c", "icns", str(iconset),
                        "-o", str(ROOT / "AppIcon.icns")], check=True)
        # Vista and later support PNG-compressed images directly in ICO files.
        sizes = [16, 32, 48, 64, 128, 256]
        offset = 6 + 16 * len(sizes)
        entries, images = [], []
        for size in sizes:
            data = (workspace / f"{size}.png").read_bytes()
            entries.append(struct.pack("<BBBBHHII", size % 256, size % 256,
                                       0, 0, 1, 32, len(data), offset))
            images.append(data)
            offset += len(data)
        (ROOT / "AppIcon.ico").write_bytes(
            struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries) + b"".join(images)
        )
        (ROOT / "icon-preview.png").write_bytes((workspace / "512.png").read_bytes())
    for name in ["app-icon.svg", "AppIcon.icns", "AppIcon.ico", "icon-preview.png"]:
        path = ROOT / name
        print(f"{name}: {path.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
