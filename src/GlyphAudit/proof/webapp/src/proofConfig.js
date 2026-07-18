// Runtime config for the proof app. Written by GlyphAudit.proof.build every
// time the fonts are recompiled, so anything the app renders (family name,
// reference dropdown, manifest URLs) tracks the source of truth without
// requiring a rebuild of the JS bundle.
//
// Schema (proof-config.json):
//   {
//     "familyName":   "Velarium Proof",              // CSS font-family used
//                                                     // for the left panel.
//     "faces": {                                       // Proof faces.
//       "roman":  { "ttf": "…", "chars": "…", "features": "…" },
//       "italic": { "ttf": "…", "chars": "…", "features": "…" }   // optional
//     },
//     "references": [                                  // Right-panel options.
//       { "name": "Verdana", "slots": [
//         { "file": "Verdana-Regular.ttf", "weight": 400, "style": "normal" },
//         { "file": "Verdana-Bold.ttf",    "weight": 700, "style": "normal" },
//         { "file": "Verdana-Italic.ttf",  "weight": 400, "style": "italic" },
//         { "file": "Verdana-BoldItalic.ttf", "weight": 700, "style": "italic" }
//       ]}
//     ]
//   }
//
// Files under `faces` and `references[].slots[].file` are resolved relative
// to the app root (i.e. the same directory the JSON is served from).

// Fallback shape used when /proof-config.json is missing. The app still
// renders — degraded — but a banner warns the user something's off.
export const FALLBACK_CONFIG = {
  familyName: 'Proof',
  faces: {},
  references: [],
}

export async function loadProofConfig() {
  try {
    const r = await fetch('/proof-config.json', { cache: 'no-store' })
    if (!r.ok) return { config: FALLBACK_CONFIG, source: 'fallback', reason: `HTTP ${r.status}` }
    const config = await r.json()
    return { config: normalize(config), source: 'file', reason: null }
  } catch (e) {
    return { config: FALLBACK_CONFIG, source: 'fallback', reason: String(e) }
  }
}

function normalize(raw) {
  return {
    familyName: raw.familyName || FALLBACK_CONFIG.familyName,
    faces: raw.faces || {},
    references: Array.isArray(raw.references) ? raw.references : [],
  }
}

// Inject @font-face rules for the proof faces + every reference slot into
// a single <style id="proof-config-fontfaces"> tag. Idempotent — safe to
// call repeatedly; the previous tag is replaced.
export function injectFontFaces(config) {
  const rules = []
  const { familyName, faces } = config

  const romanFace = faces.roman
  if (romanFace?.ttf) {
    rules.push(faceRule(familyName, romanFace.ttf, '100 900', 'normal'))
  }
  const italicFace = faces.italic
  if (italicFace?.ttf) {
    rules.push(faceRule(familyName, italicFace.ttf, '100 900', 'italic'))
  }
  for (const ref of config.references) {
    for (const slot of ref.slots || []) {
      const weight = slot.weight ?? 400
      const style  = slot.style ?? 'normal'
      rules.push(faceRule(ref.name, slot.file, String(weight), style))
    }
  }

  let tag = document.getElementById('proof-config-fontfaces')
  if (!tag) {
    tag = document.createElement('style')
    tag.id = 'proof-config-fontfaces'
    document.head.appendChild(tag)
  }
  tag.textContent = rules.join('\n\n')
}

function faceRule(family, file, weight, style) {
  const url = file.startsWith('/') || file.startsWith('http') ? file : `/${file}`
  return (
    `@font-face {\n` +
    `  font-family: ${JSON.stringify(family)};\n` +
    `  src: url(${JSON.stringify(url)}) format('truetype');\n` +
    `  font-weight: ${weight};\n` +
    `  font-style: ${style};\n` +
    `  font-display: block;\n` +
    `}`
  )
}
