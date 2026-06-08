import re
import html

filepath = r"C:\Users\User\.gemini\antigravity\brain\2fcc5c9d-21f7-4f60-ba94-b2efb46fed34\.system_generated\steps\1043\content.md"

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# Let's search for all occurrences of "RainbowButton" or "rainbow" in the text
matches = [m.start() for m in re.finditer(r'RainbowButton|rainbow-button|keyframes|animation', content, re.IGNORECASE)]
print(f"Found {len(matches)} matches.")

# Let's print snippets around matches where there's code-like structures
with open('scratch/temp_rainbow.txt', 'w', encoding='utf-8') as out_f:
    for idx, m_start in enumerate(matches):
        start = max(0, m_start - 300)
        end = min(len(content), m_start + 1500)
        snippet = content[start:end]
        clean_snippet = re.sub(r'<[^>]*>', '', snippet)
        clean_snippet = html.unescape(clean_snippet)
        if 'export' in clean_snippet or 'theme' in clean_snippet or 'animation' in clean_snippet:
            out_f.write(f"\n--- MATCH {idx} (near index {m_start}) ---\n")
            out_f.write(clean_snippet[:1200])
            out_f.write("\n" + "="*50 + "\n")

print("Wrote matches context to scratch/temp_rainbow.txt")
