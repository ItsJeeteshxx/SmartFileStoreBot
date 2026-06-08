import os
import re

target = "c:/Users/User/Downloads/AryaBotNew/TryAryaBot/plugins/arya_logger.py"

with open(target, "r", encoding="utf-8") as f:
    content = f.read()

live_batch_post = """
async def log_live_batch_post(
    job_id: str,
    files_in_batch: int,
    total_forwarded: int,
    source: str,
) -> None:
    \"\"\"Log when a Live Job successfully processes a batch of files.\"\"\"
    ts = _ist_str()
    text = (
        f"<b>⚡ Live Job Progress</b>\\n"
        f"━━━━━━━━━━━━━━━━━━━━\\n"
        f"<b>Job ID:</b> <code>{_esc(job_id)}</code>\\n"
        f"<b>Source:</b> <code>{_esc(source)}</code>\\n"
        f"<b>Status:</b> Just processed a batch of <b>{files_in_batch}</b> files.\\n"
        f"<b>Total Forwarded:</b> <code>{total_forwarded}</code>\\n"
        f"<b>Time:</b> <code>{ts}</code>"
    )
    await _send(text, 'ch_live')

"""

if "log_live_batch_post" not in content:
    content += "\n" + live_batch_post

with open(target, "w", encoding="utf-8") as f:
    f.write(content)

lb_target = "c:/Users/User/Downloads/AryaBotNew/TryAryaBot/plugins/live_batch.py"
with open(lb_target, "r", encoding="utf-8") as f:
    lb_content = f.read()

lb_replacement = """
                            await _lb_update_job(job_id, update_dict)
                            job = await _lb_get_job(job_id)
                            logger.info(f"[LiveBatch] Posted batch of {len(chunk_ids)} files. Buffer remaining: {len(buffer_mids)}")
                            try:
                                import plugins.arya_logger as _alog
                                asyncio.create_task(_alog.log_live_batch_post(job_id, len(chunk_ids), fwd_count, str(job.get("source"))))
                            except Exception as e:
                                logger.error(f"Failed to send live batch post log: {e}")
"""
lb_content = lb_content.replace(
    '                            await _lb_update_job(job_id, update_dict)\n                            job = await _lb_get_job(job_id)\n                            logger.info(f"[LiveBatch] Posted batch of {len(chunk_ids)} files. Buffer remaining: {len(buffer_mids)}")',
    lb_replacement.strip('\n')
)

with open(lb_target, "w", encoding="utf-8") as f:
    f.write(lb_content)

print("Updated logger and live_batch.py")
