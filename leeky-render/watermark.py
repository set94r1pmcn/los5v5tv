"""Burn a moving watermark into cached copies. Never serve an unprocessed source."""
import hashlib, json, os, shutil, subprocess, threading
from pathlib import Path

_LOCK=threading.Lock()

def watermarked(source, cache, preview=True):
    source=Path(source)
    stat=source.stat()
    identity=f'v5:{preview}:{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}'
    name=hashlib.sha256(identity.encode()).hexdigest()+'.mp4'
    cache=Path(cache);cache.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=cache/name
    if target.is_file():return target
    with _LOCK:
        if target.is_file():return target
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg and Path('/opt/homebrew/bin/ffmpeg').exists():ffmpeg='/opt/homebrew/bin/ffmpeg'
        if not ffmpeg:raise RuntimeError('FFmpeg is required to watermark videos.')
        temporary=cache/(name+'.working.mp4')
        # Center the preview notice and move it vertically at constant opacity.
        probe=shutil.which('ffprobe') or str(Path(ffmpeg).with_name('ffprobe'))
        info=json.loads(subprocess.check_output([probe,'-v','error','-select_streams','v:0',
             '-show_entries','stream=width:format=duration','-of','json',str(source)],timeout=30))
        mark_width=max(1,round(info['streams'][0]['width']*(.90 if preview else .2)))
        duration=float(info.get('format',{}).get('duration',0))
        if preview and duration<=0:raise RuntimeError('Cannot determine preview duration.')
        effect=f"gblur=sigma=55:steps=3:enable='gte(t,{10:.6f})'" if preview else 'null'
        graph=("[0:v]scale=ceil(iw/2)*2:ceil(ih/2)*2[base];"
               f"[1:v]scale={mark_width}:-1[mark];[base]{effect}[video];"
               "[video][mark]overlay=x='(W-w)/2':"
               "y='(H-h)*(0.5+0.43*sin(t/5))':shortest=1[out]")
        command=[ffmpeg,'-hide_banner','-loglevel','error','-y','-i',str(source),
                 '-loop','1','-i',str(Path(__file__).with_name('watermark-preview.png' if preview else 'watermark.png')),
                 '-filter_complex',graph,'-map','[out]','-map','0:a?',
                 '-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p',
                 '-c:a','aac','-b:a','192k','-movflags','+faststart','-shortest',str(temporary)]
        try:
            result=subprocess.run(command,capture_output=True,timeout=21600)
            if result.returncode:raise RuntimeError('Video watermark processing failed: '+result.stderr.decode(errors='replace')[-1000:])
            os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)
    return target
