"""Tests for the offline half of the bridge (no network, no engine needed)."""

import json
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from lux3d_bridge import aholo, asset, bench, glb, stl, urdf, verify  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "fixtures")


class GlbTests(unittest.TestCase):
    def test_cube_triangles_and_bbox(self):
        result = glb.load_glb_triangles(os.path.join(FIXTURES, "cube.glb"))
        self.assertEqual(result.stats["triangles"], 12)
        self.assertEqual(result.stats["meshes"], 1)
        self.assertEqual(result.stats["skipped"], [])
        lo, hi = result.bbox
        for axis in range(3):
            self.assertAlmostEqual(lo[axis], -0.5, places=6)
            self.assertAlmostEqual(hi[axis], 0.5, places=6)

    def test_rejects_non_glb(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "not.glb")
            with open(path, "wb") as handle:
                handle.write(b"solid not a glb")
            with self.assertRaises(glb.GlbError):
                glb.load_glb_triangles(path)


class IngestTests(unittest.TestCase):
    def test_ingest_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = asset.ingest(os.path.join(FIXTURES, "cube.glb"), "crate", tmp)
            second = asset.ingest(os.path.join(FIXTURES, "cube.glb"), "crate", tmp)
            self.assertEqual(first["asset_id"], second["asset_id"])
            self.assertEqual(first["stl_sha256"], second["stl_sha256"])
            self.assertEqual(first["triangles"], 12)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "crate.stl")))
            with open(os.path.join(tmp, "assets.lock.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(manifest["schema"], asset.MANIFEST_SCHEMA)
            self.assertIn("crate", manifest["assets"])

    def test_stl_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "crate.stl")
            result = glb.load_glb_triangles(os.path.join(FIXTURES, "cube.glb"))
            stl.write_binary_stl(path, result.triangles)
            back = stl.read_binary_stl(path)
            self.assertEqual(len(back), 12)
            self.assertAlmostEqual(back[0][0][0], result.triangles[0][0][0], places=6)

    def test_unsupported_extension_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "thing.fbx")
            open(path, "wb").close()
            with self.assertRaises(ValueError) as ctx:
                asset.ingest(path, "thing", tmp)
            self.assertIn(".fbx", str(ctx.exception))


class UrdfTests(unittest.TestCase):
    def test_injection_replaces_box_and_stamps_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "desk_arm.urdf")
            report = urdf.inject(
                os.path.join(FIXTURES, "desk_arm.urdf"),
                out,
                package="generated",
                mesh_file="crate.stl",
                link_name="tool",
                asset_id="deadbeefdeadbeef",
                asset_name="crate",
            )
            self.assertEqual(report["link"], "tool")
            self.assertEqual(report["replaced"], ["box"])
            self.assertEqual(report["mesh_uri"], "package://generated/crate.stl")
            with open(out, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("deadbeefdeadbeef", text)
            self.assertIn('filename="package://generated/crate.stl"', text)

    def test_unknown_link_lists_candidates(self):
        with self.assertRaises(ValueError) as ctx:
            urdf.inject(
                os.path.join(FIXTURES, "desk_arm.urdf"),
                os.path.join(tempfile.gettempdir(), "unused.urdf"),
                package="generated",
                mesh_file="crate.stl",
                link_name="no_such_link",
            )
        self.assertIn("base_link", str(ctx.exception))


class BenchTests(unittest.TestCase):
    def test_command_carries_the_bridge_paths(self):
        command = bench.build_command(
            engine_dir="/engine",
            urdf_robot="/out/urdf/desk_arm.urdf",
            mesh_base="/out/meshes",
            mesh_package="generated",
            dump_robot_spec="/out/robot.json",
            episodes=2,
            fingerprints="/out/fingerprints.v2.json",
        )
        joined = bench.render(command)
        self.assertIn("--urdf-robot /out/urdf/desk_arm.urdf", joined)
        self.assertIn("--mesh-package generated=/out/meshes", joined)
        self.assertIn("--verify-determinism full", joined)
        self.assertIn("--features rigid3d,cam,spec,urdf", joined)


class BenchModeTests(unittest.TestCase):
    def test_verify_mode_is_always_a_value(self):
        command = bench.build_command(
            engine_dir="/engine",
            urdf_robot="/out/urdf/desk_arm.urdf",
            mesh_base="/out/meshes",
            mesh_package="generated",
            verify_determinism="sample",
        )
        self.assertEqual(command[-2:], ["--verify-determinism", "sample"])

    def test_bool_is_normalised(self):
        self.assertIn("full", bench.build_command("/e", "/u", "/m", "p", verify_determinism=True))
        self.assertIn("off", bench.build_command("/e", "/u", "/m", "p", verify_determinism=False))

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            bench.build_command("/e", "/u", "/m", "p", verify_determinism="sometimes")


class VerifyTests(unittest.TestCase):
    def test_divergence_is_located(self):
        with tempfile.TemporaryDirectory() as tmp:
            left = {"scheme": "oriera-embodied-v2", "spec_hash": "aaa", "episodes": [{"seed": 1000}]}
            right = {"scheme": "oriera-embodied-v2", "spec_hash": "aaa", "episodes": [{"seed": 1001}]}
            a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
            with open(a, "w", encoding="utf-8") as handle:
                json.dump(left, handle)
            with open(b, "w", encoding="utf-8") as handle:
                json.dump(right, handle)
            same, where = verify._first_divergence(left, right)
            self.assertFalse(same)
            self.assertIn("episodes[0].seed", where)


class AholoClientTests(unittest.TestCase):
    """The wire contract, checked without touching the network."""

    def test_envelope_is_unwrapped(self):
        self.assertEqual(aholo.unwrap({"c": "0", "m": "ok", "d": 8389873}), 8389873)
        self.assertEqual(aholo.unwrap({"c": "0", "d": {"taskId": 7}}), {"taskId": 7})

    def test_business_error_is_raised_not_ignored(self):
        with self.assertRaises(aholo.AholoError):
            aholo.unwrap({"c": "10400", "m": "faceCount out of range"})
        with self.assertRaises(aholo.AholoError):
            aholo.unwrap({"c": "0", "m": "empty"})

    def test_integer_statuses(self):
        self.assertEqual(aholo.status_name(0), "init")
        self.assertEqual(aholo.status_name(1), "running")
        self.assertEqual(aholo.status_name(3), "success")
        self.assertEqual(aholo.status_name(4), "failed")
        self.assertEqual(aholo.status_name(6), "canceled")
        self.assertEqual(aholo.status_name(99), "status-99")

    def test_outputs_and_format_preference(self):
        task = {
            "taskId": 1,
            "status": 3,
            "outputs": [
                {"content": "https://cdn.example.com/a.zip"},
                {"content": "https://cdn.example.com/b.glb"},
            ],
        }
        urls = aholo.output_urls(task)
        self.assertEqual(len(urls), 2)
        self.assertEqual(aholo.pick_output(urls, ".glb"), "https://cdn.example.com/b.glb")
        self.assertIsNone(aholo.output_urls({"status": 1, "outputs": []}) or None)

    def test_dry_run_sends_nothing(self):
        payload = aholo.default_payload("a crate", None, "G1-Turbo", 30000, "glb", True, "photorealistic")
        self.assertEqual(payload["faceCount"], 30000)
        self.assertEqual(payload["style"], "photorealistic")
        self.assertEqual(payload["outputFormat"], ["glb"])
        task_id = aholo.create_task("global", "", payload, "text-to-3d", dry_run=True)
        self.assertEqual(task_id, "dry-run-task")


if __name__ == "__main__":
    unittest.main(verbosity=2)
