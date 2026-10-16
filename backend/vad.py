"""Silero v5 ONNX at 16 kHz; one recurrent state per microphone connection."""
from collections import deque
import importlib.util
import math
from pathlib import Path

from backend.settings import ROOT

MODEL_PATH = ROOT / 'data/models/silero-vad/silero_vad_v5.onnx'
FRAME_SAMPLES = 512
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_MS = 32


def vad_info():
    installed = all(importlib.util.find_spec(name) for name in ('onnxruntime', 'numpy'))
    return {'configured': installed and MODEL_PATH.is_file(), 'model': 'silero-vad-v5.1.2',
            'runtime': 'onnxruntime-cpu', 'sample_rate': 16000, 'frame_ms': FRAME_MS,
            'start_ms': 96, 'silence_ms': 640, 'preroll_ms': 320}


class SileroVAD:
    def __init__(self, path: Path = MODEL_PATH):
        if not path.is_file():
            raise RuntimeError('本地 Silero VAD 尚未下载，请运行 scripts.setup_local_models。')
        import numpy as np
        import onnxruntime as ort
        self.np = np
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), options, providers=['CPUExecutionProvider'])
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)

    def score(self, pcm: bytes) -> float:
        if len(pcm) != FRAME_BYTES:
            raise ValueError('VAD 每帧必须是32ms / 16kHz / 单声道16位PCM。')
        np = self.np
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32).reshape(1, -1) / 32768
        data = np.concatenate((self.context, audio), axis=1)
        probability, self.state = self.session.run(None, {
            'input': data, 'state': self.state, 'sr': np.array(16000, dtype=np.int64)})
        self.context = audio[:, -64:].copy()
        return float(probability[0, 0])


class SpeechGate:
    def __init__(self):
        self.preroll = deque(maxlen=10)
        self.speaking = False
        self.voiced = 0
        self.silent = 0

    def feed(self, pcm: bytes, probability: float) -> dict:
        if len(pcm) != FRAME_BYTES or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError('无效 VAD 帧或语音概率。')
        if not self.speaking:
            self.preroll.append(pcm)
            self.voiced = self.voiced + 1 if probability >= .6 else 0
            if self.voiced >= 3:
                self.speaking, self.silent = True, 0
                audio = list(self.preroll)
                self.preroll.clear()
                return {'event': 'start', 'audio': audio}
            return {'event': None, 'audio': []}
        # The lower exit threshold tolerates uncertain frames inside speech.
        self.silent = self.silent + 1 if probability < .35 else 0
        if self.silent >= 20:
            self.speaking, self.voiced, self.silent = False, 0, 0
            return {'event': 'end', 'audio': [pcm]}
        return {'event': None, 'audio': [pcm]}
