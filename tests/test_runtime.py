import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from helper import boc_runtime as runtime
from helper import boc_whisper_helper as helper


class RuntimeTests(unittest.TestCase):
    def hardware(self, **changes):
        result = dict(cpu_threads=8, memory_gb=16, available_gb=8, gpus=[])
        result.update(changes)
        return result

    def test_model_choice_respects_cpu_memory_and_gpu_capacity(self):
        self.assertEqual(runtime.choose_model(self.hardware()), "small")
        self.assertEqual(runtime.choose_model(self.hardware(cpu_threads=4, memory_gb=8, available_gb=3)), "base")
        self.assertEqual(runtime.choose_model(self.hardware(available_gb=1)), "tiny")
        gpu = dict(memory_gb=4, integrated=False)
        self.assertEqual(runtime.choose_model(self.hardware(cpu_threads=2, gpus=[gpu])), "small")
        self.assertEqual(runtime.choose_model(self.hardware(gpus=[gpu], available_gb=1)), "tiny")
        self.assertEqual(runtime.choose_model(self.hardware(gpus=[dict(memory_gb=16, integrated=True)], available_gb=2)), "base")

    def test_download_is_verified_atomic_and_cached(self):
        content = b"verified model"
        checksum = hashlib.sha256(content).hexdigest()
        def response():
            stream = io.BytesIO(content)
            stream.headers = {"Content-Length": str(len(content))}
            return stream
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.bin"
            path.write_bytes(b"original")
            with patch.object(runtime.urllib.request, "urlopen", return_value=response()):
                with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                    runtime.download("https://example.test/model", path, "0" * 64, lambda *a: None)
            self.assertEqual(path.read_bytes(), b"original")
            self.assertFalse(path.with_suffix(".bin.part").exists())
            with patch.object(runtime.urllib.request, "urlopen", return_value=response()):
                runtime.download("https://example.test/model", path, checksum, lambda *a: None)
            self.assertEqual(path.read_bytes(), content)
            with patch.object(runtime.urllib.request, "urlopen", side_effect=AssertionError("cached model must not download")):
                runtime.download("https://example.test/model", path, checksum, lambda *a: None)

    def test_archive_cannot_escape_install_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("../outside", "..\\outside", "C:/outside", "/outside"):
                with self.subTest(name=name):
                    archive = Path(tmp) / "unsafe.zip"
                    with zipfile.ZipFile(archive, "w") as z:
                        z.writestr(name, "unsafe")
                    with self.assertRaisesRegex(RuntimeError, "不安全"):
                        runtime.extract_zip(archive, Path(tmp) / "extract")

    def test_gpu_success_does_not_run_cpu(self):
        cues = [{"from": 0, "to": 1, "text": "hello"}]
        job = {}
        with patch.object(runtime.boc_gpu, "transcribe", return_value=cues), patch.object(helper, "transcribe") as cpu:
            result = helper.transcribe_with_runtime({"gpu_server": "gpu.exe"}, "audio.wav", "model.bin", "en", 4, lambda *a: None, job=job)
        self.assertEqual(result, cues)
        self.assertEqual(job["backend"], "vulkan")
        cpu.assert_not_called()

    def test_gpu_failure_falls_back_and_records_reason(self):
        job, reports = {}, []
        with patch.object(runtime.boc_gpu, "transcribe", side_effect=RuntimeError("driver error")), patch.object(helper, "transcribe", return_value=["cpu result"]):
            result = helper.transcribe_with_runtime({"gpu_server": "gpu.exe"}, "audio.wav", "model.bin", "en", 4, lambda *a: reports.append(a), job=job)
        self.assertEqual(result, ["cpu result"])
        self.assertEqual(job["backend"], "cpu")
        self.assertEqual(job["fallback_reason"], "driver error")
        self.assertEqual(reports[0][0], "fallback-cpu")

    def test_cancelled_gpu_job_does_not_restart_on_cpu(self):
        with patch.object(runtime.boc_gpu, "transcribe", side_effect=RuntimeError("cancelled")), patch.object(helper, "transcribe") as cpu:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                helper.transcribe_with_runtime({"gpu_server": "gpu.exe"}, "audio.wav", "model.bin", "en", 4, lambda *a: None, job={"_cancelled": True})
        cpu.assert_not_called()

    def test_cpu_auto_language_is_explicit_in_cli_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = str(Path(tmp) / "audio.wav")
            (Path(tmp) / "asr.json").write_text(json.dumps({"transcription": [
                {"text": "test", "offsets": {"from": 0, "to": 1000}}
            ]}), encoding="utf-8")
            with patch.object(helper.subprocess, "Popen") as popen:
                popen.return_value.stderr = []
                popen.return_value.returncode = 0
                result = helper.transcribe(wav, "model.bin", "auto", 4, lambda *a: None)
            args = popen.call_args.args[0]
            self.assertEqual(args[args.index("-l") + 1], "auto")
            self.assertEqual(result[0]["to"], 1)


if __name__ == "__main__":
    unittest.main()
