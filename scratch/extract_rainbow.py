import re
import html

filepath = r"C:\Users\User\.gemini\antigravity\brain\2fcc5c9d-21f7-4f60-ba94-b2efb46fed34\.system_generated\steps\1043\content.md"

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# Let's search for "RainbowButton" or "rainbow-button" code blocks
matches = [m.start() for m in re.finditer(r'RainbowButton|rainbow-button', content)]
print(f"Found {len(matches)} matches.")

# Let's extract blocks inside <pre> tags that contain button or gradient styling
blocks = re.findall(r'<pre[^>]*>(.*?)</pre>', content, re.DOTALL)
print(f"Found {len(blocks)} pre blocks.")

for idx, b in enumerate(blocks):
    clean_b = html.unescape(re.sub(r'<[^>]*>', '', b))
    if 'RainbowButton' in clean_b and 'button' in clean_b.lower() and ('export' in clean_b or '--color-1' in clean_b):
        print(f"\n--- BLOCK {idx} ---")
        print(clean_b[:2000])
        print("="*40)
