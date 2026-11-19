import { Plane, Quaternion, Vector3 } from '../vendor/three.module.js';

const WORLD_AXES = [new Vector3(1, 0, 0), new Vector3(0, 1, 0), new Vector3(0, 0, 1)];
const CLEARANCE = 0.0002;

function radius(half, basis, axis) {
  return half[0] * Math.abs(basis[0].dot(axis))
    + half[1] * Math.abs(basis[1].dot(axis))
    + half[2] * Math.abs(basis[2].dot(axis));
}

// 浮点数舍入容差，1 微米。
export const CONTACT_TOLERANCE_M = 0.000001;

export function createContactAudit(spec) {
  const table = spec.scene.fixtures.find(fixture => fixture.name === 'table');
  const shape = half => ({ half, pos: new Vector3(), axes: WORLD_AXES.map(axis => axis.clone()) });
  const worktop = shape(table.half);
  worktop.pos.fromArray(table.center);
  const prop = shape(spec.scene.props[0].shape.half);
  const jaws = WORLD_AXES.slice(0, 2).map(() => shape(spec.robot.gripper.finger_half));
  const rotation = new Quaternion();
  const offset = new Vector3();
  const cross = new Vector3();

  function pose(shape, data) {
    shape.pos.fromArray(data.pos);
    rotation.fromArray(data.rot);
    WORLD_AXES.forEach((axis, i) => shape.axes[i].copy(axis).applyQuaternion(rotation));
  }

  function depth(a, b) {
    offset.subVectors(a.pos, b.pos);
    const projected = axis => radius(a.half, a.axes, axis) + radius(b.half, b.axes, axis) - Math.abs(offset.dot(axis));
    let shortest = Math.min(...a.axes.map(projected), ...b.axes.map(projected));
    if (shortest <= 0) return 0;
    for (const u of a.axes) {
      for (const v of b.axes) {
        cross.crossVectors(u, v);
        const length = cross.length();
        if (length < 1e-7) continue;
        shortest = Math.min(shortest, projected(cross) / length);
        if (shortest <= 0) return 0;
      }
    }
    return shortest;
  }

  return poses => {
    pose(prop, poses.props[0]);
    pose(jaws[0], poses.fingers.left);
    pose(jaws[1], poses.fingers.right);
    return {
      gripper_table: Math.max(...jaws.map(jaw => depth(jaw, worktop))),
      object_table: depth(prop, worktop),
      gripper_object: Math.max(...jaws.map(jaw => depth(jaw, prop))),
      gripper_self: depth(jaws[0], jaws[1]),
    };
  };
}

export function createWorktopContact(table, size, fingerHalf) {
  const propHalf = size.map(length => length / 2);
  const surface = table.center[1] + table.half[1];
  const clipAtWorktop = [new Plane(WORLD_AXES[1].clone(), -surface)];
  const noClipping = [];
  const propAxes = WORLD_AXES.map(axis => axis.clone());
  const fingerAxes = WORLD_AXES.map(axis => axis.clone());
  const opening = new Vector3();
  const offset = new Vector3();
  const cross = new Vector3();
  const overlapsWorktop = (position, half, axes) => [0, 2].every(i =>
    Math.abs(position.getComponent(i) - table.center[i])
      < table.half[i] + radius(half, axes, WORLD_AXES[i]));

  function separates(axis) {
    return Math.abs(offset.dot(axis)) >= radius(propHalf, propAxes, axis)
      + radius(fingerHalf, fingerAxes, axis);
  }

  function intersectsProp() {
    if (propAxes.some(separates) || fingerAxes.some(separates)) return false;
    for (const propAxis of propAxes) {
      for (const fingerAxis of fingerAxes) {
        cross.crossVectors(propAxis, fingerAxis);
        if (cross.lengthSq() > 1e-12 && separates(cross)) return false;
      }
    }
    return true;
  }

  return (target, fingers) => {
    WORLD_AXES.forEach((axis, i) => propAxes[i].copy(axis).applyQuaternion(target.quaternion));
    if (target.position.y >= surface && overlapsWorktop(target.position, propHalf, propAxes)) {
      const bottom = target.position.y - radius(propHalf, propAxes, WORLD_AXES[1]);
      if (bottom < surface) target.position.y += surface - bottom;
    }

    opening.subVectors(fingers[0].position, fingers[1].position).normalize();
    fingers.forEach((finger, index) => {
      WORLD_AXES.forEach((axis, i) => fingerAxes[i].copy(axis).applyQuaternion(finger.quaternion));
      offset.subVectors(finger.position, target.position);
      if (opening.lengthSq() > 0 && intersectsProp()) {
        const side = index === 0 ? 1 : -1;
        const required = radius(propHalf, propAxes, opening) + radius(fingerHalf, fingerAxes, opening);
        const shift = required + CLEARANCE - side * offset.dot(opening);
        finger.position.addScaledVector(opening, side * shift);
      }
      const clipping = overlapsWorktop(finger.position, fingerHalf, fingerAxes) ? clipAtWorktop : noClipping;
      finger.material.clippingPlanes = clipping;
    });
  };
}
