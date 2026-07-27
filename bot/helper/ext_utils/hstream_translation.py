import argparse
import asyncio
import json
import re
import socket
from collections import deque
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

MODEL_DIR = "/opt/models/nllb-200-distilled-600M-int8"
_PROTECTED = re.compile(r"(\{[^{}]*\}|<[^<>]+>|\\[Nnh]|\r\n|\r|\n)")


def _json_request(url, payload=None, timeout=30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


class _NllbEngine:
    def __init__(self, model_dir):
        import ctranslate2
        from transformers import AutoTokenizer

        self.translator = ctranslate2.Translator(
            model_dir,
            device="cpu",
            compute_type="int8",
            inter_threads=1,
            intra_threads=2,
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_dir,
            src_lang="eng_Latn",
            local_files_only=True,
        )

    def translate(self, texts):
        sources = [
            self.tokenizer.convert_ids_to_tokens(self.tokenizer.encode(text))
            for text in texts
        ]
        results = self.translator.translate_batch(
            sources,
            target_prefix=[["tam_Taml"] for _ in sources],
            beam_size=4,
            max_batch_size=8,
        )
        translated = []
        for result in results:
            tokens = result.hypotheses[0]
            if tokens and tokens[0] == "tam_Taml":
                tokens = tokens[1:]
            translated.append(
                self.tokenizer.decode(
                    self.tokenizer.convert_tokens_to_ids(tokens),
                    skip_special_tokens=True,
                ).strip()
            )
        return translated


def _handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            content = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/translate":
                self._send(404, {"error": "not found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size))
                texts = payload.get("texts") or []
                if not isinstance(texts, list) or len(texts) > 64:
                    raise ValueError("texts must be a list with at most 64 entries")
                self._send(200, {"translations": engine.translate(texts)})
            except Exception as error:
                self._send(500, {"error": str(error)})

        def log_message(self, *_):
            return

    return Handler


def serve(model_dir, port):
    engine = _NllbEngine(model_dir)
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(engine))
    server.serve_forever()


class HstreamTranslator:
    def __init__(self, model_dir=MODEL_DIR):
        self.model_dir = model_dir
        self.process = None
        self.port = None
        self.cache = {}
        self.stderr_lines = deque(maxlen=40)
        self.stderr_task = None

    async def _drain_stderr(self):
        while self.process and self.process.stderr:
            line = await self.process.stderr.readline()
            if not line:
                break
            self.stderr_lines.append(line.decode(errors="ignore").rstrip())

    async def start(self, cancel_event=None):
        if not Path(self.model_dir).is_dir():
            raise RuntimeError(f"NLLB model is missing: {self.model_dir}")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.process = await asyncio.create_subprocess_exec(
            __import__("sys").executable,
            "-m",
            "bot.helper.ext_utils.hstream_translation",
            "--serve",
            "--model-dir",
            self.model_dir,
            "--port",
            str(self.port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        for _ in range(180):
            if cancel_event and cancel_event.is_set():
                await self.stop()
                raise RuntimeError("Hstream translation startup was cancelled")
            if self.process.returncode is not None:
                raise RuntimeError("\n".join(self.stderr_lines)[-1200:])
            try:
                result = await asyncio.to_thread(
                    _json_request, f"http://127.0.0.1:{self.port}/health", None, 2
                )
                if result.get("ok"):
                    return
            except (OSError, URLError, TimeoutError):
                pass
            await asyncio.sleep(1)
        await self.stop()
        raise TimeoutError("NLLB translator did not become ready")

    async def stop(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 15)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.stderr_task:
            with suppress(asyncio.CancelledError):
                await self.stderr_task
            self.stderr_task = None

    async def translate(self, texts):
        missing = []
        for text in texts:
            text = str(text or "").strip()
            if text and text not in self.cache and text not in missing:
                missing.append(text)
        for start in range(0, len(missing), 32):
            batch = missing[start : start + 32]
            error = None
            for attempt in range(3):
                try:
                    result = await asyncio.to_thread(
                        _json_request,
                        f"http://127.0.0.1:{self.port}/translate",
                        {"texts": batch},
                        180,
                    )
                    values = result.get("translations") or []
                    if len(values) != len(batch):
                        raise RuntimeError("translator returned an incomplete batch")
                    self.cache.update(zip(batch, values, strict=True))
                    error = None
                    break
                except Exception as current:
                    error = current
                    await asyncio.sleep(2**attempt)
            if error:
                raise RuntimeError(f"Tamil translation failed: {error}")
        return [self.cache.get(str(text or "").strip(), text) for text in texts]


def _segments(text):
    parts = _PROTECTED.split(text or "")
    indexes = [
        index
        for index, part in enumerate(parts)
        if part and not _PROTECTED.fullmatch(part) and re.search(r"[A-Za-z]", part)
    ]
    return parts, indexes


async def _translate_event_texts(texts, translator):
    parsed = [_segments(text) for text in texts]
    values = [parts[index] for parts, indexes in parsed for index in indexes]
    translations = iter(await translator.translate(values))
    output = []
    for parts, indexes in parsed:
        for index in indexes:
            parts[index] = next(translations)
        output.append("".join(parts))
    return output


async def translate_subtitle(source, target, translator):
    extension = Path(source).suffix.lower()
    if extension == ".vtt":
        import webvtt

        captions = webvtt.read(source)
        translated = await _translate_event_texts(
            [caption.raw_text for caption in captions], translator
        )
        for caption, text in zip(captions, translated, strict=True):
            caption.lines = text.splitlines()
        captions.save(target)
        return target

    import pysubs2

    subtitles = pysubs2.load(source, encoding="utf-8-sig")
    translated = await _translate_event_texts(
        [event.text for event in subtitles.events], translator
    )
    for event, text in zip(subtitles.events, translated, strict=True):
        event.text = text
    subtitles.save(target, encoding="utf-8")
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--model-dir", default=MODEL_DIR)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.serve:
        serve(args.model_dir, args.port)


if __name__ == "__main__":
    main()
