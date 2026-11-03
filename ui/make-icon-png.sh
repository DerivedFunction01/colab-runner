#!/usr/bin/env bash
# ui/make-icon-png.sh — rasterize the Colab icon SVG to PNG fallbacks.
#
# The SVG (static/colab-icon.svg) is the tracked source of truth. The
# PNGs are generated, never tracked: run this when a consumer cannot
# take SVG (an artifact library icon slot, an apple-touch-icon) and
# the PNGs land next to the SVG in static/, where ui/deploy.sh picks
# them up when present.
#
#   ui/make-icon-png.sh            # 192, 512, and 180 (apple touch)
#
# Uses the first rasterizer found: rsvg-convert, magick/convert,
# inkscape, ffmpeg (with its svg demuxer). Fails loudly if none is
# available rather than shipping a placeholder.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVG="$HERE/static/colab-icon.svg"
[ -f "$SVG" ] || { echo "make-icon-png: $SVG not found" >&2; exit 2; }

render() { # render <size> <out.png>
  local size="$1" out="$2"
  if command -v rsvg-convert >/dev/null 2>&1; then
    rsvg-convert -w "$size" -h "$size" "$SVG" -o "$out"
  elif command -v magick >/dev/null 2>&1; then
    magick -background none -density 384 "$SVG" -resize "${size}x${size}" "$out"
  elif command -v convert >/dev/null 2>&1; then
    convert -background none -density 384 "$SVG" -resize "${size}x${size}" "$out"
  elif command -v inkscape >/dev/null 2>&1; then
    inkscape "$SVG" --export-type=png --export-filename="$out" -w "$size" -h "$size" >/dev/null 2>&1
  elif command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -hide_banner -loglevel error -y -i "$SVG" -vf "scale=${size}:${size}" -frames:v 1 "$out"
  else
    echo "make-icon-png: no SVG rasterizer found (need rsvg-convert, magick, convert, inkscape, or ffmpeg)" >&2
    exit 1
  fi
  echo "make-icon-png: wrote $out (${size}x${size})"
}

render 192 "$HERE/static/colab-icon-192.png"
render 512 "$HERE/static/colab-icon-512.png"
render 180 "$HERE/static/colab-icon-180.png"
