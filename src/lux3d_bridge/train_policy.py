import argparse
import bisect
import importlib.machinery
import importlib.util
import json
import math
import subprocess
from pathlib import Path

import torch
from torch import nn

from workbench_config import COLLECTION_EPISODES, COLLECTION_SEED, EVAL_EPISODES, EVAL_SEED, MIN_SUCCESSES, VALIDATION_SEED

VALIDATION_EPISODES = 6
REGULARIZATION = (1e-7, 4e-7, 1e-6, 4e-6, 1e-5, 4e-5, 1e-4)


class Cancelled(Exception):
    pass


def emit(phase, **data):
    print("POLICY " + json.dumps({"phase": phase, **data}, ensure_ascii=False), flush=True)


def check_cancel(directory):
    if (directory / "cancel.requested").exists():
        raise Cancelled()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    staged = path.with_suffix(".tmp")
    staged.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    staged.replace(path)


def load_native(path):
    loader = importlib.machinery.ExtensionFileLoader("oriera_sim", str(path))
    spec = importlib.util.spec_from_file_location("oriera_sim", path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class BrowserEngine:
    def __init__(self, process, spec_hash):
        self.process = process
        self.spec_hash = spec_hash

    def request(self, command, **payload):
        self.process.stdin.write(json.dumps({"type": command, **payload}, allow_nan=False) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("lux3D-engine 的 WebAssembly 评估进程提前退出。")
        return json.loads(line)

    def reset(self, seed):
        return self.request("reset", seed=seed)["obs"]

    def step(self, action):
        result = self.request("step", action=action)
        self.position = result["objectPosition"]
        self.held = result["holding"]
        return tuple(result[key] for key in ("obs", "reward", "terminated", "truncated", "success"))

    def cube_pos(self):
        return self.position

    def holding(self):
        return self.held

    def audit_result(self):
        result = self.request("end")
        if result["specHash"] != self.spec_hash:
            raise ValueError("训练场景与 WebAssembly 评估场景不一致。")
        return result


def collect(directory, collector):
    emit("collecting", completed=0, total=COLLECTION_EPISODES)
    target = directory / "demonstrations"
    cmd = [
        str(collector), "--spec", str(directory / "scene.json"), "--out", str(target),
        "--no-cam", "--gate", "report", "--verify-determinism", "off",
        "--episodes", str(COLLECTION_EPISODES), "--seed", str(COLLECTION_SEED),
    ]
    log_path = directory / "collection.log"
    with log_path.open("w", encoding="utf-8") as log:
        with subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT) as child:
            try:
                while child.poll() is None:
                    check_cancel(directory)
                    try:
                        child.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        continue
            except Cancelled:
                child.terminate()
                child.wait()
                raise
    if child.returncode:
        detail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        raise RuntimeError("lux3D-engine 采集失败。\n" + "\n".join(detail[-8:]))
    report = read_json(target / "eval_report.json")
    if report["task_id"] != "pick_place@v2" or len(report["runs"]) != COLLECTION_EPISODES:
        raise ValueError("采集回执与场景任务不一致。")
    emit("collecting", completed=report["episodes"], total=COLLECTION_EPISODES, successes=report["successes"])
    if report["successes"] < MIN_SUCCESSES:
        raise ValueError(f"脚本只完成 {report['successes']}/{report['episodes']} 次抓放。请检查资产比例和夹取宽度。")
    return target, report


def load_demonstrations(target, report, obs_count, action_count):
    files = sorted((target / "lerobot-v21" / "data").rglob("episode_*.jsonl"))
    if len(files) != len(report["runs"]):
        raise ValueError("采集回执与轨迹文件数量不一致。")
    episodes = []
    for index, (path, info) in enumerate(zip(files, report["runs"])):
        if not info["success"]:
            continue
        x, y = [], []
        with path.open(encoding="utf-8") as handle:
            for frame, line in enumerate(handle):
                row = json.loads(line)
                obs, action = row["observation.state"], row["action"]
                if (row["episode_index"] != index or row["frame_index"] != frame
                        or len(obs) != obs_count or len(action) != action_count
                        or not all(math.isfinite(v) for v in obs + action)):
                    raise ValueError(f"示范轨迹 {path.name} 的帧 {frame} 无效。")
                x.append(obs + [frame])
                y.append(action)
        if len(x) != info["steps"]:
            raise ValueError(f"示范轨迹 {path.name} 的帧数和回执不一致。")
        episodes.append((torch.tensor(x, dtype=torch.float64), torch.tensor(y, dtype=torch.float64)))
    return episodes


def phase_boundaries(episodes):
    length = max(len(action) for _, action in episodes)
    seen = torch.zeros(length)
    votes = torch.zeros(length)
    for _, action in episodes:
        jumps = (action[1:] - action[:-1]).abs().amax(dim=1)
        seen[1:len(action)] += 1
        votes[1:len(action)] += jumps >= 0.075
    return torch.where((seen >= len(episodes) * 0.5) & (votes >= seen * 0.75))[0].tolist()


class Policy(nn.Module):
    def __init__(self, obs_names, action_count, boundaries):
        super().__init__()
        self.ee = [obs_names.index(f"ee.{axis}") for axis in "xyz"]
        self.object_relative = [obs_names.index(f"obj_rel.{axis}") for axis in "xyz"]
        self.goal_relative = [obs_names.index(f"goal_rel.{axis}") for axis in "xyz"]
        self.boundaries = boundaries
        phases = len(boundaries) + 1
        self.a, self.b = torch.triu_indices(6, 6)
        self.register_buffer("mean", torch.zeros(phases, 6, dtype=torch.float64))
        self.register_buffer("scale", torch.ones_like(self.mean))
        self.register_buffer("weights", torch.zeros(phases, 28, action_count, dtype=torch.float64))

    def positions(self, obs):
        ee = obs[..., self.ee]
        return torch.cat([ee + obs[..., self.object_relative], ee + obs[..., self.goal_relative]], dim=-1)

    def features(self, position, phase):
        x = ((position - self.mean[phase]) / self.scale[phase]).clamp(-5, 5)
        constant = torch.ones_like(x[..., :1])
        return torch.cat([constant, x, x[..., self.a] * x[..., self.b]], dim=-1)

    def forward(self, obs, step):
        phase = bisect.bisect_right(self.boundaries, step)
        return self.features(self.positions(obs), phase) @ self.weights[phase]


@torch.inference_mode()
def rollout(env, model, spec, seed, *, record=False):
    obs = env.reset(seed)
    max_steps = spec["max_steps"]
    prop = spec["scene"]["props"][0]
    table = next(item for item in spec["scene"]["fixtures"] if item["name"] == "table")
    resting_y = table["center"][1] + table["half"][1] + prop["shape"]["half"][1]
    lifted = False
    actions = []
    reward = 0.0
    joints_ok = [(joint["limits"][0], joint["limits"][1]) for joint in spec["robot"]["joints"]]
    success = False
    for frame in range(max_steps):
        raw = model(torch.tensor(obs, dtype=torch.float64), frame).tolist()
        action = [max(low, min(high, cmd)) for cmd, (low, high) in zip(raw[:-1], joints_ok)]
        action.append(float(raw[-1] >= 0.5))
        obs, value, terminated, truncated, success = env.step(action)
        reward += value
        lifted |= env.holding() and env.cube_pos()[1] > resting_y + 0.025
        if record:
            actions.append(action)
        if terminated or truncated:
            break
    data = {
        "seed": seed, "steps": frame + 1, "success": bool(success and lifted),
        "lifted": lifted, "settled_in_goal": bool(success),
        "reward": reward,
    }
    if record:
        audit = env.audit_result()
        data["fingerprint"] = audit["fingerprint"]
        data["contact"] = audit["contact"]
        data["success"] &= audit["contact"]["penetrating_frames"] == 0
        data["actions"] = actions
    return data


def train(directory, episodes, env, spec):
    torch.set_num_threads(2)
    training = [item for i, item in enumerate(episodes) if i % 6 != 0]
    validation = [item for i, item in enumerate(episodes) if i % 6 == 0]
    boundaries = phase_boundaries(training)
    model = Policy(env.obs_names, len(env.action_names), boundaries)
    x, y = map(torch.cat, zip(*training))
    vx, vy = map(torch.cat, zip(*validation))
    print(f"从示范中识别到 {len(boundaries) + 1} 段动作，分界步为 {boundaries}。", flush=True)
    matrices = []
    starts = [0, *boundaries]
    ends = [*boundaries, spec["max_steps"]]
    for phase, (start, end) in enumerate(zip(starts, ends)):
        select = (x[:, -1] >= start) & (x[:, -1] < end)
        vselect = (vx[:, -1] >= start) & (vx[:, -1] < end)
        positions = model.positions(x[select])
        model.mean[phase] = positions.mean(dim=0)
        model.scale[phase] = positions.std(dim=0, unbiased=False).clamp_min(0.008)
        design = model.features(positions, phase)
        vdesign = model.features(model.positions(vx[vselect]), phase)
        matrices.append((design, y[select], vdesign, vy[vselect]))
    curve = []
    best = None
    checkpoint = directory / "policy.pt"
    for epoch, regularization in enumerate(REGULARIZATION, 1):
        check_cancel(directory)
        squared_error = 0.0
        validation_error = 0.0
        for phase, (features, labels, vfeatures, vlabels) in enumerate(matrices):
            penalty = torch.eye(features.shape[1], dtype=features.dtype) * (regularization * len(features))
            penalty[0, 0] = 0
            weights = torch.linalg.solve(features.T @ features + penalty, features.T @ labels)
            model.weights[phase] = weights
            squared_error += (features @ weights - labels).square().sum().item()
            validation_error += (vfeatures @ weights - vlabels).square().sum().item()
        val_loss = validation_error / vy.numel()
        row = {"epoch": epoch, "training_loss": squared_error / y.numel(), "validation_loss": val_loss}
        curve.append(row)
        emit("training", epochs=len(REGULARIZATION), **row)
        validation_runs = [
            rollout(env, model, spec, VALIDATION_SEED + i)
            for i in range(VALIDATION_EPISODES)
        ]
        successes = sum(int(run["success"]) for run in validation_runs)
        lifted = sum(int(run["lifted"]) for run in validation_runs)
        score = (successes, lifted, -val_loss)
        row["validation_successes"] = successes
        row["validation_episodes"] = VALIDATION_EPISODES
        if best is None or score > best:
            best = score
            torch.save({
                "epoch": epoch, "state": model.state_dict(), "boundaries": boundaries,
                "obs_names": env.obs_names, "action_names": env.action_names,
            }, checkpoint)
        print(f"验证策略第 {epoch} 轮，完成 {successes}/{VALIDATION_EPISODES} 集。", flush=True)
    return curve, checkpoint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--collector", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    args = parser.parse_args()
    directory = args.run_dir
    try:
        check_cancel(directory)
        native = load_native(args.native)
        spec_path = directory / "scene.json"
        spec = read_json(spec_path)
        environment = native.PickPlaceEnv.from_spec(str(spec_path))
        target, report = collect(directory, args.collector)
        episodes = load_demonstrations(
            target, report, len(environment.obs_names), len(environment.action_names),
        )
        curve, checkpoint = train(directory, episodes, environment, spec)
        stored = torch.load(checkpoint, map_location="cpu", weights_only=True)
        if stored["obs_names"] != environment.obs_names or stored["action_names"] != environment.action_names:
            raise ValueError("策略与评估使用的观测或动作不一致。")
        model = Policy(environment.obs_names, len(environment.action_names), stored["boundaries"])
        model.load_state_dict(stored["state"])
        model.eval()
        rows = []
        replay_dir = directory / "replay"
        replay_dir.mkdir()
        emit("evaluating", completed=0, total=EVAL_EPISODES, successes=0)
        evaluator = ["node", str(Path(__file__).with_name("policy_eval.mjs")), str(args.kernel), str(spec_path)]
        with subprocess.Popen(evaluator, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              encoding="utf-8", text=True) as child:
            try:
                evaluation = BrowserEngine(child, report["spec_hash"])
                for i in range(EVAL_EPISODES):
                    check_cancel(directory)
                    row = rollout(evaluation, model, spec, EVAL_SEED + i, record=True)
                    write_json(replay_dir / f"{row['seed']}.json", row)
                    rows.append({key: value for key, value in row.items() if key != "actions"})
                    emit("evaluating", completed=i + 1, total=EVAL_EPISODES,
                         successes=sum(int(item["success"]) for item in rows))
            finally:
                child.stdin.close()
        if child.returncode:
            raise RuntimeError("lux3D-engine 的 WebAssembly 评估进程异常结束。")
        successes = sum(int(item["success"]) for item in rows)
        clean = sum(item["contact"]["penetrating_frames"] == 0 for item in rows)
        write_json(directory / "policy_report.json", {
            "algorithm": "behavior_cloning", "checkpoint": checkpoint.name, "epoch": stored["epoch"],
            "spec_hash": report["spec_hash"], "demonstrations": len(episodes),
            "episodes": EVAL_EPISODES, "successes": successes, "success_rate": successes / EVAL_EPISODES,
            "passed": successes >= MIN_SUCCESSES and clean == EVAL_EPISODES,
            "required_successes": MIN_SUCCESSES, "collision_free_episodes": clean,
            "runs": rows, "curve": curve, "engine": native.build_info(), "evaluation_backend": "oriera-wasm",
        })
    except Cancelled:
        emit("cancelled")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
