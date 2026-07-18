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
        style={{ ...sharedStyle, fontSize: headlineSize + 'px' }}
      >
        {defaultHeadline}
      </div>
      <div
        ref={bodyRef}
        className="editable body"
        contentEditable={!readOnly}
        suppressContentEditableWarning
        style={{ ...sharedStyle, fontSize: bodySize + 'px' }}
      >
        {defaultBody}
      </div>
    </div>
  )
})

export default TextPanel
