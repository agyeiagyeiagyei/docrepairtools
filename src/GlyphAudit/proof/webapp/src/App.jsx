import { useState, useEffect, useRef, useCallback } from 'react'
import Controls from './components/Controls'
import TextPanel from './components/TextPanel'
import { FALLBACK_CONFIG, injectFontFaces, loadProofConfig } from './proofConfig'
import './App.css'

const DEFAULT_BODY =
  'The quick brown fox jumps over the lazy dog Pack my box with five dozen liquor jugs How vexingly quick daft zebras jump'

// Base font-feature-settings applied to every editable region. Kept in sync
// with the panel-level rule in App.css; feature-spans concatenate onto this
// so wrapping a selection to enable e.g. `ss02` doesn't accidentally re-enable
// the ligatures or kerning we're explicitly suppressing for proofing.
// (Kerning off keeps the two panels' line breaks aligned — see App.css.)
const BASE_FEATURE_OFF = [
  "'liga' 0", "'clig' 0", "'dlig' 0", "'hlig' 0", "'calt' 0", "'kern' 0",
]
const featureCssFor = (tag) =>
  [...BASE_FEATURE_OFF, `'${tag}' 1`].join(', ')

// Wrap each visible character in the proof panel with a class describing
// the strongest signal about it. Two classes, applied in priority order:
//
//   .missing-glyph   — character has no glyph in the proof face at all
//                      (red wavy underline; structural absence).
//   .width-mismatch  — advance width diverges from the paired reference
//                      by more than 1u (amber text colour; the widths
//                      manifest is per-reference so this updates when
//                      the "Compare with" dropdown changes).
//
// The two classes are mutually exclusive per glyph — a missing character
// can't be measured, so it stays "missing" only. Tooltip carries the
// delta for width mismatches so the user can jump straight to the
// numeric divergence without opening the Glyphs.app panel.
function applyGlyphMarks(html, charSet, widthDeltas) {
  if (!charSet && !widthDeltas) return html
  let result = ''
  let inTag = false
  for (let i = 0; i < html.length; i++) {
    const ch = html[i]
    if (ch === '<') { inTag = true; result += ch; continue }
    if (ch === '>') { inTag = false; result += ch; continue }
    if (inTag) { result += ch; continue }
    // Handle HTML entities as opaque runs (`&amp;` etc.).
    if (ch === '&') {
      const semi = html.indexOf(';', i)
      if (semi !== -1 && semi - i < 8) {
        result += html.slice(i, semi + 1)
        i = semi
        continue
      }
    }
    const cp = ch.codePointAt(0)
    if (cp <= 32) { result += ch; continue }
    if (charSet && !charSet.has(cp)) {
      result += `<span class="missing-glyph">${ch}</span>`
      continue
    }
    if (widthDeltas) {
      const delta = widthDeltas.get(cp)
      if (delta !== undefined) {
        const sign = delta > 0 ? '+' : ''
        result += `<span class="width-mismatch" title="advance ${sign}${delta}u vs reference">${ch}</span>`
        continue
      }
    }
    result += ch
  }
  return result
}

function stripGlyphMarks(html) {
  // Both marker classes strip the same way — they wrap a single text run.
  return html
    .replace(/<span class="missing-glyph">([^<]*)<\/span>/g, '$1')
    .replace(/<span class="width-mismatch"(?:\s+title="[^"]*")?>([^<]*)<\/span>/g, '$1')
}

// Wrap the current Selection range with a <span data-otf-feature="TAG"> carrying
// the merged font-feature-settings string. No-op if the selection is empty or
// outside the Velarium panel. The right panel mirrors via syncToSystem.
function wrapSelectionWithFeature(panelEl, tag) {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0) return false
  const range = sel.getRangeAt(0)
  if (range.collapsed) return false
  if (!panelEl.contains(range.commonAncestorContainer)) return false
  const span = document.createElement('span')
  span.setAttribute('data-otf-feature', tag)
  span.setAttribute('style', `font-feature-settings: ${featureCssFor(tag)}`)
  try {
    range.surroundContents(span)
  } catch {
    // surroundContents throws on a range that partially selects a non-Text node.
    // Fall back to extract+wrap+reinsert, which handles split boundaries.
    const contents = range.extractContents()
    span.appendChild(contents)
    range.insertNode(span)
  }
  return true
}

// Unwrap every <span data-otf-feature> whose contents intersect the current
// selection. Right panel mirrors via syncToSystem.
function clearFeatureSpansInSelection(panelEl) {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0) return
  const range = sel.getRangeAt(0)
  if (!panelEl.contains(range.commonAncestorContainer)) return
  for (const span of Array.from(panelEl.querySelectorAll('span[data-otf-feature]'))) {
    if (range.intersectsNode(span)) {
      const parent = span.parentNode
      while (span.firstChild) parent.insertBefore(span.firstChild, span)
      parent.removeChild(span)
    }
  }
  panelEl.normalize()
}

function App() {
  const [proofConfig, setProofConfig] = useState(FALLBACK_CONFIG)
  const [configLoaded, setConfigLoaded] = useState(false)
  const [configReason, setConfigReason] = useState(null)
  const [systemFont, setSystemFont] = useState('')
  const [headlineSize, setHeadlineSize] = useState(48)
  const [bodySize, setBodySize] = useState(16)
  const [lineHeight, setLineHeight] = useState(1.4)
  const [letterSpacing, setLetterSpacing] = useState(0)
  const [fontLoaded, setFontLoaded] = useState(true)
  const [availableChars, setAvailableChars] = useState(null)
  const [availableFeatures, setAvailableFeatures] = useState(null)
  // Map<codepoint, delta> for the currently-selected reference. Loaded from
  // `/widths-roman-<slug>.json` (built by GlyphAudit.proof.write_width_manifests).
  // Null → not yet loaded / no reference selected; empty Map → loaded, no
  // mismatches. Both mean "no width marks on this render".
  const [widthDeltas, setWidthDeltas] = useState(null)

  const velariumRef = useRef(null)
  const systemRef = useRef(null)
  const familyName = proofConfig.familyName
  const referenceNames = proofConfig.references.map((r) => r.name)
  const chars_manifest = proofConfig.faces.roman?.chars || '/available-chars.json'
  const features_manifest = proofConfig.faces.roman?.features || '/available-features.json'

  useEffect(() => {
    let cancelled = false
    loadProofConfig().then(({ config, source, reason }) => {
      if (cancelled) return
      injectFontFaces(config)
      setProofConfig(config)
      setConfigLoaded(true)
      setConfigReason(source === 'fallback' ? reason : null)
      // Default the reference dropdown to the first reference the config
      // gave us, if any — otherwise leave the previous selection.
      if (config.references[0]?.name) {
        setSystemFont((prev) => prev || config.references[0].name)
      }
    })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!configLoaded) return
    // Force both proof faces to load up-front. Without this the italic face
    // is only fetched the first time an italic run appears in the DOM, which
    // produces a visible weight shift as the browser swaps synthetic-oblique-
    // on-Roman for the real italic on the next paint.
    const wantItalic = Boolean(proofConfig.faces.italic?.ttf)
    Promise.all([
      document.fonts.load(`16px "${familyName}"`),
      wantItalic ? document.fonts.load(`italic 16px "${familyName}"`) : Promise.resolve(),
    ]).finally(() => {
      const loaded =
        document.fonts.check(`16px "${familyName}"`) &&
        (!wantItalic || document.fonts.check(`italic 16px "${familyName}"`))
      setFontLoaded(loaded)
    })
  }, [configLoaded, familyName, proofConfig])

  useEffect(() => {
    if (!chars_manifest || !features_manifest) return
    const loadManifests = () => {
      fetch(chars_manifest, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : null))
        .then((codepoints) => {
          if (codepoints) setAvailableChars(new Set(codepoints))
        })
        .catch(() => {})
      fetch(features_manifest, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : null))
        .then((features) => {
          if (features) setAvailableFeatures(features)
        })
        .catch(() => {})
    }
    loadManifests()
    const interval = setInterval(loadManifests, 3000)
    return () => clearInterval(interval)
  }, [chars_manifest, features_manifest])

  // Load the width-mismatch manifest for the currently-selected reference.
  // Re-fetches whenever `systemFont` changes. Slug convention must stay
  // in sync with GlyphAudit.proof.build._slugify — kebabbed lowercase.
  useEffect(() => {
    if (!configLoaded || !systemFont) {
      setWidthDeltas(null)
      return
    }
    const slug = systemFont.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
    // Only the roman face here — italic width comparison is future work.
    const url = `/widths-roman-${slug}.json`
    let cancelled = false
    const load = () => {
      fetch(url, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : null))
        .then((entries) => {
          if (cancelled || !entries) return
          const m = new Map()
          for (const e of entries) m.set(e.cp, e.delta)
          setWidthDeltas(m)
        })
        .catch(() => {})
    }
    load()
    const interval = setInterval(load, 3000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [configLoaded, systemFont])

  const syncToSystem = useCallback(() => {
    const h = velariumRef.current?.getHeadlineHtml()
    const b = velariumRef.current?.getBodyHtml()
    if (h != null) systemRef.current?.setHeadlineHtml(stripGlyphMarks(h))
    if (b != null) systemRef.current?.setBodyHtml(stripGlyphMarks(b))
  }, [])

  const handleBold = useCallback(() => {
    if (velariumRef.current?.execCommand('bold')) {
      syncToSystem()
      return
    }
    systemRef.current?.execCommand('bold')
  }, [syncToSystem])

  const handleItalic = useCallback(() => {
    if (velariumRef.current?.execCommand('italic')) {
      syncToSystem()
      return
    }
    systemRef.current?.execCommand('italic')
  }, [syncToSystem])

  const handleApplyFeature = useCallback(
    (tag) => {
      const vPanel = document.querySelector('.panels > :first-child')
      if (!vPanel) return
      if (wrapSelectionWithFeature(vPanel, tag)) syncToSystem()
    },
    [syncToSystem]
  )

  const handleClearFeatures = useCallback(() => {
    const vPanel = document.querySelector('.panels > :first-child')
    if (!vPanel) return
    clearFeatureSpansInSelection(vPanel)
    syncToSystem()
  }, [syncToSystem])

  useEffect(() => {
    const vPanel = document.querySelector('.panels > :first-child')
    if (!vPanel) return
    vPanel.addEventListener('input', syncToSystem)
    return () => vPanel.removeEventListener('input', syncToSystem)
  }, [syncToSystem])

  // Apply per-character marks (missing-glyph + width-mismatch) to the proof
  // panel, but only when not actively editing so the caret doesn't jump.
  // Re-runs whenever the missing-char set OR the width-mismatch map changes,
  // so switching references paints the new reference's deltas.
  useEffect(() => {
    if (!availableChars && !widthDeltas) return
    const vPanel = document.querySelector('.panels > :first-child')
    if (!vPanel) return

    const applyUnderlines = () => {
      const editables = vPanel.querySelectorAll('.editable')
      for (const el of editables) {
        // Skip if this element or a child is focused
        if (el.contains(document.activeElement) || el === document.activeElement) continue
        const clean = stripGlyphMarks(el.innerHTML)
        const marked = applyGlyphMarks(clean, availableChars, widthDeltas)
        if (el.innerHTML !== marked) el.innerHTML = marked
      }
    }

    // Apply initially
    applyUnderlines()

    // Re-apply on blur (when user clicks away from an editable)
    const onFocusOut = (e) => {
      // Small delay to let focus settle
      setTimeout(applyUnderlines, 50)
    }

    vPanel.addEventListener('focusout', onFocusOut)
    return () => vPanel.removeEventListener('focusout', onFocusOut)
  }, [availableChars, widthDeltas])

  const sharedTypography = {
    lineHeight,
    letterSpacing: letterSpacing + 'em',
  }

  const defaultHeadline = familyName

  return (
    <div className="app">
      {!fontLoaded && (
        <div className="banner">
          Font not built — run the build to populate <code>proof-config.json</code>.
        </div>
      )}
      {configLoaded && configReason && (
        <div className="banner">
          Using fallback config (couldn't load <code>proof-config.json</code>: {configReason}).
        </div>
      )}

      <Controls
        systemFont={systemFont}
        onSystemFontChange={setSystemFont}
        referenceFonts={referenceNames}
        onBold={handleBold}
        onItalic={handleItalic}
        headlineSize={headlineSize}
        onHeadlineSizeChange={setHeadlineSize}
        bodySize={bodySize}
        onBodySizeChange={setBodySize}
        lineHeight={lineHeight}
        onLineHeightChange={setLineHeight}
        letterSpacing={letterSpacing}
        onLetterSpacingChange={setLetterSpacing}
        features={availableFeatures}
        onApplyFeature={handleApplyFeature}
        onClearFeatures={handleClearFeatures}
      />

      <div className="panels">
        <TextPanel
          ref={velariumRef}
          label={familyName}
          fontFamily={familyName}
          fontWeight={400}
          fontStyle={false}
          defaultHeadline={defaultHeadline}
          defaultBody={DEFAULT_BODY}
          headlineSize={headlineSize}
          bodySize={bodySize}
          typography={sharedTypography}
        />

        <TextPanel
          ref={systemRef}
          label={systemFont || '(no reference)'}
          fontFamily={systemFont || 'sans-serif'}
          fontWeight={400}
          fontStyle={false}
          defaultHeadline={defaultHeadline}
          defaultBody={DEFAULT_BODY}
          headlineSize={headlineSize}
          bodySize={bodySize}
          typography={sharedTypography}
          readOnly
        />
      </div>
    </div>
  )
}

export default App
