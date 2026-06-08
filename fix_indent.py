import sys

with open('plugins/share_bot.py', encoding='utf-8') as f:
    lines = f.readlines()

out = []
in_try = False
try_idx = -1

for i, line in enumerate(lines):
    if line.strip() == 'try:' and i+2 < len(lines) and lines[i+2].startswith('    sent_ids'):
        in_try = True
        try_idx = i
        out.append(line)
        continue
        
    if line.strip() == 'except Exception as e:' and in_try:
        in_try = False
        out.append(line)
        continue
        
    if in_try and line.strip() != '':
        out.append('    ' + line)
    elif in_try and line.strip() == '':
        out.append('\n')
    else:
        out.append(line)

with open('plugins/share_bot.py', 'w', encoding='utf-8') as f:
    f.writelines(out)
