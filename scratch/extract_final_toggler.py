import re
import html

filepath = r"C:\Users\User\.gemini\antigravity\brain\2fcc5c9d-21f7-4f60-ba94-b2efb46fed34\.system_generated\steps\895\content.md"

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# Let's search for polygonCollapsed in the document
matches = [m.start() for m in re.finditer(r'polygonCollapsed', content)]
print(f"Matches for 'polygonCollapsed': {matches}")

for m in matches:
    # Find the start of the code block preceding this match
    # Look back for "use client" or "import"
    start_idx = content.rfind('use client', 0, m)
    if start_idx == -1:
        start_idx = content.rfind('import { useCallback', 0, m)
    
    if start_idx != -1:
        # Adjust start_idx to the beginning of the line or code block start
        # Usually it starts around "use client" or "import"
        # Find the end of this code block (```)
        end_idx = content.find('```', m)
        if end_idx != -1:
            code = content[start_idx:end_idx]
            code = html.unescape(code)
            
            # Clean up the code a bit if it has visual backslashes
            code = code.replace('\\n', '\n').replace('\\"', '"').replace('\\t', '\t')
            # If the code starts with 'use client" or similar due to truncation
            if code.startswith('use client"'):
                code = '"use client"\n' + code[11:]
            elif code.startswith('use client'):
                code = '"use client"\n' + code[10:]

            with open('scratch/theme_toggler.tsx', 'w', encoding='utf-8') as out_f:
                out_f.write(code)
            print("Successfully extracted theme toggler to scratch/theme_toggler.tsx")
            break

