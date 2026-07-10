from PIL import Image
import os

# Original source that contains both s5_b03_g00 and s5_b03_g01
# s5 = a84cc60798af7470510dc2f171495b776487305.png
src = r'C:\Users\12619\Desktop\a84cc60798af7470510dc2f171495b776487305.png'

out_dir = r'C:\Users\12619\Desktop\hunter_sprites_clean'
os.makedirs(out_dir, exist_ok=True)

img = Image.open(src).convert("RGBA")
w, h = img.size
pixels = img.load()

def is_bg_white(r, g, b, a, thresh=30):
    if a < 200:
        return True
    return (255 - r) < thresh and (255 - g) < thresh and (255 - b) < thresh

def is_hunter_green(r, g, b):
    if g <= max(r, b) + 5:
        return False
    avg = (r + g + b) / 3
    return 30 <= avg <= 240

def col_content_profile(img, y0, y1):
    """Count non-white pixels per column in y range"""
    px = img.load()
    w = img.width
    cols = []
    for x in range(w):
        cnt = 0
        for y in range(y0, y1):
            r, g, b, a = px[x, y]
            if not is_bg_white(r, g, b, a):
                cnt += 1
        cols.append(cnt)
    return cols

# Find content bands (same as v4)
bands = []
in_band = False
band_start = 0
for y in range(h):
    non_bg = 0
    for x in range(w):
        r, g, b, a = pixels[x, y]
        if not is_bg_white(r, g, b, a):
            non_bg += 1
            break
    if non_bg > 0:
        if not in_band:
            band_start = y
            in_band = True
    else:
        if in_band:
            if y - band_start > 30:
                bands.append((band_start, y))
            in_band = False
if in_band and h - band_start > 30:
    bands.append((band_start, h))

print(f"Found {len(bands)} bands in {src}")

# For band 3 (which has hunter_34 and hunter_35), extract the full content strip
# First let's identify which band contains our frames by looking at green content

# Analyze each band to find hunter content
for bi, (y0, y1) in enumerate(bands):
    band_h = y1 - y0
    cols = col_content_profile(img, y0, y1)
    max_col = max(cols) if cols else 0
    
    # Find content x-range (non-zero columns)
    content_x = [x for x in range(w) if cols[x] > 3]
    if not content_x:
        continue
    
    # Find green columns
    green_cols = [0] * w
    for y in range(y0, y1):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            if a > 30 and is_hunter_green(r, g, b):
                green_cols[x] += 1
    
    green_x = [x for x in range(w) if green_cols[x] > 3]
    if not green_x:
        continue
    
    # Find green ranges (where hunter green is concentrated)
    in_green = False
    green_ranges = []
    gs = 0
    for x in range(w):
        if green_cols[x] > 3:
            if not in_green:
                gs = x
                in_green = True
        else:
            if in_green:
                green_ranges.append((gs, x))
                in_green = False
    if in_green:
        green_ranges.append((gs, w))
    
    # Merge ranges that are close
    merged = []
    for r_start, r_end in green_ranges:
        if merged and r_start - merged[-1][1] < 80:
            merged[-1] = (merged[-1][0], r_end)
        else:
            merged.append((r_start, r_end))
    
    print(f"\nBand {bi} (rows {y0}-{y1}, {band_h}px):")
    print(f"  Content x: {content_x[0]}-{content_x[-1]}")
    print(f"  Green ranges: {merged}")
    
    if bi == 3:
        print("  *** THIS IS THE TARGET BAND ***")
        
        # For the target band, extract ALL the content between the green ranges
        # Instead of splitting, take min_x to max_x of ALL green ranges merged
        if len(merged) >= 2:
            full_start = merged[0][0]
            full_end = merged[-1][1]
            print(f"  Extracting FULL strip: x=[{full_start}-{full_end}]")
            
            # Crop the full strip
            strip = img.crop((full_start, y0, full_end, y1))
            
            # Make white bg transparent
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
                
                out_path = os.path.join(out_dir, f"s5_band3_full.png")
                strip.save(out_path)
                print(f"  Saved: {out_path}  size={strip.size}")
                
                # Now split this full strip into 3 parts:
                # Left = hunter_34 region, Middle = missing strip, Right = hunter_35 region
                # Align by left content boundary
                
            else:
                print("  WARNING: empty strip!")
        elif len(merged) == 1:
            print(f"  Single green range, already extracted in v4")
