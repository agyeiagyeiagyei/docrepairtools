import { useRef, useImperativeHandle, forwardRef } from 'react'

const TextPanel = forwardRef(function TextPanel(
  {
    label,
    fontFamily,
    fontWeight,
    fontStyle,
    headlineSize,
    bodySize,
    typography,
    defaultHeadline,
    defaultBody,
    readOnly,
  },
  ref
) {
  const panelRef = useRef(null)
  const headlineRef = useRef(null)
  const bodyRef = useRef(null)

  useImperativeHandle(ref, () => ({
    execCommand(command) {
      const sel = window.getSelection()
      if (!sel || sel.rangeCount === 0) return false
      if (panelRef.current && panelRef.current.contains(sel.anchorNode)) {
        document.execCommand(command, false, null)
        return true
      }
      return false
    },
    getHeadlineHtml() {
      return headlineRef.current?.innerHTML || ''
    },
    getBodyHtml() {
      return bodyRef.current?.innerHTML || ''
    },
    setHeadlineHtml(html) {
      const el = headlineRef.current
      if (el && !el.contains(document.activeElement) && el !== document.activeElement) {
        el.innerHTML = html
      }
    },
    setBodyHtml(html) {
      const el = bodyRef.current
      if (el && !el.contains(document.activeElement) && el !== document.activeElement) {
        el.innerHTML = html
      }
    },
  }))

  // Paste plain text only. Chrome's contentEditable paste preserves the
  // source's inline styles, and a pasted `font-family` on a child span beats
  // the panel's own font — so pasting a specimen string from a web page (or
  // out of the reference panel next door) silently swaps the proof panel to
  // whatever font the text was copied from, and `syncToSystem` then mirrors
  // that span into the reference panel too. Both panels end up rendering the
  // same wrong face. Strip formatting on the way in; the panel's own controls
  // are the only thing that should decide how this text is set.
  const handlePaste = (e) => {
    e.preventDefault()
    const text = e.clipboardData?.getData('text/plain') ?? ''
    if (text) document.execCommand('insertText', false, text)
  }

  const sharedStyle = {
    fontFamily: `"${fontFamily}", sans-serif`,
    fontWeight,
    fontStyle: fontStyle ? 'italic' : 'normal',
    lineHeight: typography.lineHeight,
    letterSpacing: typography.letterSpacing,
  }

  return (
    <div className="panel" ref={panelRef}>
      <div className="panel-label">{label}</div>
      <div
        ref={headlineRef}
        className="editable"
        contentEditable={!readOnly}
        suppressContentEditableWarning
        onPaste={handlePaste}
        style={{ ...sharedStyle, fontSize: headlineSize + 'px' }}
      >
        {defaultHeadline}
      </div>
      <div
        ref={bodyRef}
        className="editable body"
        contentEditable={!readOnly}
        suppressContentEditableWarning
        onPaste={handlePaste}
        style={{ ...sharedStyle, fontSize: bodySize + 'px' }}
      >
        {defaultBody}
      </div>
    </div>
  )
})

export default TextPanel
