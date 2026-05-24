def view_lines(filepath, start, end):
    with open(filepath, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            if start <= i <= end:
                # Replace non-ascii chars to avoid cp1252 errors on Windows console
                clean_line = line.encode('ascii', errors='replace').decode('ascii')
                print(f"{i}: {clean_line.strip()}")

view_lines("plugins/multijob.py", 550, 780)
