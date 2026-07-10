from PIL import Image

left_path = r'C:\Users\12619\Desktop\hunter_sprites_clean\hunter_34.png'
right_path = r'C:\Users\12619\Desktop\hunter_sprites_clean\hunter_35.png'

left = Image.open(left_path).convert("RGBA")
right = Image.open(right_path).convert("RGBA")

# Max height = taller of the two
max_h = max(left.height, right.height)
gap = 20  # gap between them

combined = Image.new('RGBA', (left.width + gap + right.width, max_h), (0, 0, 0, 0))

# Paste left, vertically centered
left_y = (max_h - left.height) // 2
combined.paste(left, (0, left_y), left)

# Paste right, vertically centered
right_y = (max_h - right.height) // 2
combined.paste(right, (left.width + gap, right_y), right)

out_path = r'C:\Users\12619\Desktop\hunter_sprites_clean\hunter_34_35_combined.png'
combined.save(out_path)
print(f"Saved: {out_path}")
print(f"  left:  {left.size}")
print(f"  right: {right.size}")
print(f"  combined: {combined.size}")
