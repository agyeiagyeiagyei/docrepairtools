import { useState, useEffect, useRef, useCallback } from 'react'
import Controls from './components/Controls'
import TextPanel from './components/TextPanel'
import './App.css'

// Fallback values used until proof-config.json loads (or if the build script
// hasn't run yet). The build script writes proof-config.json into public/
// alongside the proof font; we fetch it on startup.
const FALLBACK_PROOF_FAMILY = 'Proof'
const FALLBACK_HEADLINE = 'Proof'
const DEFAULT_BODY =
  'The quick brown fox jumps over the lazy dog Pack my box with five dozen liquor jugs How vexingly quick daft zebras jump'

// Inject @font-face declarations for the proof font (built locally) and any
// reference fonts the config lists. Idempotent — replaces a single <style>
// node tagged with our id on every config change.
function injectFontFaces(config) {
  const id = 'glyphaudit-preview-fontfaces'
  let style = document.getElementById(id)
  if (!style) {
    style = document.createElement('style')
    style.id = id
    document.head.appendChild(style)
  }
  const rules = []
  if (config?.proofFont?.file) {
    rules.push(
      `@font-face {`,
      `  font-family: '${config.proofFont.family}';`,
      `  src: url('/${config.proofFont.file}') format('truetype');`,
      `  font-weight: ${config.proofFont.weight || '100 900'};`,
      `  font-style: ${config.proofFont.style || 'normal'};`,
      `}`,
    )
  }
  for (const ref of (config?.referenceFonts || [])) {
    for (const [styleKey, file] of Object.entries(ref.files || {})) {
      const [weight, fontStyle] =
        styleKey === 'regular'    ? ['400', 'normal'] :
        styleKey === 'bold'       ? ['700', 'normal'] :
        styleKey === 'italic'     ? ['400', 'italic'] :
        styleKey === 'boldItalic' ? ['700', 'italic'] :
                                    ['400', 'normal']
      rules.push(
        `@font-face {`,
        `  font-family: '${ref.family}';`,
        `  src: url('/${file}') format('truetype');`,
        `  font-weight: ${weight};`,
        `  font-style: ${fontStyle};`,
        `}`,
      )
    }
  }
  style.textContent = rules.join('\n')
}

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

function addMissingUnderlines(html, charSet) {
  if (!charSet) return html
  // Walk through text content, wrapping missing chars
  // Preserve existing HTML tags
  let result = ''
  let inTag = false
  for (let i = 0; i < html.length; i++) {
    const ch = html[i]
    if (ch === '<') { inTag = true; result += ch; continue }
    if (ch === '>') { inTag = false; result += ch; continue }
    if (inTag) { result += ch; continue }
    // Handle HTML entities
    if (ch === '&') {
      const semi = html.indexOf(';', i)
      if (semi !== -1 && semi - i < 8) {
        result += html.slice(i, semi + 1)
        i = semi
        continue
      }
    }
    const cp = ch.codePointAt(0)
    if (cp > 32 && !charSet.has(cp)) {
      result += `<span class="missing-glyph">${ch}</span>`
    } else {
      result += ch
    }
  }
  return result
}

function stripMissingUnderlines(html) {
  return html.replace(/<span class="missing-glyph">([^<]*)<\/span>/g, '$1')
}

// Wrap the current Selection range with a <span data-otf-feature="TAG"> carrying
// the merged font-feature-settings string. No-op if the selection is empty or
// outside the proof panel. The right (reference) panel mirrors via syncToSystem.
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
  const [config, setConfig] = useState(null)
  const [systemFont, setSystemFont] = useState(null)
  const [headlineSize, setHeadlineSize] = useState(48)
  const [bodySize, setBodySize] = useState(16)
  const [lineHeight, setLineHeight] = useState(1.4)
  const [letterSpacing, setLetterSpacing] = useState(0)
  const [fontLoaded, setFontLoaded] = useState(true)
  const [availableChars, setAvailableChars] = useState(null)
  const [availableFeatures, setAvailableFeatures] = useState(null)

  const proofRef = useRef(null)
  const systemRef = useRef(null)

  const proofFamily = config?.proofFont?.family || FALLBACK_PROOF_FAMILY
  const proofLabel = config?.proofFont?.label || proofFamily
  const defaultHeadline = config?.defaults?.headline || FALLBACK_HEADLINE
  const defaultBody = config?.defaults?.body || DEFAULT_BODY
  const referenceFontFamilies =
    (config?.referenceFonts || []).map((r) => r.family)
  const systemFontFamily = systemFont ?? referenceFontFamilies[0] ?? 'Verdana'

  useEffect(() => {
    document.fonts.ready.then(() => {
      const loaded = document.fonts.check(`16px "${proofFamily}"`)
      setFontLoaded(loaded)
    })
  }, [proofFamily])

  useEffect(() => {
    const loadManifests = () => {
      fetch('/proof-config.json', { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : null))
        .then((c) => {
          if (c) {
            setConfig(c)
            injectFontFaces(c)
          }
        })
        .catch(() => {})
      fetch('/available-chars.json', { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : null))
        .then((codepoints) => {
          if (codepoints) setAvailableChars(new Set(codepoints))
        })
        .catch(() => {})
      fetch('/available-features.json', { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : null))
        .then((features) => {
          if (features) setAvailableFeatures(features)
        })
        .catch(() => {})
    }
    loadManifests()
    const interval = setInterval(loadManifests, 3000)
    return () => clearInterval(interval)
  }, [])

  const syncToSystem = useCallback(() => {
    const h = proofRef.current?.getHeadlineHtml()
    const b = proofRef.current?.getBodyHtml()
    if (h != null) systemRef.current?.setHeadlineHtml(stripMissingUnderlines(h))
    if (b != null) systemRef.current?.setBodyHtml(stripMissingUnderlines(b))
  }, [])

  const handleBold = useCallback(() => {
    if (proofRef.current?.execCommand('bold')) {
      syncToSystem()
      return
    }
    systemRef.current?.execCommand('bold')
  }, [syncToSystem])

  const handleItalic = useCallback(() => {
    if (proofRef.current?.execCommand('italic')) {
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

  // Apply underlines to missing chars, but only when not actively editing
  useEffect(() => {
    if (!availableChars) return
    const vPanel = document.querySelector('.panels > :first-child')
    if (!vPanel) return

    const applyUnderlines = () => {
      const editables = vPanel.querySelectorAll('.editable')
      for (const el of editables) {
        // Skip if this element or a child is focused
        if (el.contains(document.activeElement) || el === document.activeElement) continue
        const clean = stripMissingUnderlines(el.innerHTML)
        const marked = addMissingUnderlines(clean, availableChars)
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
  }, [availableChars])

  const sharedTypography = {
    lineHeight,
    letterSpacing: letterSpacing + 'em',
  }

  return (
    <div className="app">
      {!fontLoaded && (
        <div className="banner">
          Proof font not loaded — run the build (e.g. <code>python build.py</code>) first.
        </div>
      )}

      <Controls
        systemFont={systemFontFamily}
        onSystemFontChange={setSystemFont}
        referenceFontFamilies={referenceFontFamilies}
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
          ref={proofRef}
          label={proofLabel}
          fontFamily={proofFamily}
          fontWeight={400}
          fontStyle={false}
          defaultHeadline={defaultHeadline}
          defaultBody={defaultBody}
          headlineSize={headlineSize}
          bodySize={bodySize}
          typography={sharedTypography}
        />

        <TextPanel
          ref={systemRef}
          label={systemFontFamily}
          fontFamily={systemFontFamily}
          fontWeight={400}
          fontStyle={false}
          defaultHeadline={defaultHeadline}
          defaultBody={defaultBody}
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
