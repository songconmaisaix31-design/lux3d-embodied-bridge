// STL -> triangle positions. Handles both binary STL (what the bridge writes)
// and ASCII STL (what a CAD export may hand you); anything else throws.

export function parseStl(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength >= 84) {
    const count = view.getUint32(80, true);
    if (84 + count * 50 === buffer.byteLength) return parseBinary(view, count);
  }
  const text = new TextDecoder().decode(new Uint8Array(buffer));
  if (/^\s*solid/i.test(text)) return parseAscii(text);
  throw new Error(`not an STL this viewer understands (${buffer.byteLength} bytes)`);
}

function parseBinary(view, count) {
  const positions = new Float32Array(count * 9);
  let offset = 84;
  for (let i = 0; i < count; i += 1) {
    offset += 12; // skip the facet normal, normals are recomputed
    for (let v = 0; v < 9; v += 1) {
      positions[i * 9 + v] = view.getFloat32(offset, true);
      offset += 4;
    }
    offset += 2; // attribute byte count
  }
  return positions;
}

function parseAscii(text) {
  const vertices = [];
  for (const match of text.matchAll(/vertex\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)/g)) {
    vertices.push(Number(match[1]), Number(match[2]), Number(match[3]));
  }
  if (vertices.length % 9 !== 0 || vertices.length === 0) {
    throw new Error('ASCII STL is not made of whole triangles');
  }
  return new Float32Array(vertices);
}
