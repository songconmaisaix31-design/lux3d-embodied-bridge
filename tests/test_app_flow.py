"""The application half: prepare a run, start it, read the result back.

No engine and no network: a fake ``Popen`` stands in for ``cargo run``, and the
run directory it would have written is produced by the test itself.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from lux3d_bridge import asset, pipeline, server  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "fixtures")


class FakeProcess:
    """Stands in for `cargo run`: prints a few lines and exits 0."""

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.stdout = iter([
            "   Compiling oriera-agent v0.13.0\n",
            "  episode 0 · success=true · steps=700\n",
            "  episode 1 · success=false · steps=700\n",
            "  ✓ 确定性复核（full）：双跑逐位一致\n",
        ])
        self.terminated = False

    def wait(self):
        return 0

    def terminate(self):
        self.terminated = True


class FakeProcessFailure(FakeProcess):
    def wait(self):
        return 101


def make_asset_dir(tmp):
    """A real lock entry from the bundled fixture, in a temp directory."""
    stl_dir = os.path.join(tmp, "meshes")
    entry = asset.ingest(os.path.join(FIXTURES, "cube.glb"), "crate", stl_dir)
    return stl_dir, entry


def write_engine_artifacts(run_dir, episodes=2):
    os.makedirs(run_dir, exist_ok=True)
    report = {
        "task": "pick_place@v1",
        "spec_hash": "deadbeefdeadbeef",
        "gate": "not-applied",
        "protocol": {"id": "course@v1", "episodes": episodes},
        "episodes": episodes,
        "successes": 1,
        "success_rate": 1 / episodes,
        "wilson95": [0.09, 0.91],
        "throughput_steps_per_s": 1234.0,
        "verify_determinism": "full",
        "runs": [
            {"seed": 1000, "success": True, "steps": 700, "reward_sum": 1.5},
            {"seed": 66538, "success": False, "steps": 700, "reward_sum": 0.0, "failure_phase": "grasp"},
        ],
    }
    fingerprints = {
        "spec_hash": "deadbeefdeadbeef",
        "protocol_id": "course@v1",
        "transitions": 1400,
        "fingerprints": {"episode_fingerprints_v2": [1, 2]},
    }
    with open(os.path.join(run_dir, "eval_report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle)
    with open(os.path.join(run_dir, "fingerprints.v2.json"), "w", encoding="utf-8") as handle:
        json.dump(fingerprints, handle)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self._runs = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"LUX3D_RUNS_DIR": self._runs.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._runs.cleanup()

    def test_prepare_writes_mesh_and_urdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            stl_dir, entry = make_asset_dir(tmp)
            run = pipeline.prepare_run(REPO_ROOT, entry, os.path.join(stl_dir, entry["stl_file"]), episodes=2)
            try:
                self.assertTrue(os.path.isfile(os.path.join(run["dir"], "meshes", "crate.stl")))
                urdf_path = os.path.join(run["dir"], "urdf", "desk_arm.urdf")
                with open(urdf_path, "r", encoding="utf-8") as handle:
                    text = handle.read()
                self.assertIn("package://generated/crate.stl", text)
                self.assertIn(entry["asset_id"], text)
                record = run["record"]
                self.assertEqual(record["asset"]["asset_id"], entry["asset_id"])
                self.assertEqual(record["episodes"], 2)
                self.assertEqual(record["command"], [])  # no engine -> no command, not a broken one
            finally:
                import shutil

                shutil.rmtree(run["dir"], ignore_errors=True)

    def test_unknown_asset_is_404(self):
        app = server.App(root=REPO_ROOT, key="")
        with self.assertRaises(server.ApiError) as caught:
            app.prepare({"asset": "nope"})
        self.assertEqual(caught.exception.status, 404)


class TrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"LUX3D_RUNS_DIR": os.path.join(self.tmp.name, "runs")})
        self._env.start()
        self.asset_dir, self.entry = make_asset_dir(self.tmp.name)
        self.engine = os.path.join(self.tmp.name, "engine")
        os.makedirs(os.path.join(self.engine, "crates"), exist_ok=True)
        self.app = server.App(root=REPO_ROOT, key="", engine=self.engine, asset_dirs=[self.asset_dir])

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def _fake_prepare(self, real):
        def prepare(root, record, stl, **kwargs):
            result = real(root, record, stl, **kwargs)
            write_engine_artifacts(result["dir"])
            return result

        return prepare

    def test_train_without_engine_is_409(self):
        app = server.App(root=REPO_ROOT, key="", engine="", asset_dirs=[self.asset_dir])
        with self.assertRaises(server.ApiError) as caught:
            app.train({"asset": "crate"})
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("engine", str(caught.exception))

    def test_train_validates_episodes(self):
        with self.assertRaises(server.ApiError) as caught:
            self.app.train({"asset": "crate", "episodes": 99})
        self.assertIn("episodes", str(caught.exception))

    def test_training_job_reads_the_result_back(self):
        real_prepare = pipeline.prepare_run
        with mock.patch.object(server.pipeline, "prepare_run", side_effect=self._fake_prepare(real_prepare)), \
             mock.patch.object(server.subprocess, "Popen", FakeProcess):
            response = self.app.train({"asset": "crate", "episodes": 2, "seed": 1000})
            job_id = response["job"]["id"]
            deadline = time.time() + 10
            job = None
            while time.time() < deadline:
                job = self.app.jobs.get(job_id).to_json()
                if job["state"] in ("success", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(job["state"], "success", job["message"])
        self.assertEqual(job["kind"], "train")
        self.assertEqual(job["result"]["result"]["successes"], 1)
        self.assertAlmostEqual(job["result"]["result"]["success_rate"], 0.5)
        self.assertEqual(job["result"]["receipt"]["transitions"], 1400)
        self.assertTrue(any("确定性复核" in line for line in job["log"]))
        # the command handed to the engine is the engine's real CLI
        command = job["command"]
        self.assertIn("embodied_sim_collect", command)
        self.assertIn("--urdf-robot", command)
        self.assertIn("--cam", command)

    def test_failed_engine_exit_is_reported_not_swallowed(self):
        real_prepare = pipeline.prepare_run
        with mock.patch.object(server.pipeline, "prepare_run", side_effect=self._fake_prepare(real_prepare)), \
             mock.patch.object(server.subprocess, "Popen", FakeProcessFailure):
            response = self.app.train({"asset": "crate"})
            job_id = response["job"]["id"]
            deadline = time.time() + 10
            job = None
            while time.time() < deadline:
                job = self.app.jobs.get(job_id).to_json()
                if job["state"] in ("success", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(job["state"], "failed")
        self.assertIn("101", job["message"])

    def test_runs_listing_and_frames_path_safety(self):
        runs = self.app.runs()["runs"]
        self.assertIsInstance(runs, list)
        self.assertIsNone(pipeline.frame_path(REPO_ROOT, "x", "../../etc", "episode_000000", "frame_000000.png"))
        self.assertIsNone(pipeline.frame_path(REPO_ROOT, "x", "observation.images.top", "episode_000000", "evil.png"))


if __name__ == "__main__":
    unittest.main()
