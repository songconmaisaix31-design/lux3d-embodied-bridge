"""URDF -> JSON model for the WebGL viewer.

The viewer must show the same description the bench consumes, not a hand-made
scene: the arm on screen is parsed from the URDF that the bridge injects into.
Scope is deliberately the subset this repository's fixtures use - one visual
and one collision per link, ``box``/``cylinder``/``sphere``/``mesh`` geometry,
``revolute``/``continuous``/``prismatic``/``fixed`` joints - and anything outside
that subset is reported, not guessed.

What it never does:

  * invent a missing origin (URDF default is 0 0 0 - that default *is* applied)
  * reorder the tree (document order is preserved; ``joints`` keeps file order)
  * silently skip a malformed link: a missing parent/child raises
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

Geometry = Dict[str, Any]

JOINT_TYPES = ("revolute", "continuous", "prismatic", "fixed")

#: Everything the viewer can draw. A mesh with an unsupported URI still parses,
#: but is reported in ``warnings`` so the page can say "not drawn" out loud.
GEOMETRY_TYPES = ("box", "cylinder", "sphere", "mesh")


class UrdfError(ValueError):
    """A description the viewer cannot draw, with the reason attached."""


def _floats(text: Optional[str], default: tuple) -> List[float]:
    if text is None or not text.strip():
        return [float(value) for value in default]
    try:
        return [float(value) for value in text.split()]
    except ValueError as exc:
        raise UrdfError(f"expected numbers, got {text!r}") from exc


def _origin(node: Optional[ET.Element]) -> Dict[str, List[float]]:
    if node is None:
        return {"xyz": [0.0, 0.0, 0.0], "rpy": [0.0, 0.0, 0.0]}
    return {
        "xyz": _floats(node.get("xyz"), (0, 0, 0)),
        "rpy": _floats(node.get("rpy"), (0, 0, 0)),
    }


def _geometry(node: ET.Element) -> Optional[Geometry]:
    shape = node.find("geometry")
    if shape is None:
        return None
    for child in list(shape):
        kind = child.tag.lower()
        if kind == "box":
            return {"type": "box", "size": _floats(child.get("size"), (0, 0, 0))}
        if kind == "cylinder":
            return {
                "type": "cylinder",
                "radius": _floats(child.get("radius"), (0,))[0],
                "length": _floats(child.get("length"), (0,))[0],
            }
        if kind == "sphere":
            return {"type": "sphere", "radius": _floats(child.get("radius"), (0,))[0]}
        if kind == "mesh":
            return {
                "type": "mesh",
                "filename": child.get("filename") or "",
                "scale": _floats(child.get("scale"), (1, 1, 1)),
            }
    return None


def _parse_link(element: ET.Element, warnings: List[str]) -> Dict[str, Any]:
    name = element.get("name") or ""
    if not name:
        raise UrdfError("a <link> has no name")
    visuals = []
    for index, node in enumerate(element.findall("visual")):
        geometry = _geometry(node)
        if geometry is None:
            warnings.append(f"{name}: <visual> #{index} has no geometry")
            continue
        if geometry["type"] == "mesh":
            warnings.append(f"{name}: mesh visual {geometry['filename']!r} is listed, not loaded")
        visuals.append({"origin": _origin(node.find("origin")), "geometry": geometry})
    collisions = []
    for index, node in enumerate(element.findall("collision")):
        geometry = _geometry(node)
        if geometry is None:
            warnings.append(f"{name}: <collision> #{index} has no geometry")
            continue
        collisions.append({"origin": _origin(node.find("origin")), "geometry": geometry})
    link: Dict[str, Any] = {"name": name, "visuals": visuals, "collisions": collisions}
    inertial = element.find("inertial")
    if inertial is not None:
        mass = inertial.find("mass")
        link["mass_kg"] = _floats(mass.get("value") if mass is not None else None, (0,))[0]
    return link


def _parse_joint(element: ET.Element, links: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    name = element.get("name") or ""
    kind = (element.get("type") or "").lower()
    if kind not in JOINT_TYPES:
        raise UrdfError(f"joint {name!r}: unsupported type {kind!r} (known: {', '.join(JOINT_TYPES)})")
    parent = element.find("parent")
    child = element.find("child")
    parent_link = parent.get("link") if parent is not None else None
    child_link = child.get("link") if child is not None else None
    for side, value in (("parent", parent_link), ("child", child_link)):
        if not value:
            raise UrdfError(f"joint {name!r}: missing <{side} link=...>")
        if value not in links:
            raise UrdfError(f"joint {name!r}: {side} link {value!r} is not described")
    axis = _floats((element.find("axis").get("xyz") if element.find("axis") is not None else None), (1, 0, 0))
    limit = element.find("limit")
    limits: Dict[str, float] = {}
    if limit is not None:
        for key in ("lower", "upper", "effort", "velocity"):
            if limit.get(key) is not None:
                limits[key] = _floats(limit.get(key), (0,))[0]
    if kind in ("revolute", "prismatic"):
        if "lower" not in limits or "upper" not in limits:
            raise UrdfError(f"joint {name!r} is {kind} but its <limit> has no lower/upper")
        if limits["lower"] > limits["upper"]:
            raise UrdfError(f"joint {name!r}: lower {limits['lower']} > upper {limits['upper']}")
    joint = {
        "name": name,
        "type": kind,
        "parent": parent_link,
        "child": child_link,
        "origin": _origin(element.find("origin")),
        "axis": axis,
        "limits": limits,
        "dof": kind != "fixed",
    }
    dynamics = element.find("dynamics")
    if dynamics is not None:
        joint["damping"] = _floats(dynamics.get("damping"), (0,))[0]
    return joint


def parse_urdf(path: str) -> Dict[str, Any]:
    """Parse a URDF file into the model the browser draws and drives."""
    if not os.path.isfile(path):
        raise UrdfError(f"no such URDF: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise UrdfError(f"{os.path.basename(path)} is not valid XML: {exc}") from exc
    if root.tag.lower() != "robot":
        raise UrdfError(f"{os.path.basename(path)}: root element is <{root.tag}>, expected <robot>")

    warnings: List[str] = []
    links: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for element in root.findall("link"):
        link = _parse_link(element, warnings)
        if link["name"] in links:
            raise UrdfError(f"duplicate link {link['name']!r}")
        links[link["name"]] = link
        order.append(link["name"])
    if not links:
        raise UrdfError("no <link> elements")

    joints = [_parse_joint(element, links) for element in root.findall("joint")]
    children = {joint["child"] for joint in joints}
    roots = [name for name in order if name not in children]
    if len(roots) != 1:
        raise UrdfError(
            f"expected exactly one root link, found {len(roots)}: {', '.join(roots) or 'none'}"
        )
    _check_acyclic(links, joints)
    for link in links.values():
        if not link["visuals"] and not link["collisions"]:
            warnings.append(f"{link['name']}: no visual and no collision geometry")

    return {
        "robot": root.get("name") or os.path.basename(path),
        "source": os.path.basename(path),
        "root": roots[0],
        "links": [links[name] for name in order],
        "joints": joints,
        "dof": sum(1 for joint in joints if joint["dof"]),
        "warnings": warnings,
    }


def _check_acyclic(links: Dict[str, Dict[str, Any]], joints: List[Dict[str, Any]]) -> None:
    """A URDF is a tree; a loop would make the viewer's FK ambiguous."""
    parents = {joint["child"]: joint["parent"] for joint in joints}
    for start in parents:
        seen = {start}
        cursor = start
        while cursor in parents:
            cursor = parents[cursor]
            if cursor in seen:
                raise UrdfError(f"joint graph has a cycle through {cursor!r}")
            seen.add(cursor)


def summary(model: Dict[str, Any]) -> str:
    return (
        f"{model['robot']} ({model['source']}): {len(model['links'])} links, "
        f"{len(model['joints'])} joints, {model['dof']} DoF"
    )
