// URDF model -> a driven three.js hierarchy.
//
// The transform chain is exactly the one the description declares:
//   parent link --(origin xyz/rpy)--> joint frame --(rotate about axis)--> child link
// Nothing is re-parented or offset by hand; if the arm looks wrong, the URDF is
// wrong, which is the point of drawing it from the file the bench consumes.

import * as THREE from 'three';

const PALETTE = {
  base: 0x2c3644,
  link: 0x9aa7b8,
  tool: 0xff7a3d,
};

function eulerFromRpy([roll, pitch, yaw]) {
  // URDF rpy is fixed-axis roll-pitch-yaw: R = Rz(yaw) Ry(pitch) Rx(roll).
  return new THREE.Quaternion().setFromEuler(new THREE.Euler(roll, pitch, yaw, 'ZYX'));
}

function geometryFor(shape) {
  if (shape.type === 'box') {
    const [x, y, z] = shape.size;
    return new THREE.BoxGeometry(Math.max(x, 1e-4), Math.max(y, 1e-4), Math.max(z, 1e-4));
  }
  if (shape.type === 'cylinder') {
    const geometry = new THREE.CylinderGeometry(shape.radius, shape.radius, shape.length, 24, 1);
    geometry.rotateX(Math.PI / 2); // URDF cylinders run along +Z
    return geometry;
  }
  if (shape.type === 'sphere') {
    return new THREE.SphereGeometry(shape.radius, 24, 16);
  }
  return null;
}

function materialFor(linkName) {
  if (linkName === 'tool') {
    return new THREE.MeshStandardMaterial({ color: PALETTE.tool, metalness: 0.4, roughness: 0.35, emissive: 0x331305 });
  }
  if (linkName === 'base_link') {
    return new THREE.MeshStandardMaterial({ color: PALETTE.base, metalness: 0.2, roughness: 0.85 });
  }
  return new THREE.MeshStandardMaterial({ color: PALETTE.link, metalness: 0.6, roughness: 0.42 });
}

function addVisuals(group, link, materials) {
  for (const visual of link.visuals || []) {
    const geometry = geometryFor(visual.geometry);
    if (!geometry) continue;
    const material = materials.get(link.name) || (materials.set(link.name, materialFor(link.name)), materials.get(link.name));
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.fromArray(visual.origin.xyz);
    mesh.quaternion.copy(eulerFromRpy(visual.origin.rpy));
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    group.add(mesh);

    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(geometry, 24),
      new THREE.LineBasicMaterial({ color: 0x0c1017, transparent: true, opacity: 0.55 })
    );
    edges.position.copy(mesh.position);
    edges.quaternion.copy(mesh.quaternion);
    group.add(edges);
  }
}

export function buildArm(model) {
  const links = new Map(model.links.map((link) => [link.name, link]));
  const childrenOf = new Map();
  for (const joint of model.joints) {
    if (!childrenOf.has(joint.parent)) childrenOf.set(joint.parent, []);
    childrenOf.get(joint.parent).push(joint);
  }

  const materials = new Map();
  const joints = new Map();
  const linkObjects = new Map();

  function buildLink(name) {
    const group = new THREE.Group();
    group.name = `link:${name}`;
    const link = links.get(name);
    if (link) addVisuals(group, link, materials);
    linkObjects.set(name, group);
    for (const joint of childrenOf.get(name) || []) {
      const frame = new THREE.Group();          // joint origin in the parent link
      frame.position.fromArray(joint.origin.xyz);
      frame.quaternion.copy(eulerFromRpy(joint.origin.rpy));

      const axisNode = new THREE.Group();        // rotation about the declared axis
      frame.add(axisNode);
      group.add(frame);

      axisNode.add(buildLink(joint.child));

      if (joint.dof) {
        const axis = new THREE.Vector3().fromArray(joint.axis).normalize();
        // A short cyan pin along the declared axis: it makes "joint1 turns about
        // Z, joint2 about Y" visible instead of something you infer from motion.
        const pin = new THREE.Mesh(
          new THREE.CylinderGeometry(0.0045, 0.0045, 0.075, 12),
          new THREE.MeshBasicMaterial({ color: 0x63d2ff, transparent: true, opacity: 0.9, depthTest: false })
        );
        pin.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), axis);
        pin.renderOrder = 3;
        frame.add(pin);

        joints.set(joint.name, {
          name: joint.name,
          type: joint.type,
          axis,
          limits: joint.limits,
          node: axisNode,
          value: 0,
        });
      }
    }
    return group;
  }

  const root = new THREE.Group();
  root.name = `robot:${model.robot}`;
  // URDF is Z-up; the scene is Y-up. One rotation, applied once, at the root.
  root.rotation.x = -Math.PI / 2;
  root.add(buildLink(model.root));

  function setJoint(name, value) {
    const joint = joints.get(name);
    if (!joint) return;
    const lower = joint.limits.lower ?? -Math.PI;
    const upper = joint.limits.upper ?? Math.PI;
    const clamped = Math.min(upper, Math.max(lower, value));
    joint.value = clamped;
    joint.node.quaternion.setFromAxisAngle(joint.axis, clamped);
  }

  function worldPosition(name, target = new THREE.Vector3()) {
    const object = linkObjects.get(name);
    if (!object) return null;
    return object.getWorldPosition(target);
  }

  return {
    model,
    root,
    joints,
    setJoint,
    worldPosition,
    toolLink: model.links[model.links.length - 1]?.name ?? model.root,
    hasTool: linkObjects.has('tool'),
  };
}
