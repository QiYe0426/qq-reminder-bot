from PIL import Image

LEFT = r"C:\Users\12619\Desktop\hunter_sprites_clean\hunter_34.png"
RIGHT = r"C:\Users\12619\Desktop\hunter_sprites_clean\hunter_35.png"
OUT = r"C:\Users\12619\Desktop\hunter_sprites_clean\hunter_34_35_combined.png"
GAP = 20

def bottom_visible_row(img: Image.Image) -> int:
    """Return the y-coordinate of the bottom-most non-transparent pixel."""
    alpha = img.getchannel("A")
    for y in range(img.height - 1, -1, -1):
        for x in range(img.width):
            if alpha.getpixel((x, y)) > 0:
                return y
    return 0

left = Image.open(LEFT).convert("RGBA")
right = Image.open(RIGHT).convert("RGBA")

left_bottom = bottom_visible_row(left)
right_bottom = bottom_visible_row(right)

# The bottom-most visible pixel of each sprite should sit on the same ground line.
# We'll place them so that their bottom_visible_row aligns.
# Total canvas height = max(content height of either sprite)
left_content_h = left_bottom + 1
right_content_h = right_bottom + 1
canvas_h = max(left_content_h, right_content_h)

# Left sprite: its bottom row goes to canvas_h - 1
left_y = canvas_h - 1 - left_bottom
# Right sprite: same ground line
right_y = canvas_h - 1 - right_bottom

canvas_w = left.width + GAP + right.width

canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
canvas.paste(left, (0, left_y), left)
canvas.paste(right, (left.width + GAP, right_y), right)

canvas.save(OUT)
print(f"Saved: {OUT}")
print(f"Dimensions: {canvas.width} x {canvas.height}")
print(f"Left bottom row: {left_bottom}, Right bottom row: {right_bottom}")
print(f"Left offset: y={left_y}, Right offset: y={right_y}")
