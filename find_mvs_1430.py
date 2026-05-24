with open("catbox_log_downloaded.txt", "r", encoding="utf-8") as f:
    lines = f.readlines()

for line in lines:
    parts = line.split()
    if len(parts) >= 2:
        try:
            msg_id = int(parts[1])
            if 6090 <= msg_id <= 6150:
                print(line.strip())
        except ValueError:
            pass
