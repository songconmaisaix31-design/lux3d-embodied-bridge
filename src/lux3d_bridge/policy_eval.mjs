import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { CONTACT_TOLERANCE_M, createContactAudit } from '../../web/js/workbench-contact.js';

const [kernel, spec] = process.argv.slice(2);
const module = await import(pathToFileURL(resolve(kernel, 'oriera_wasm.js')));
await module.default({ module_or_path: await readFile(resolve(kernel, 'oriera_wasm_bg.wasm')) });
const scene = await readFile(spec, 'utf8');
const sim = module.OrieraSim.from_spec_json(scene);
const audit = createContactAudit(JSON.parse(scene));
let contact;

function inspect(poses) {
  const depths = audit(poses);
  contact.frames++;
  if (Object.values(depths).some(depth => depth > CONTACT_TOLERANCE_M)) contact.penetrating_frames++;
  for (const [pair, depth] of Object.entries(depths)) {
    contact.max_depth_mm[pair] = Math.max(contact.max_depth_mm[pair] || 0, depth * 1000);
  }
}
const commands = createInterface({ input: process.stdin, crlfDelay: Infinity });
try {
  for await (const line of commands) {
    const command = JSON.parse(line);
    let response;
    if (command.type === 'reset') {
      response = { obs: [...sim.reset(command.seed)] };
      contact = { frames: 0, penetrating_frames: 0, max_depth_mm: {}, tolerance_mm: CONTACT_TOLERANCE_M * 1000 };
      inspect(JSON.parse(sim.poses()));
    } else if (command.type === 'step') {
      const step = sim.step(Float32Array.from(command.action));
      try {
        const pose = JSON.parse(sim.poses());
        inspect(pose);
        response = {
          obs: [...step.obs], reward: step.reward, terminated: step.terminated,
          truncated: step.truncated, success: step.success,
          holding: pose.holding, objectPosition: pose.props[0].pos,
        };
      } finally {
        step.free();
      }
    } else if (command.type === 'end') {
      response = { fingerprint: sim.fingerprint(), specHash: sim.spec_hash(), contact };
    } else {
      throw new Error('无法识别的评估命令。');
    }
    process.stdout.write(JSON.stringify(response) + '\n');
  }
} finally {
  sim.free();
}
