import { useEffect, useRef, useState } from 'react'

const STATUS_META = {
  compiled: {
    dot: 'compiled',
    label: 'Ready in proof font',
    hint: 'Compiled into the proof font. Click to apply to selection.',
  },
  'missing-glyphs': {
    dot: 'missing',
    label: 'Missing glyphs in source',
    hint: 'Defined in source but some referenced glyphs aren’t drawn yet — would not compile until they are.',
  },
  'needs-environment': {
    dot: 'needs-env',
    label: 'Needs classes / shared lookups',
    hint: 'Uses @classes or external lookups that the proof build currently strips. Would not compile in the proof font yet.',
  },
  disabled: {
    dot: 'disabled',
    label: 'Disabled in source',
    hint: 'Defined in source but unchecked in Glyphs.app — won’t compile until enabled there.',
  },
}

const STATUS_ORDER = ['compiled', 'missing-glyphs', 'needs-environment', 'disabled']

export default function FeatureMenu({ features, onApply, onClear, disabled }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)

  useEffect(() => {
    if (!open) return
    const onDocClick = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [open])

  const grouped = STATUS_ORDER
    .map((status) => ({
      status,
      meta: STATUS_META[status],
      items: (features || []).filter((f) => f.status === status),
    }))
    .filter((g) => g.items.length > 0)

  return (
    <div className="feature-menu" ref={rootRef}>
      <button
        className="toggle feature-menu-trigger"
        onMouseDown={(e) => {
          e.preventDefault() // keep selection alive on the proof panel
          setOpen((o) => !o)
        }}
        disabled={disabled}
        title={
          disabled
            ? 'Features manifest not loaded yet — build the proof font.'
            : 'Apply OpenType feature to selection'
        }
      >
        Features ▾
      </button>

      {open && (
        <div className="feature-menu-dropdown">
          <button
            className="feature-menu-item feature-menu-clear"
            onMouseDown={(e) => {
              e.preventDefault()
              onClear()
              setOpen(false)
            }}
          >
            Clear features in selection
          </button>

          {grouped.length === 0 && (
            <div className="feature-menu-empty">No features in source.</div>
          )}

          {grouped.map(({ status, meta, items }) => (
            <div key={status} className="feature-menu-group">
              <div className="feature-menu-group-label" title={meta.hint}>
                <span className={`feature-dot feature-dot-${meta.dot}`} />
                {meta.label}
              </div>
              {items.map((f) => (
                <button
                  key={f.tag}
                  className={`feature-menu-item feature-menu-item-${meta.dot}`}
                  onMouseDown={(e) => {
                    e.preventDefault()
                    if (status === 'compiled') {
                      onApply(f.tag)
                      setOpen(false)
                    }
                  }}
                  disabled={status !== 'compiled'}
                  title={
                    status === 'compiled'
                      ? meta.hint
                      : `${meta.hint}${
                          f.missingGlyphs?.length
                            ? '\nMissing: ' + f.missingGlyphs.slice(0, 8).join(', ') +
                              (f.missingGlyphs.length > 8 ? ', …' : '')
                            : ''
                        }`
                  }
                >
                  <span className="feature-tag">{f.tag}</span>
                  <span className="feature-name">{f.name}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
