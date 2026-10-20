"""One-shot CPU-only NudeNet worker. Never imported by the bot process.

The parent enforces a wall deadline and kills/reaps this process. Model and
native runtime memory are released on exit instead of staying in the bot.
"""
import json
import resource
import sys
import time
from pathlib import Path


def main():
    started = time.monotonic()
    import cv2
    import nudenet
    import onnxruntime as ort
    from nudenet import NudeDetector

    cv2.setNumThreads(1)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    # NudeNet 3.4.2 ignores its providers argument and exposes no SessionOptions.
    # Populate the pinned detector's documented detect() inputs explicitly.
    detector = NudeDetector.__new__(NudeDetector)
    detector.onnx_session = ort.InferenceSession(
        str(Path(nudenet.__file__).parent / '320n.onnx'),
        sess_options=options, providers=['CPUExecutionProvider'],
    )
    detector.input_width = detector.input_height = 320
    detector.input_name = detector.onnx_session.get_inputs()[0].name
    detections = detector.detect(sys.stdin.buffer.read(8 * 1024 * 1024))
    print(json.dumps({
        'detections': detections,
        'elapsed_seconds': round(time.monotonic() - started, 3),
        'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }))


if __name__ == '__main__':
    main()
