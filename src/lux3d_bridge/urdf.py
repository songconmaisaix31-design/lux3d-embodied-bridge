"""URDF collision-mesh injection.

The upstream bench resolves ``<collision><geometry><mesh filename=...>``
through ``--mesh-package`` and parses STL into a convex hull. This module
writes exactly that reference, and stamps the asset id next to it so a
description can always be traced back to the bytes Lux3D produced.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

GEOMETRY_TAGS = ("box", "cylinder", "sphere", "capsule", "mesh")


def _pick_link(robot: ET.Element, link_name: str | None) -> ET.Element:
    links = robot.findall("link")
    if not links:
        raise ValueError("URDF has no <link> element")
    if link_name is None:
        return links[-1]
    for link in links:
        if link.get("name") == link_name:
            return link
    names = ", ".join(link.get("name", "?") for link in links)
    raise ValueError(f"no link named {link_name!r}; URDF links: {names}")


def inject(
    urdf_path: str,
    out_path: str,
    package: str,
    mesh_file: str,
    link_name: str | None = None,
    asset_id: str | None = None,
    asset_name: str | None = None,
) -> Dict[str, Any]:
    """Replace the target link's collision geometry with a packaged mesh."""
    tree = ET.parse(urdf_path)
    robot = tree.getroot()
    link = _pick_link(robot, link_name)

    collisions = link.findall("collision")
    collision = collisions[0] if collisions else ET.SubElement(link, "collision")
    geometry = collision.find("geometry")
    if geometry is None:
        geometry = ET.SubElement(collision, "geometry")

    replaced: List[str] = []
    for child in list(geometry):
        replaced.append(child.tag)
        geometry.remove(child)

    mesh = ET.SubElement(geometry, "mesh")
    mesh.set("filename", f"package://{package}/{mesh_file}")

    if asset_id:
        note = f" oriera:asset {asset_name or mesh_file} id={asset_id} "
        # keep provenance inside the file that the bench actually consumes
        collision.insert(0, ET.Comment(note))

    data = ET.tostring(robot, encoding="unicode")
    # ElementTree does not indent comments; put the provenance note on its own line
    data = re.sub(
        r"(?m)^([ \t]*)(<!--\s*oriera:asset[^\n]*?-->)(<geometry>)",
        r"\1\2\n\1\3",
        data,
    )
    header = '<?xml version="1.0" encoding="utf-8"?>\n'
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(header)
        handle.write(data)
        handle.write("\n")

    # parse the result back: a malformed description must fail here, not in the bench
    ET.parse(out_path)

    return {
        "link": link.get("name"),
        "mesh_uri": mesh.get("filename"),
        "replaced": replaced,
        "out": os.path.abspath(out_path),
    }
