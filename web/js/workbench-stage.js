import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { createWorktopContact } from './workbench-contact.js';

const MATERIAL = {
  arm: new THREE.MeshStandardMaterial({ color: 0xe1e6df, roughness: 0.42, metalness: 0.19 }),
  joint: new THREE.MeshStandardMaterial({ color: 0x4b5a54, roughness: 0.45, metalness: 0.29 }),
  table: new THREE.MeshStandardMaterial({ color: 0x333e3a, roughness: 0.83, metalness: 0.12 }),
};

function box(half, material) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(...half.map(n => 2 * n)), material);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

function disposeGroup(group) {
  const disposed = new Set();
  const release = (resource) => {
    if (!disposed.has(resource)) {
      disposed.add(resource);
      resource.dispose();
    }
  };
  group.traverse(obj => {
    if (obj.geometry) release(obj.geometry);
    if (!obj.material) return;
    for (const material of [obj.material].flat()) {
      if (Object.values(MATERIAL).includes(material)) continue;
      for (const property of Object.values(material)) {
        if (property?.isTexture) release(property);
      }
      release(material);
    }
  });
}

function applyPose(target, pose) {
  target.position.fromArray(pose.pos);
  target.quaternion.fromArray(pose.rot);
}

let kernel;
function loadKernel() {
  if (!kernel) {
    kernel = import('/api/workbench/kernel/oriera_wasm.js').then(async mod => {
      await mod.default();
      return mod.OrieraSim;
    }).catch(error => {
      kernel = null;
      throw error;
    });
  }
  return kernel;
}

export function createWorkbenchStage(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.8));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.21;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.localClippingEnabled = true;

  const world = new THREE.Scene();
  world.background = new THREE.Color(0x151d1a);
  world.fog = new THREE.Fog(0x151d1a, 2.1, 5.5);
  const camera = new THREE.PerspectiveCamera(36, 1, 0.002, 40);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = false;
  controls.maxDistance = 5;
  controls.minDistance = 0.04;
  controls.maxPolarAngle = Math.PI * 0.49;

  const light = new THREE.DirectionalLight(0xfff3d8, 3.0);
  light.position.set(1.2, 2.6, 1.25);
  light.castShadow = true;
  light.shadow.mapSize.set(1536, 1536);
  light.shadow.camera.left = -1.4;
  light.shadow.camera.right = 1.4;
  light.shadow.camera.top = 1.4;
  light.shadow.camera.bottom = -1.4;
  light.shadow.normalBias = 0.007;
  const rim = new THREE.DirectionalLight(0xa5d3c7, 1.5);
  rim.position.set(-1.2, 0.85, -1.3);
  world.add(light, rim, new THREE.HemisphereLight(0xe1fff2, 0x3f4039, 1.55));

  const grid = new THREE.GridHelper(12, 120, 0x465650, 0x28352f);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(28, 28), new THREE.MeshStandardMaterial({
    color: 0x192420, roughness: 0.97,
  }));
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -0.002;
  floor.receiveShadow = true;
  world.add(floor, grid);
  const gltf = new GLTFLoader();
  let content = new THREE.Group();
  let simulation = null;
  let config = null;
  let frames = [];
  let links = [];
  let fingers = [];
  let goal = null;
  let target = null;
  let outline = null;
  let resolveContact = null;
  let showCollision = false;
  let revision = 0;
  world.add(content);

  function render() {
    renderer.render(world, camera);
  }

  function resize() {
    const { clientWidth: width, clientHeight: height } = canvas;
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    render();
  }
  new ResizeObserver(resize).observe(canvas);
  controls.addEventListener('change', render);

  function fit() {
    const bounds = new THREE.Box3().setFromObject(content);
    const sphere = bounds.isEmpty()
      ? new THREE.Sphere(new THREE.Vector3(0, 0.3, 0), 0.38)
      : bounds.getBoundingSphere(new THREE.Sphere());
    const margin = camera.aspect < 1 ? 1.2 / camera.aspect : 1.2;
    const distance = Math.max(0.12, sphere.radius * margin / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)));
    controls.target.copy(sphere.center);
    camera.position.copy(sphere.center).add(new THREE.Vector3(1.1, 0.75, 1.5).normalize().multiplyScalar(distance));
    controls.update();
    render();
  }

  function replace(next) {
    simulation?.free();
    simulation = null;
    config = null;
    frames = [];
    outline = null;
    resolveContact = null;
    world.remove(content);
    disposeGroup(content);
    content = next;
    world.add(content);
  }

  async function loadAsset(url) {
    const model = (await gltf.loadAsync(url)).scene;
    model.traverse(node => {
      if (node.isMesh) {
        node.castShadow = true;
        node.receiveShadow = true;
      }
    });
    return model;
  }

  async function asset(url) {
    const ticket = ++revision;
    const object = await loadAsset(url);
    if (ticket !== revision) {
      disposeGroup(object);
      return;
    }
    const bounds = new THREE.Box3().setFromObject(object);
    const center = bounds.getCenter(new THREE.Vector3());
    object.position.set(-center.x, -bounds.min.y + 0.004, -center.z);
    const group = new THREE.Group();
    group.add(object);
    replace(group);
    fit();
  }

  function paint(poses) {
    for (const [i, pose] of poses.links.entries()) applyPose(links[i], pose);
    applyPose(fingers[0], poses.fingers.left);
    applyPose(fingers[1], poses.fingers.right);
    applyPose(target, poses.props[0]);
    resolveContact(target, fingers);
    goal.position.set(poses.goal[0], config.surface_m + 0.0015, poses.goal[2]);
    render();
  }

  async function scene(data) {
    const ticket = ++revision;
    const [Engine, object] = await Promise.all([loadKernel(), loadAsset(data.glb_url)]);
    if (ticket !== revision) {
      disposeGroup(object);
      return;
    }
    const next = Engine.from_spec_json(JSON.stringify(data.spec));
    next.reset(1000);
    const initial = JSON.parse(next.poses());
    const group = new THREE.Group();
    const table = data.spec.scene.fixtures.find(item => item.name === 'table');
    const tableMesh = box(table.half, MATERIAL.table);
    tableMesh.position.fromArray(table.center);
    group.add(tableMesh);
    const nextLinks = data.spec.robot.links.map((link, i) => {
      const part = new THREE.Group();
      const mesh = box(link.shape.Box.half, i < 2 ? MATERIAL.joint : MATERIAL.arm);
      mesh.position.fromArray(link.collider_offset);
      part.add(mesh);
      group.add(part);
      return part;
    });
    const fingerHalf = data.spec.robot.gripper.finger_half;
    const nextFingers = [0, 1].map(() => {
      const material = MATERIAL.joint.clone();
      material.clipShadows = true;
      return box(fingerHalf, material);
    });
    group.add(...nextFingers);
    const nextGoal = new THREE.Mesh(
      new THREE.RingGeometry(data.spec.task.params.goal_xz_tol - 0.0015, data.spec.task.params.goal_xz_tol, 64),
      new THREE.MeshBasicMaterial({ color: 0xa7dab9, opacity: 0.82, transparent: true, side: THREE.DoubleSide }),
    );
    nextGoal.rotation.x = -Math.PI / 2;
    group.add(nextGoal);

    const nextTarget = new THREE.Group();
    const orientation = new THREE.Group();
    if (data.asset_rotation) orientation.rotation.set(...data.asset_rotation);
    object.scale.setScalar(data.scale);
    object.position.fromArray(data.pivot_m).multiplyScalar(-data.scale);
    orientation.add(object);
    const bounds = new THREE.BoxGeometry(...data.size_m);
    const nextOutline = new THREE.LineSegments(
      new THREE.EdgesGeometry(bounds),
      new THREE.LineBasicMaterial({ color: 0xc6efc7, transparent: true, opacity: 0.9 }),
    );
    bounds.dispose();
    nextOutline.scale.setScalar(1.004);
    nextOutline.visible = showCollision;
    nextTarget.add(orientation, nextOutline);
    group.add(nextTarget);

    replace(group);
    simulation = next;
    config = data;
    links = nextLinks;
    fingers = nextFingers;
    goal = nextGoal;
    target = nextTarget;
    outline = nextOutline;
    // 只修正视口的接触显示。回放仍用引擎原始状态核对指纹。
    resolveContact = createWorktopContact(table, data.size_m, fingerHalf);
    paint(initial);
    fit();
  }

  async function replay(data) {
    if (!simulation) throw new Error('请先加载该评估使用的场景。');
    const ticket = ++revision;
    const Engine = await loadKernel();
    const run = Engine.from_spec_json(JSON.stringify(config.spec));
    try {
      run.reset(data.seed);
      const states = [JSON.parse(run.poses())];
      for (const [index, action] of data.actions.entries()) {
        if (index % 40 === 0) {
          await new Promise(requestAnimationFrame);
          if (ticket !== revision) throw new Error('已切换到另一场景。');
        }
        const step = run.step(Float32Array.from(action));
        step.free();
        states.push(JSON.parse(run.poses()));
      }
      if (run.fingerprint() !== data.fingerprint) {
        throw new Error('浏览器回放与同一集评估的物理状态不一致。');
      }
      frames = states;
      paint(frames[0]);
      return frames.length - 1;
    } finally {
      run.free();
    }
  }

  function collision(visible) {
    showCollision = visible;
    if (outline) outline.visible = visible;
    render();
  }

  function frame(index) {
    const state = frames[index];
    if (state) paint(state);
  }

  resize();
  fit();
  return { asset, scene, replay, frame, fit, collision };
}
