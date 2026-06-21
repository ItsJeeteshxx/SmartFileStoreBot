import re

with open('plugins/jobs.py', 'r', encoding='utf-8') as f:
    text = f.read()

pattern = r"(except FloodWait as fw:\s*# Respect Telegram flood wait fully - account safety\s*logger\.warning\(f\"\[Job \{job_id\}\] FloodWait \{fw\.value\}s in live fetch - waiting\"\)\s*)(await asyncio\.sleep\(fw\.value \+ 2\))"

replacement = r"""\1try:
                    import plugins.arya_logger as arya_log
                    asyncio.create_task(arya_log.log_admin_dm("Live Fetch FloodWait", f"Job {job_id}\nGot FloodWait for {fw.value}s."))
                except: pass
                \2"""

new_text = re.sub(pattern, replacement, text)

if new_text != text:
    with open('plugins/jobs.py', 'w', encoding='utf-8') as f:
        f.write(new_text)
    print("Updated jobs.py successfully.")
else:
    print("Pattern not found in jobs.py")
