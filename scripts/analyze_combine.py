from PIL import Image

left_path = r'C:\Users\12619\Desktop\hunter_sprites_clean\hunter_34.png'
right_path = r'C:\Users\12619\Desktop\hunter_sprites_clean\hunter_35.png'

left = Image.open(left_path).convert("RGBA")
right = Image.open(right_path).convert("RGBA")

def analyze(img, name):
    px = img.load()
    w, h = img.size
    # Per-column content (any non-transparent pixel)
    col_has = []
    for x in range(w):
        has = False
        for y in range(h):
            r, g, b, a = px[x, y]
            if a > 30:
                has = True
                break
        col_has.append(has)
    
    # Per-row content
    row_has = []
    for y in range(h):
        has = False
        for x in range(w):
            r, g, b, a = px[x, y]
            if a > 30:
                has = True
                break
        row_has.append(has)
    
    # Leftmost and rightmost content columns
    left_x = next((x for x in range(w) if col_has[x]), None)
    right_x = next((x for x in range(w-1, -1, -1) if col_has[x]), None)
    
    # Topmost and bottommost content rows
    top_y = next((y for y in range(h) if row_has[y]), None)
    bottom_y = next((y for y in range(h-1, -1, -1) if row_has[y]), None)
    
    print(f"\n=== {name} ({w}x{h}) ===")
    print(f"  Content X range: [{left_x}-{right_x}] ({right_x-left_x+1}px wide)")
    print(f"  Content Y range: [{top_y}-{bottom_y}] ({bottom_y-top_y+1}px tall)")
    
    # Show column content density for each column
    print(f"  Column density (non-transparent pixel count per column):")
    for x in range(w):
        cnt = 0
        for y in range(h):
            r, g, b, a = px[x, y]
            if a > 30:
                cnt += 1
        bar = "#" * min(cnt // 5, 40)
        if cnt > 0:
            print(f"    x={x:3d}: {cnt:4d} px {bar}")
    
    return left_x, right_x, top_y, bottom_y

l_cx, l_cx2, l_ty, l_by = analyze(left, "hunter_34 (LEFT)")
r_cx, r_cx2, r_ty, r_by = analyze(right, "hunter_35 (RIGHT)")
