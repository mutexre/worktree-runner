#!/usr/bin/env bash
set -euo pipefail

# --- Single source of truth for the image size budget (bytes) ---
MAX_BYTES=512000

DOCS_DIR="$(git rev-parse --show-toplevel)/docs"

if ! command -v pngquant &>/dev/null; then
    echo "ERROR: pngquant is not installed." >&2
    echo "  macOS:        brew install pngquant" >&2
    echo "  Debian/Ubuntu: sudo apt-get install pngquant" >&2
    exit 1
fi

resize_image() {
    local file="$1" width="$2"
    if command -v sips &>/dev/null; then
        sips --resampleWidth "$width" "$file" --out "$file" &>/dev/null
    elif command -v convert &>/dev/null; then
        convert "$file" -resize "${width}x" "$file"
    else
        echo "ERROR: No resize tool available (need sips on macOS or ImageMagick convert on Linux)." >&2
        exit 1
    fi
}

optimise_file() {
    local file="$1"
    local size
    size=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null)

    if [ "$size" -le "$MAX_BYTES" ]; then
        return 0
    fi

    echo "Optimising $(basename "$file") (${size} bytes)..."

    pngquant --quality=80-95 --speed 1 --strip --force --output "$file" "$file"
    size=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null)
    if [ "$size" -le "$MAX_BYTES" ]; then
        echo "  -> $(basename "$file"): ${size} bytes (OK)"
        return 0
    fi

    echo "  Still ${size} bytes; downscaling to 1600px wide..."
    resize_image "$file" 1600
    pngquant --quality=80-95 --speed 1 --strip --force --output "$file" "$file"
    size=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null)
    if [ "$size" -le "$MAX_BYTES" ]; then
        echo "  -> $(basename "$file"): ${size} bytes (OK)"
        return 0
    fi

    echo "  Still ${size} bytes; downscaling to 1200px wide..."
    resize_image "$file" 1200
    pngquant --quality=80-95 --speed 1 --strip --force --output "$file" "$file"
    size=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null)
    if [ "$size" -le "$MAX_BYTES" ]; then
        echo "  -> $(basename "$file"): ${size} bytes (OK)"
        return 0
    fi

    echo "ERROR: $(basename "$file") is still ${size} bytes after all passes (limit: ${MAX_BYTES})." >&2
    return 1
}

shopt -s nullglob
files=("$DOCS_DIR"/*.png)
shopt -u nullglob

if [ ${#files[@]} -eq 0 ]; then
    echo "No PNG files in $DOCS_DIR"
    exit 0
fi

fail=0
for f in "${files[@]}"; do
    if ! optimise_file "$f"; then
        fail=1
    fi
done

exit $fail
