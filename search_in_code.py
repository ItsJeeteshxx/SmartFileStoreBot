def search_file(filepath, keywords):
    print(f"Searching {filepath} for keywords: {keywords}")
    with open(filepath, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            for kw in keywords:
                if kw.lower() in line.lower():
                    print(f"Line {i}: {line.strip()}")
                    break

search_file("plugins/multijob.py", ["smart", "duplicate", "sort", "def "])
