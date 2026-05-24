import os

src_dir = "pocket-arya-store-new/src"
for root, dirs, files in os.walk(src_dir):
    for f in files:
        if f.endswith((".ts", ".tsx", ".json", ".js", ".jsx", ".html")):
            path = os.path.join(root, f)
            try:
                with open(path, "r", encoding="utf-8") as file:
                    content = file.read()
                # Find all characters outside basic ASCII (excluding standard punctuation and whitespace)
                non_ascii = []
                for i, char in enumerate(content):
                    ord_c = ord(char)
                    if ord_c > 127 and ord_c not in range(8192, 8303): # skip some common layout chars
                        # get a snippet around it
                        start = max(0, i - 20)
                        end = min(len(content), i + 20)
                        non_ascii.append((char, content[start:end].replace('\n', ' ')))
                if non_ascii:
                    print(f"File: {path}")
                    for char, snippet in non_ascii[:10]: # print first 10
                        print(f"  Char: {char} (code {ord(char)}) in context: {snippet}")
            except Exception as e:
                pass
