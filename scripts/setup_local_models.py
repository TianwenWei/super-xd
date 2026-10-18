"""Download the selected Silero model, then verify offline inference."""
if __name__ == "__main__":
    from super_xd.demo.environment import load_local_environment
    load_local_environment()

import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

from super_xd.demo.audio.vad import MODEL_PATH, SileroVAD

VAD_URL = 'https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx'


def main():
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not MODEL_PATH.exists():
        target = MODEL_PATH.with_suffix('.download')
        try:
            with urlopen(VAD_URL, timeout=60) as response, target.open('wb') as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            SileroVAD(target)
            target.replace(MODEL_PATH)
        finally:
            target.unlink(missing_ok=True)
    vad = SileroVAD()
    report = {'vad': {'path': str(MODEL_PATH), 'source': VAD_URL,
                      'sha256': hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest(),
                      'silence_probability': vad.score(bytes(1024))}}
    manifest = MODEL_PATH.parent.parent / 'manifest.json'
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
