"""Tests for the WebGL viewer: URDF -> model, and the local JSON API.

Everything here is offline: the dry-run job stops before any network call, and
the HTTP tests talk to a server bound to an ephemeral localhost port.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from lux3d_bridge import robot, server  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "fixtures")
ARM = os.path.join(FIXTURES, "desk_arm.urdf")


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class RobotModelTests(unittest.TestCase):
    def test_fixture_arm(self):
        model = robot.parse_urdf(ARM)
        self.assertEqual(model["robot"], "desk_arm")
        self.assertEqual(len(model["links"]), 4)
        self.assertEqual(len(model["joints"]), 3)
        self.assertEqual(model["dof"], 2)
        self.assertEqual(model["root"], "base_link")
        self.assertEqual(model["warnings"], [])
        self.assertEqual([link["name"] for link in model["links"]], ["base_link", "link1", "link2", "tool"])

    def test_joint_geometry_and_limits(self):
        model = robot.parse_urdf(ARM)
        joint = model["joints"][0]
        self.assertEqual(joint["name"], "joint1")
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual(joint["parent"], "base_link")
        self.assertEqual(joint["child"], "link1")
        self.assertEqual(joint["origin"]["xyz"], [0.0, 0.0, 0.15])
        self.assertEqual(joint["axis"], [0.0, 0.0, 1.0])
        self.assertAlmostEqual(joint["limits"]["lower"], -1.5708, places=6)
        self.assertAlmostEqual(joint["limits"]["upper"], 1.5708, places=6)

    def test_visual_boxes_come_from_the_file(self):
        model = robot.parse_urdf(ARM)
        base = model["links"][0]
        self.assertEqual(base["visuals"][0]["geometry"]["type"], "box")
        self.assertEqual(base["visuals"][0]["geometry"]["size"], [0.12, 0.12, 0.05])
        self.assertEqual(base["mass_kg"], 1.2)

    def test_unknown_parent_link_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                os.path.join(tmp, "bad.urdf"),
                '<robot name="r"><link name="a"/><joint name="j" type="fixed">'
                '<parent link="a"/><child link="ghost"/></joint></robot>',
            )
            with self.assertRaises(robot.UrdfError) as caught:
                robot.parse_urdf(path)
            self.assertIn("ghost", str(caught.exception))

    def test_cycle_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                os.path.join(tmp, "cycle.urdf"),
                '<robot name="r"><link name="a"/><link name="b"/>'
                '<joint name="j1" type="fixed"><parent link="a"/><child link="b"/></joint>'
                '<joint name="j2" type="fixed"><parent link="b"/><child link="a"/></joint></robot>',
            )
            with self.assertRaises(robot.UrdfError):
                robot.parse_urdf(path)

    def test_two_roots_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                os.path.join(tmp, "two.urdf"),
                '<robot name="r"><link name="a"/><link name="b"/></robot>',
            )
            with self.assertRaises(robot.UrdfError) as caught:
                robot.parse_urdf(path)
            self.assertIn("exactly one root", str(caught.exception))

    def test_revolute_without_limits_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                os.path.join(tmp, "nolimit.urdf"),
                '<robot name="r"><link name="a"/><link name="b"/>'
                '<joint name="j" type="revolute"><parent link="a"/><child link="b"/></joint></robot>',
            )
            with self.assertRaises(robot.UrdfError):
                robot.parse_urdf(path)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = server.App(root=REPO_ROOT, profile="global", key="")

    def test_config_shape(self):
        config = self.app.config()
        self.assertTrue(config["ok"])
        self.assertEqual(config["profile"], "global")
        self.assertIn("G1-Turbo", config["versions"])
        self.assertEqual(config["face_range"], [10000, 300000])

    def test_robot_endpoint_returns_the_model(self):
        model = self.app.robot_model(None)
        self.assertTrue(model["ok"])
        self.assertEqual(model["summary"], "desk_arm (desk_arm.urdf): 4 links, 3 joints, 2 DoF")

    def test_paths_outside_the_repo_are_refused(self):
        with self.assertRaises(server.ApiError) as caught:
            self.app.robot_model("../../../etc/passwd")
        self.assertEqual(caught.exception.status, 403)

    def test_assets_resolve_the_glb_by_identity(self):
        listing = self.app.assets()
        self.assertTrue(listing["ok"])
        if not listing["assets"]:
            self.skipTest("no out/ assets in this checkout")
        for record in listing["assets"]:
            self.assertIn("asset_id", record)
            self.assertIn("bbox_m", record)
        crate = next((item for item in listing["assets"] if item["name"] == "crate_live"), None)
        if crate is None:
            self.skipTest("crate_live is not in this checkout")
        self.assertTrue(crate["has_glb"], "the GLB is resolved through sha256, not the file name")
        path, content_type = self.app.asset_bytes("crate_live", "glb")
        self.assertEqual(content_type, "model/gltf-binary")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(4), b"glTF")

    def test_stl_bytes_are_the_collision_mesh(self):
        crate = next((item for item in self.app.assets()["assets"] if item["name"] == "crate_live"), None)
        if crate is None:
            self.skipTest("crate_live is not in this checkout")
        self.assertTrue(crate["has_stl"])
        path, content_type = self.app.asset_bytes("crate_live", "stl")
        self.assertEqual(content_type, "model/stl")
        self.assertEqual(os.path.getsize(path), crate["stl_bytes"])

    def test_unknown_asset_is_404(self):
        with self.assertRaises(server.ApiError) as caught:
            self.app.asset_bytes("nope", "glb")
        self.assertEqual(caught.exception.status, 404)

    def test_receipts_come_back_with_a_pair_verdict(self):
        listing = self.app.assets()
        if len(listing["receipts"]) < 2:
            self.skipTest("needs two receipts in this checkout")
        self.assertEqual(listing["pairs"][0]["match"], True)
        self.assertIsNone(listing["pairs"][0]["first_difference"])

    def test_generate_validation(self):
        cases = [
            ({"prompt": ""}, "prompt is required"),
            ({"prompt": "x", "face_count": 5}, "face_count"),
            ({"prompt": "x", "name": "../evil"}, "name must match"),
            ({"prompt": "x", "version": "G2"}, "version"),
            ({"prompt": "x", "mode": "text-to-video"}, "mode"),
        ]
        for payload, expected in cases:
            with self.assertRaises(server.ApiError) as caught:
                self.app.generate({**payload, "dry_run": True})
            self.assertIn(expected, str(caught.exception))

    def test_missing_key_refuses_a_real_job(self):
        app = server.App(root=REPO_ROOT, key="")
        app.key = ""
        with self.assertRaises(server.ApiError) as caught:
            app.generate({"prompt": "x", "dry_run": False})
        self.assertEqual(caught.exception.status, 409)

    def test_dry_run_job_finishes_without_network(self):
        app = server.App(root=REPO_ROOT, key="")
        job = app.generate({"prompt": "a crate", "dry_run": True, "name": "dry"})["job"]
        deadline = time.time() + 10
        while time.time() < deadline:
            current = app.jobs.get(job["id"]).to_json()
            if current["state"] in ("success", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(current["state"], "success")
        self.assertEqual(current["payload_json"]["faceCount"], 20000)
        self.assertTrue(any("DRY-RUN" in line for line in current["log"]))


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = server.App(root=REPO_ROOT, key="")
        cls.server = server.Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get_json(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as response:
            return response.status, json.load(response)

    def test_health_and_config(self):
        with urllib.request.urlopen(self.url("/healthz"), timeout=10) as response:
            self.assertEqual(response.read().strip(), b"ok")
        status, body = self.get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_static_index_is_served(self):
        with urllib.request.urlopen(self.url("/"), timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("机械臂工作台", html)
        self.assertIn("importmap", html)
        self.assertIn('id="panel-train"', html)
        with urllib.request.urlopen(self.url("/index.html"), timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("LUX3D", html)
        self.assertIn("data-panel=\"train\"", html)

    def test_unknown_route_is_json_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url("/api/nope"), timeout=10)
        self.assertEqual(caught.exception.code, 404)
        body = json.load(caught.exception)
        self.assertFalse(body["ok"])

    def test_generate_dry_run_over_http(self):
        request = urllib.request.Request(
            self.url("/api/generate"),
            data=json.dumps({"prompt": "a crate", "dry_run": True, "name": "httpdry"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 202)
            job = json.load(response)["job"]
        deadline = time.time() + 10
        state = None
        while time.time() < deadline:
            _, body = self.get_json(f"/api/jobs/{job['id']}")
            state = body["job"]["state"]
            if state in ("success", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(state, "success")


if __name__ == "__main__":
    unittest.main()
