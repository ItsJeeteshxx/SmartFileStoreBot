import sys
file_path = "plugins/settings.py"
with open(file_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

for i in range(903, 1029): # lines 904 to 1029
    if lines[i].startswith(" "):
        # Check if it starts with 3 spaces but not 4 (wait, we just want to remove 1 space)
        # Actually, let's just remove 1 space from every line that has at least 1 space
        lines[i] = lines[i][1:]

with open(file_path, "w", encoding="utf-8") as f:
    f.writelines(lines)
