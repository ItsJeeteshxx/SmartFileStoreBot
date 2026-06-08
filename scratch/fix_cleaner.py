import os

fpath = "c:/Users/User/Downloads/AryaBotNew/TryAryaBot/plugins/cleaner.py"

with open(fpath, "r", encoding="utf-8") as f:
    content = f.read()

content = content.replace('"connectionerror", "connection", "reset"', '"connectionerror", "connection", "reset", "broken pipe", "errno 32"')
content = content.replace('"connectionerror", "connection reset", "connection refused"', '"connectionerror", "connection reset", "connection refused", "broken pipe", "errno 32"')

with open(fpath, "w", encoding="utf-8") as f:
    f.write(content)

print("cleaner.py updated")

mpath = "c:/Users/User/Downloads/AryaBotNew/TryAryaBot/plugins/merger.py"
if os.path.exists(mpath):
    with open(mpath, "r", encoding="utf-8") as f:
        mcontent = f.read()
    mcontent = mcontent.replace('"Connection", "Read", "reset",', '"Connection", "Read", "reset", "broken pipe", "errno 32",')
    with open(mpath, "w", encoding="utf-8") as f:
        f.write(mcontent)
    print("merger.py updated")
