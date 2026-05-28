import FeatureMenu from './FeatureMenu'

// System fonts every macOS / Windows install ships. The build-supplied
// `referenceFontFamilies` (proper licensed copies with full GSUB features)
// are merged in at the top of the list so they're the default pick.
const SYSTEM_FONTS = [
  'Arial',
  'Georgia',
  'Times New Roman',
  'Courier New',
  'Trebuchet MS',
  'Palatino',
]

const SIZE_PRESETS = [12, 16, 24, 36, 48, 72]

export default function Controls({
  systemFont,
  onSystemFontChange,
  referenceFontFamilies = [],
  onBold,
  onItalic,
  bodySize,
  onBodySizeChange,
  headlineSize,
  onHeadlineSizeChange,
  lineHeight,
  onLineHeightChange,
  letterSpacing,
  onLetterSpacingChange,
  features,
  onApplyFeature,
  onClearFeatures,
}) {
  const fontOptions = [
    ...referenceFontFamilies,
    ...SYSTEM_FONTS.filter((f) => !referenceFontFamilies.includes(f)),
  ]
  return (
    <div className="controls">
      <label>
        Compare with:
        <select
          value={systemFont}
          onChange={(e) => onSystemFontChange(e.target.value)}
        >
          {fontOptions.map((f) => (
            <option key={f} value={f}>
              {f}
            </option>
          ))}
        </select>
      </label>

      <div className="separator" />

      <button
        className="toggle"
        onMouseDown={(e) => {
          e.preventDefault() // keep selection alive
          onBold()
        }}
      >
        <strong>B</strong>
      </button>

      <button
        className="toggle"
        onMouseDown={(e) => {
          e.preventDefault()
          onItalic()
        }}
      >
        <em>I</em>
      </button>

      <FeatureMenu
        features={features}
        onApply={onApplyFeature}
        onClear={onClearFeatures}
        disabled={!features}
      />

      <div className="separator" />

      <label>
        Headline:
        <select
          value={headlineSize}
          onChange={(e) => onHeadlineSizeChange(Number(e.target.value))}
        >
          {SIZE_PRESETS.map((s) => (
            <option key={s} value={s}>
              {s}px
            </option>
          ))}
        </select>
      </label>

      <label>
        Body:
        <input
          type="range"
          className="size-slider"
          min="10"
          max="72"
          value={bodySize}
          onChange={(e) => onBodySizeChange(Number(e.target.value))}
        />
        <span>{bodySize}px</span>
      </label>

      <div className="separator" />

      <label>
        Line height:
        <input
          type="range"
          className="size-slider"
          min="0.8"
          max="2.5"
          step="0.05"
          value={lineHeight}
          onChange={(e) => onLineHeightChange(Number(e.target.value))}
        />
        <span>{lineHeight.toFixed(2)}</span>
      </label>

      <label>
        Tracking:
        <input
          type="range"
          className="size-slider"
          min="-0.1"
          max="0.3"
          step="0.005"
          value={letterSpacing}
          onChange={(e) => onLetterSpacingChange(Number(e.target.value))}
        />
        <span>{letterSpacing.toFixed(3)}em</span>
      </label>
    </div>
  )
}
