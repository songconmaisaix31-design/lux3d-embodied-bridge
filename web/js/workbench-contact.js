import { Plane, Vector3 } from 'three';

const WORLD_AXES = [new Vector3(1, 0, 0), new Vector3(0, 1, 0), new Vector3(0, 0, 1)];
const CLEARANCE = 0.0002;

function radius(half, basis, axis) {
  return half[0] * Math.abs(basis[0].dot(axis))
    + half[1] * Math.abs(basis[1].dot(axis))
    + half[2] * Math.abs(basis[2].dot(axis));
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
