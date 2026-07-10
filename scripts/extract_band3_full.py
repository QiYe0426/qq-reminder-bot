from PIL import Image
from pathlib import Path

src = r'C:\Users\12619\Desktop\a84cc60798af7470510dc2f171495b776487305.png'
out_dir = Path(r'C:\Users\12619\Desktop\hunter_sprites_clean')

img = Image.open(src).convert("RGBA")
w, h = img.size
pixels = img.load()

def is_bg_white(r, g, b, a, thresh=30):
    if a < 200:
        return True
    return (255 - r) < thresh and (255 - g) < thresh and (255 - b) < thresh

# Band 3: rows 5673-6838, green range x=282-984
y0, y1 = 5673, 6838
x_start, x_end = 282, 984

print(f"Extracting band 3: rows [{y0}-{y1}], columns [{x_start}-{x_end}]")
strip = img.crop((x_start, y0, x_end, y1))
print(f"  Raw strip size: {strip.size}")

# Make white background transparent
sp = strip.load()
sw, sh = strip.size
for y in range(sh):
    for x in range(sw):
        r, g, b, a = sp[x, y]
        if is_bg_white(r, g, b, a, 35):
            a = 0
        sp[x, y] = (r, g, b, a)

# Tight crop
bbox = strip.getbbox()
if bbox:
    strip = strip.crop(bbox)
    print(f"  After tight crop: {strip.size}")

out_path = out_dir / 's5_band3_full.png'
strip.save(out_path)
print(f"  Saved: {out_path}")

# Now also extract the FULL strip without tight cropping (keep original spacing)
# This preserves the original green range width
strip2 = img.crop((x_start, y0, x_end, y1))
sp2 = strip2.load()
sw2, sh2 = strip2.size
for y in range(sh2):
    for x in range(sw2):
        r, g, b, a = sp2[x, y]
        if is_bg_white(r, g, b, a, 35):
            a = 0
        sp2[x, y] = (r, g, b, a)

out_path2 = out_dir / 's5_band3_full_uncropped.png'
strip2.save(out_path2)
print(f"  Saved uncropped: {out_path2}  size={strip2.size}")
