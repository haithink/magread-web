"""Optional local faster-whisper worker, isolated from Web request threads."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from kandu import core_config as config, core_parsers as store

WORKER_LOCK = threading.Lock()
MODELS = {'tiny.en', 'base.en', 'small.en', 'medium.en', 'distil-large-v3', 'large-v3', 'large-v3-turbo'}


def settings():
    value = store._json_read(config.app_dir()/'podcasts'/'transcription.json') or {}
    model, device = value.get('model', 'small.en'), value.get('device', 'cpu')
    if model not in MODELS or device not in ('cpu', 'cuda'):
        raise ValueError('本地转写配置无效，请检查 podcasts/transcription.json')
    return dict(model=model, device=device)


def available():
    return importlib.util.find_spec('faster_whisper') is not None


def start_worker(arguments, log):
    return subprocess.Popen(arguments, cwd=str(Path(__file__).resolve().parents[1]),
        stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def stop_worker(process):
    # Windows venv python.exe can spawn a second Python process. Terminate the
    # worker tree so stopping the launcher cannot leave the recognizer running.
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _stamp(seconds):
    value = max(0, int(seconds))
    return f'{value//3600:02}:{value//60%60:02}:{value%60:02}'


def paragraphs(segments):
    """Group unchanged speech into readable paragraphs, preserving timestamps."""
    result, words, texts, start = [], 0, [], 0
    for segment in segments:
        text = segment['text'].strip()
        if not text:
            continue
        if not texts:
            start = segment['start']
        texts.append(text)
        words += len(text.split())
        if words >= 100 or (words >= 55 and text.endswith(('.', '?', '!'))):
            result.append(f'({_stamp(start)}) ' + ' '.join(texts))
            words, texts = 0, []
    if texts:
        result.append(f'({_stamp(start)}) ' + ' '.join(texts))
    return result


def transcribe(path, identity, control, update):
    folder = config.app_dir()/'podcasts'/'transcripts'
    folder.mkdir(parents=True, exist_ok=True)
    result_path = folder/(identity+'.json')
    saved = store._json_read(result_path)
    if saved and saved.get('complete') and saved.get('paragraphs'):
        return saved
    if not available():
        raise ValueError('尚未安装本地转写依赖，请先安装 requirements-transcription.txt')
    options = settings()
    update(status='正在等待本地转写通道')
    while not WORKER_LOCK.acquire(timeout=.25):
        control.check()
    process = None
    progress_path = folder/(identity+'.progress.json')
    try:
        control.check()
        progress_path.unlink(missing_ok=True)
        update(status='正在加载本地语音模型（首次需下载），可随时停止')
        with (folder/(identity+'.worker.log')).open('wb') as log:
            process = start_worker([sys.executable, '-B', '-m', 'webapp.podcast_transcribe',
                str(path), str(result_path), str(progress_path), options['model'], options['device']],
                log)
            started, previous = time.monotonic(), None
            while process.poll() is None:
                control.check()
                progress = store._json_read(progress_path) or {}
                text = progress.get('status')
                if text and text != previous:
                    update(status=text)
                    previous = text
                if time.monotonic()-started > 7200:
                    raise ValueError('本地转写超过两小时，音频已保留，可再次补缺')
                control.event.wait(.25)
            control.check()
            value = store._json_read(result_path)
            if process.returncode or not value or not value.get('complete'):
                raise ValueError('本地转写失败，音频已保留；请检查转写日志和模型配置')
            return value
    finally:
        try:
            if process is not None and process.poll() is None:
                stop_worker(process)
        finally:
            WORKER_LOCK.release()


def worker(audio, result, progress, model_name, device):
    # Plain HTTPS also works on hosts where the optional Xet transport is blocked.
    os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
    # Register pip-installed NVIDIA libraries on Windows without modifying PATH
    # globally. CPU installations do not require CUDA or a separate FFmpeg.
    handles = []
    if os.name == 'nt' and device == 'cuda':
        import site
        for base in site.getsitepackages():
            for folder in (Path(base)/'nvidia').glob('*/bin'):
                handles.append(os.add_dll_directory(str(folder)))
    from faster_whisper import WhisperModel
    store._atomic_json_write(Path(progress), dict(status=f'正在加载 {model_name} · {device}（首次下载后缓存）'))
    options = dict(device=device, compute_type='int8_float16' if device == 'cuda' else 'int8',
        cpu_threads=4, download_root=str(config.app_dir()/'models'/'faster-whisper'))
    try:
        model = WhisperModel(model_name, local_files_only=True, **options)
    except (OSError, ValueError):
        model = WhisperModel(model_name, **options)
    stream, info = model.transcribe(audio, language='en', beam_size=5, vad_filter=True,
        condition_on_previous_text=False, word_timestamps=False)
    if not 0 < info.duration <= 3*3600:
        raise ValueError('音频为空或超过三小时')
    segments, last_update = [], 0
    for segment in stream:
        segments.append(dict(start=segment.start, end=segment.end, text=segment.text.strip()))
        if time.monotonic()-last_update >= 1:
            store._atomic_json_write(Path(progress), dict(status=
                f'本地转写 {_stamp(segment.end)} / {_stamp(info.duration)} · {min(99, int(segment.end/info.duration*100))}%'))
            last_update = time.monotonic()
    text = paragraphs(segments)
    if len(' '.join(text).split()) < 50:
        raise ValueError('未识别到足够的英语正文，未创建空白文章')
    store._atomic_json_write(Path(result), dict(complete=True, model=model_name, device=device,
        language='en', duration=info.duration, segments=segments, paragraphs=text, generatedAt=int(time.time()*1000)))


if __name__ == '__main__':
    worker(*sys.argv[1:])
