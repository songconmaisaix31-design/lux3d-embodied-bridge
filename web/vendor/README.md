# Vendored three.js

The viewer must open with no network and no build step, so these files are checked in.

| File | Version | License | Source |
|---|---|---|---|
| `three.module.js` | r169 (0.169.0) | MIT | `https://cdn.jsdelivr.net/npm/three@0.169.0/build/three.module.js` |
| `addons/controls/OrbitControls.js` | r169 | MIT | `https://cdn.jsdelivr.net/npm/three@0.169.0/examples/jsm/controls/OrbitControls.js` |
| `addons/loaders/GLTFLoader.js` | r169 | MIT | `https://cdn.jsdelivr.net/npm/three@0.169.0/examples/jsm/loaders/GLTFLoader.js` |
| `addons/utils/BufferGeometryUtils.js` | r169 | MIT | `https://cdn.jsdelivr.net/npm/three@0.169.0/examples/jsm/utils/BufferGeometryUtils.js` |

Upstream: <https://github.com/mrdoob/three.js> — MIT, © three.js authors.

`web/index.html` maps the bare specifier `three` to `./vendor/three.module.js` and
`three/addons/` to `./vendor/addons/`, so the addons load unmodified.

To upgrade: replace the four files with the same paths from a newer release, keep the
import map as is, and re-run `python -m unittest discover -s tests` plus one manual
`python bridge.py serve` smoke test (the page either draws the arm and the crate or it does not).
