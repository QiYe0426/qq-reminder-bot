from PIL import Image

left_path = r'C:\Users\12619\Desktop\hunter_sprites_clean\hunter_34.png'
right_path = r'C:\Users\12619\Desktop\hunter_sprites_clean\hunter_35.png'

left = Image.open(left_path).convert("RGBA")
right = Image.open(right_path).convert("RGBA")

# Find bottom of actual content for each
def content_bottom(img):
    px = img.load()
    w, h = img.size
    for y in range(h-1, -1, -1):
        for x in range(w):
            if px[x, y][3] > 30:
                return y
    return h-1

l_bot = content_bottom(left)
r_bot = content_bottom(right)
print(f"Left content bottom: {l_bot}, Right content bottom: {r_bot}")
print(f"Left h={left.height}, Right h={right.height}")

# Max height based on taller content
max_h = max(left.height, right.height)

# Place with 0 gap, bottom-aligned by content bottom
combined = Image.new('RGBA', (left.width + 0 + right.width, max_h), (0, 0, 0, 0))

# Left: position so its content bottom aligns with max_h - 1
l_y = max_h - 1 - l_bot
r_y = max_h - 1 - r_bot
print(f"Left y={l_y}, Right y={r_y}")

combined.paste(left, (0, l_y), left)
combined.paste(right, (left.width, r_y), right)

# Tight crop to content
bbox = combined.getbbox()
if bbox:
    combined = combined.crop(bbox)

out_path = r'C:\Users\12619\Desktop\hunter_sprites_clean\hunter_34_35_combined.png'
combined.save(out_path)
print(f"Saved: {out_path}  size={combined.size}")
