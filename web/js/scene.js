// Renderer, camera, lights and the table the arm stands on.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

export function createStage(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.25;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();

  const camera = new THREE.PerspectiveCamera(36, 1, 0.02, 30);
  camera.position.set(1.02, 0.74, 1.0);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 0.16, 0);
  controls.enableDamping = true;
  controls.dampingFactor = 0.075;
  controls.minDistance = 0.32;
  controls.maxDistance = 4.5;
  controls.maxPolarAngle = Math.PI * 0.495;
  controls.autoRotateSpeed = 1.1;

  scene.add(new THREE.HemisphereLight(0xa8bcdd, 0x1d242e, 1.15));

  const key = new THREE.DirectionalLight(0xfff2e2, 3.1);
  key.position.set(0.95, 1.7, 0.75);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.camera.near = 0.2;
  key.shadow.camera.far = 5;
  key.shadow.camera.left = -1.2;
  key.shadow.camera.right = 1.2;
  key.shadow.camera.top = 1.2;
  key.shadow.camera.bottom = -1.2;
  key.shadow.bias = -0.0008;
  key.shadow.normalBias = 0.012;
  scene.add(key);

  const rim = new THREE.DirectionalLight(0x86b6ff, 1.15);
  rim.position.set(-1.3, 0.9, -1.1);
  scene.add(rim);

  const table = new THREE.Mesh(
    new THREE.BoxGeometry(1.62, 0.05, 1.02),
    new THREE.MeshStandardMaterial({ color: 0x2c2620, roughness: 0.88, metalness: 0.04 })
  );
  table.position.y = -0.026;
  table.receiveShadow = true;
  scene.add(table);

  const grid = new THREE.GridHelper(1.62, 32, 0x344054, 0x1c2431);
  grid.position.y = 0.0015;
  grid.material.transparent = true;
  grid.material.opacity = 0.5;
  scene.add(grid);

  const clock = new THREE.Clock();
  let running = true;

  function resize() {
    const parent = canvas.parentElement;
    const width = parent.clientWidth || 1;
    const height = parent.clientHeight || 1;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  resize();
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(resize).observe(canvas.parentElement);

  function render() {
    if (!running) return;
    requestAnimationFrame(render);
    const delta = clock.getDelta();
    controls.update(delta);
    renderer.render(scene, camera);
  }
  requestAnimationFrame(render);

  return {
    scene,
    camera,
    renderer,
    controls,
    grid,
    table,
    stop() {
      running = false;
    },
    frameOn(box, padding = 1.25) {
      if (!box || box.isEmpty()) return;
      const sphere = box.getBoundingSphere(new THREE.Sphere());
      const radius = Math.max(sphere.radius, 0.05) * padding;
      const vertical = (camera.fov * Math.PI) / 180;
      const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * camera.aspect);
      const distance = Math.max(radius / Math.sin(vertical / 2), radius / Math.sin(horizontal / 2));
      const direction = new THREE.Vector3(0.55, 0.52, 1.0).normalize();
      controls.target.copy(sphere.center);
      camera.position.copy(sphere.center).addScaledVector(direction, distance);
      controls.update();
    },
  };
}
