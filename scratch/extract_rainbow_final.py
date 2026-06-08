import re
import html

filepath = r"C:\Users\User\.gemini\antigravity\brain\2fcc5c9d-21f7-4f60-ba94-b2efb46fed34\.system_generated\steps\1043\content.md"

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

matches = [802357, 807088]

with open('scratch/rainbow_code.tsx', 'w', encoding='utf-8') as out_f:
    for idx, m in enumerate(matches):
        out_f.write(f"\n--- MATCH {idx} (index {m}) ---\n")
        snippet = content[m:m+15000]
        # find the end of code tag or text
        end_idx = snippet.find('```')
        if end_idx == -1:
            end_idx = snippet.find('</pre>')
        if end_idx == -1:
            end_idx = 3000
        code = snippet[:end_idx]
        code = html.unescape(re.sub(r'<[^>]*>', '', code))
        out_f.write(code)
        out_f.write("\n" + "="*50 + "\n")

print("Extracted matches to scratch/rainbow_code.tsx")
