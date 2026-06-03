import re
import os

path = r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\plugins\cleaner.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Chunk 1
old1 = """# ─── FFmpeg: TURBO (dynaudnorm = single-pass, 10× faster than loudnorm) ──────
def _run_ffmpeg_sync(cmd: list) -> tuple:
    \"\"\"Blocking FFmpeg call — runs in ThreadPoolExecutor thread.\"\"\"
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=2700)
        if r.returncode != 0:
            return False, r.stderr.decode('utf-8', 'ignore')[:500]
        return True, \"\"
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timeout (45m)"
    except Exception as e:
        return False, str(e)


async def _ffmpeg_async(cmd: list) -> tuple:
    \"\"\"Runs _run_ffmpeg_sync in thread pool so event loop stays free for downloads.\"\"\"
    loop = asyncio.get_event_loop()
    async with _cl_ff_sem:
        return await loop.run_in_executor(_FFMPEG_POOL, _run_ffmpeg_sync, cmd)"""

new1 = """# ─── FFmpeg: TURBO (dynaudnorm = single-pass, 10× faster than loudnorm) ──────
async def _ffmpeg_async(cmd: list) -> tuple:
    \"\"\"Runs FFmpeg asynchronously and safely cleans up if cancelled.\"\"\"
    async with _cl_ff_sem:
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2700)
            if process.returncode != 0:
                return False, stderr.decode('utf-8', 'ignore')[:500]
            return True, \"\"
        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            return False, "FFmpeg timeout (45m)"
        except asyncio.CancelledError:
            try:
                process.kill()
            except Exception:
                pass
            raise
        except Exception as e:
            try:
                process.kill()
            except Exception:
                pass
            return False, str(e)"""

content = content.replace(old1, new1)

# Chunk 2
old2 = """                    def _probe_dur_sync(_ppath):
                        try:
                            _pr = __import__("subprocess").run(
                                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "default=noprint_wrappers=1:nokey=1", _ppath],
                                capture_output=True, text=True, timeout=60, stdin=__import__("subprocess").DEVNULL
                            )
                            return float(_pr.stdout.strip() or "0")
                        except Exception:
                            return 0.0"""

new2 = """                    async def _probe_dur_async(_ppath):
                        try:
                            _pr = await asyncio.create_subprocess_exec(
                                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1", _ppath,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                stdin=asyncio.subprocess.DEVNULL
                            )
                            stdout, _ = await asyncio.wait_for(_pr.communicate(), timeout=60)
                            return float(stdout.decode('utf-8').strip() or "0")
                        except Exception:
                            return 0.0"""

content = content.replace(old2, new2)

# Chunk 3
old3 = """                        _dur = await _inj_loop.run_in_executor(_FFMPEG_POOL, _probe_dur_sync, out_path)"""
new3 = """                        _dur = await _probe_dur_async(out_path)"""

content = content.replace(old3, new3)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("Done")
