---
version: alpha
name: Sentri-Inspired-design-analysis
description: An inspired interpretation of Sentri's design language — a developer-tools brand built on a deep purple-violet midnight canvas, electric lime accents, and a slightly subversive illustrated personality. The system pairs a custom display sans (chunky, playful, near-condensed) with the open Rubik family for UI copy and Monaco for code, then leans on dark-on-light pricing surfaces, sticker-style mascots, and a single-color CTA hierarchy where black-violet buttons read as the primary action against either polarity.
colors:
  primary: "#150f23"
  ink-deep: "#1f1633"
  on-primary: "#ffffff"
  accent-lime: "#c2ef4e"
  accent-pink: "#fa7faa"
  accent-violet: "#6a5fc1"
  accent-violet-deep: "#422082"
  accent-violet-mid: "#79628c"
  surface-canvas-dark: "#1f1633"
  surface-canvas-light: "#ffffff"
  surface-night: "#150f23"
  surface-press-light: "#f0f0f0"
  surface-press-stronger: "#efefef"
  hairline-violet: "#362d59"
  hairline-cool: "#cfcfdb"
  hairline-cloud: "#e5e7eb"
  ink: "#1f1633"
  ink-press: "#1a1a1a"
  on-dark-muted: "#bdb8c0"
  on-dark-faint: "#3f3849"
  ring-focus: "#9dc1f5"
typography:
  display-hero:
    fontFamily: "Sentri Display, Rubik, system-ui, sans-serif"
    fontSize: 88px
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: 0
  display-large:
    fontFamily: "Sentri Display, Rubik, system-ui, sans-serif"
    fontSize: 60px
    fontWeight: 500
    lineHeight: 1.1
    letterSpacing: 0
  heading-xl:
    fontFamily: "Rubik, -apple-system, system-ui, Segoe UI, Helvetica, Arial, sans-serif"
    fontSize: 30px
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: 0
  heading-lg:
    fontFamily: "Rubik, -apple-system, system-ui, sans-serif"
    fontSize: 27px
    fontWeight: 500
    lineHeight: 1.25
    letterSpacing: 0
  heading-md:
    fontFamily: "Rubik, -apple-system, system-ui, sans-serif"
    fontSize: 24px
    fontWeight: 500
    lineHeight: 1.25
    letterSpacing: 0
  heading-sm:
    fontFamily: "Rubik, -apple-system, system-ui, sans-serif"
    fontSize: 20px
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: 0
  body-lg:
    fontFamily: "Rubik, -apple-system, system-ui, sans-serif"
    fontSize: 16px
    fontWeight: 400
    lineHeight: 2.0
    letterSpacing: 0
  body-strong:
    fontFamily: "Rubik, -apple-system, system-ui, sans-serif"
    fontSize: 16px
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: 0
  body-md:
    fontFamily: "Rubik, -apple-system, system-ui, sans-serif"
    fontSize: 16px
    fontWeight: 500
    lineHeight: 1.5
    letterSpacing: 0
  eyebrow:
    fontFamily: "Rubik, -apple-system, system-ui, sans-serif"
    fontSize: 15px
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: 0
  button-cap:
    fontFamily: "Rubik, -apple-system, system-ui, sans-serif"
    fontSize: 14px
    fontWeight: 700
    lineHeight: 1.14
    letterSpacing: 0.2px
  button-cap-light:
    fontFamily: "Rubik, -apple-system, system-ui, sans-serif"
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.29
    letterSpacing: 0.2px
  caption:
    fontFamily: "Rubik, -apple-system, system-ui, sans-serif"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.43
    letterSpacing: 0
  micro-cap:
    fontFamily: "Rubik, -apple-system, system-ui, sans-serif"
    fontSize: 10px
    fontWeight: 600
    lineHeight: 1.8
    letterSpacing: 0.25px
  code:
    fontFamily: "Monaco, Menlo, Ubuntu Mono, monospace"
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: 0
  code-strong:
    fontFamily: "Monaco, Menlo, Ubuntu Mono, monospace"
    fontSize: 16px
    fontWeight: 700
    lineHeight: 1.5
    letterSpacing: 0
rounded:
  xs: 4px
  sm: 6px
  md: 8px
  lg: 10px
  xl: 12px
  xxl: 18px
  full: 9999px
spacing:
  xxs: 2px
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  xxl: 32px
  section: 96px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.button-cap}"
    rounded: "{rounded.md}"
    padding: 12px 16px
  button-primary-pressed:
    backgroundColor: "{colors.surface-press-stronger}"
    textColor: "{colors.ink-press}"
    typography: "{typography.button-cap}"
    rounded: "{rounded.md}"
    padding: 12px 16px
  button-inverted:
    backgroundColor: "{colors.on-primary}"
    textColor: "{colors.ink-deep}"
    typography: "{typography.button-cap}"
    rounded: "{rounded.md}"
    padding: 12px 16px
  button-inverted-pressed:
    backgroundColor: "{colors.surface-press-light}"
    textColor: "{colors.ink-press}"
    typography: "{typography.button-cap}"
    rounded: "{rounded.md}"
    padding: 12px 16px
  button-ghost-on-dark:
    backgroundColor: "{colors.on-dark-faint}"
    textColor: "{colors.on-primary}"
    typography: "{typography.button-cap}"
    rounded: "{rounded.xl}"
    padding: 8px
  button-violet-token:
    backgroundColor: "{colors.accent-violet-mid}"
    textColor: "{colors.on-primary}"
    typography: "{typography.button-cap-light}"
    rounded: "{rounded.xl}"
    padding: 8px 16px
  button-disabled:
    backgroundColor: "{colors.hairline-cloud}"
    textColor: "{colors.on-dark-muted}"
    typography: "{typography.button-cap}"
    rounded: "{rounded.md}"
    padding: 12px 16px
  pill-neutral-dark:
    backgroundColor: "{colors.surface-night}"
    textColor: "{colors.on-primary}"
    typography: "{typography.caption}"
    rounded: "{rounded.xs}"
    padding: 4px 8px
  chip-lime-keyword:
    backgroundColor: "{colors.accent-lime}"
    textColor: "{colors.ink-deep}"
    typography: "{typography.display-hero}"
    rounded: "{rounded.xs}"
    padding: 0 12px
  text-input:
    backgroundColor: "{colors.surface-canvas-light}"
    textColor: "{colors.ink-deep}"
    typography: "{typography.body-md}"
    rounded: "{rounded.sm}"
    padding: 8px 12px
  text-input-focused:
    backgroundColor: "{colors.surface-canvas-light}"
    textColor: "{colors.ink-deep}"
    typography: "{typography.body-md}"
    rounded: "{rounded.sm}"
    padding: 8px 12px
  select-violet:
    backgroundColor: "{colors.accent-violet-deep}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body-md}"
    rounded: "{rounded.md}"
    padding: 8px 16px
  card-pricing:
    backgroundColor: "{colors.surface-canvas-light}"
    textColor: "{colors.ink-deep}"
    typography: "{typography.body-md}"
    rounded: "{rounded.xl}"
    padding: 32px
  card-pricing-featured:
    backgroundColor: "{colors.surface-night}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body-md}"
    rounded: "{rounded.xl}"
    padding: 32px
  card-feature-dark:
    backgroundColor: "{colors.ink-deep}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body-lg}"
    rounded: "{rounded.xxl}"
    padding: 32px
  card-spotlight-violet:
    backgroundColor: "{colors.accent-violet-deep}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body-lg}"
    rounded: "{rounded.xxl}"
    padding: 32px
  code-block:
    backgroundColor: "{colors.surface-night}"
    textColor: "{colors.on-primary}"
    typography: "{typography.code}"
    rounded: "{rounded.md}"
    padding: 16px
  link-on-dark:
    backgroundColor: "{colors.surface-canvas-dark}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body-md}"
    rounded: "{rounded.xs}"
    padding: 0px
  link-on-light:
    backgroundColor: "{colors.surface-canvas-light}"
    textColor: "{colors.ink-deep}"
    typography: "{typography.body-md}"
    rounded: "{rounded.xs}"
    padding: 0px
  nav-bar-light:
    backgroundColor: "{colors.surface-canvas-light}"
    textColor: "{colors.ink-deep}"
    typography: "{typography.body-md}"
    rounded: "{rounded.xs}"
    padding: 16px 24px
  footer-light:
    backgroundColor: "{colors.surface-canvas-light}"
    textColor: "{colors.ink-deep}"
    typography: "{typography.caption}"
    rounded: "{rounded.xs}"
    padding: 32px 24px
---
## Overview
Sentri's design language reads like a debugging console wearing a leather jacket. The home and product surfaces sit on a near-black violet midnight (`{colors.surface-canvas-dark}` / `{colors.surface-night}`), strewn with starfield textures and floating sticker-style mascots — astronauts, monsters, traffic cones — that puncture the seriousness of an observability product. Headlines run in a chunky proprietary display sans where the most important keywords are wrapped in lime-green highlight chips (`{colors.accent-lime}`), as if the copy itself has been marked up by a developer redlining their own console output.
The palette is deliberately narrow: deep midnight as the dominant canvas, electric lime as the primary attention-grabber, hot pink (`{colors.accent-pink}`) as a secondary punctuation, and a violet-mid (`{colors.accent-violet-mid}`) for tag chips and hairline strokes. White appears in two roles — as text on dark, and as the canvas for pricing, contact, and content-heavy pages where developers need to scan dense tables. The "single primary CTA" is visually inverted depending on context: filled black-violet (`{colors.primary}`) with white type on light surfaces, or filled white with dark type on dark surfaces. The button always reads as the strongest UI affordance regardless of polarity.
Typography splits cleanly between three families: a custom display sans for hero and section openers (chunky, near-condensed, slightly playful), Rubik for every UI text role (body, captions, eyebrow caps, button labels), and Monaco for code. Buttons and eyebrows almost always run in uppercase with a 0.2px tracking lift to give them the snap of console output.
**Key Characteristics:**
- Two-polarity canvas system: deep violet midnight (`{colors.surface-canvas-dark}`) for marketing hero and product feature pages, white (`{colors.surface-canvas-light}`) for pricing, contact, and dense reference content — the system never tries to blur the two.
- Lime keyword highlight (`{colors.accent-lime}`) treated as a typographic device, not a color swatch — it wraps single words inside the display headline to act as a syntax highlight on the reading flow.
- Sticker illustration system: floating mascot characters with hand-drawn outlines, appearing at section junctions, never inside cards — they create rhythm and personality between dense info blocks.
- Uppercase eyebrow + button caps in `{typography.button-cap}` and `{typography.eyebrow}`, with a consistent 0.2px tracking lift, give the brand its "developer console" cadence.
- Single-primary CTA hierarchy: every page has one filled button reading either `{colors.primary}` on light or `{colors.on-primary}` on dark; outlined and ghost variants are downgraded.
- Card surfaces follow the canvas: dark sections nest dark cards (`{colors.ink-deep}` with subtle hairline) and light sections nest white cards with `{colors.hairline-cloud}` borders — chrome stays consistent, only the polarity flips.
- A pricing-page color rhythm of cream-white tiers with one dark inverted "featured" tier (`{colors.surface-night}`), avoiding the typical accent-bordered featured pattern.
## Colors
> **Source pages:** home (`/welcome/`), product/error-monitoring, contact/enterprise, pricing.
### Brand & Accent
- **Midnight Violet** (`{colors.primary}` — `#150f23`): The system's primary action color and the deepest surface tone. Used for filled primary buttons on light surfaces, code-block backgrounds, and the strongest dark cards.
- **Ink Violet** (`{colors.ink-deep}` — `#1f1633`): Slightly lifted from primary, this is the marketing hero canvas and the default body-text color on light surfaces — a single token doing double duty as background and ink.
- **Electric Lime** (`{colors.accent-lime}` — `#c2ef4e`): The signature highlight color. Wrapped around individual headline keywords as a syntax-highlight chip (`{rounded.xs}` corner, no padding-y, 12px padding-x). Also used as the squiggly footer divider stroke. Never a button background.
- **Hot Pink** (`{colors.accent-pink}` — `#fa7faa`): Secondary punctuation color used for sticker outlines, chart points, and supporting accents — never on buttons, never on type at body size.
- **Violet Link** (`{colors.accent-violet}` — `#6a5fc1`): Inline link color when emphasis is needed beyond underline.
- **Deep Violet** (`{colors.accent-violet-deep}` — `#422082`): The select-dropdown fill on contact forms; also used on spotlight cards inside dark sections.
- **Mid Violet** (`{colors.accent-violet-mid}` — `#79628c`): Tag-chip fill and faint accent on dark surfaces.
### Surface
- **Dark Canvas** (`{colors.surface-canvas-dark}` — `#1f1633`): Hero, product, and feature-page background. Carries the deepest atmospheric weight.
- **Night** (`{colors.surface-night}` — `#150f23`): Cards on dark canvas, code blocks, and the "featured" pricing tier.
- **Light Canvas** (`{colors.surface-canvas-light}` — `#ffffff`): Pricing, contact, and dense-reference page background.
- **Surface Press Light** (`{colors.surface-press-light}` — `#f0f0f0`) and **Press Stronger** (`{colors.surface-press-stronger}` — `#efefef`): The pressed/active fill of inverted buttons on dark surfaces.
- **Hairline Violet** (`{colors.hairline-violet}` — `#362d59`): 1px borders on dark cards.
- **Hairline Cool** (`{colors.hairline-cool}` — `#cfcfdb`): 1px borders on text inputs and form fields.
- **Hairline Cloud** (`{colors.hairline-cloud}` — `#e5e7eb`): Pricing-table dividers and pricing-card borders on light canvas.
### Text
- **On Primary** (`{colors.on-primary}` — `#ffffff`): All text on dark canvas, all CTA labels on filled dark buttons.
- **Ink** (`{colors.ink}` — `#1f1633`): Body text on light canvas; identical hex to the dark canvas, repurposed as type.
- **Ink Press** (`{colors.ink-press}` — `#1a1a1a`): Reserved for the pressed/active state of inverted buttons.
- **On Dark Muted** (`{colors.on-dark-muted}` — `rgba(255,255,255,0.72)`): Secondary text, captions, and table cell values on dark canvas.
- **On Dark Faint** (`{colors.on-dark-faint}` — `rgba(255,255,255,0.18)`): Translucent surface-on-dark — used for ghost button fills and dimmed nav items.
### Semantic
- **Focus Ring** (`{colors.ring-focus}` — `rgba(59,130,246,0.5)`): Translucent blue focus ring — the only blue in the system, reserved for keyboard focus on form fields.
## Typography
### Font Family
The display tier is a proprietary geometric sans with chunky, near-condensed proportions and a slightly subversive personality (closing apertures, optical-stress letterforms). When unavailable, fall back to **Rubik** at heavier weights for visual continuity.
The UI tier is **Rubik** — an open-source Hebrew/Latin sans on Google Fonts — with system fallbacks (`-apple-system, system-ui, Segoe UI, Helvetica, Arial`). Rubik handles every body, caption, button, and eyebrow role.
The code tier is **Monaco** with Menlo and Ubuntu Mono fallbacks — used in code blocks, install snippets, and inline tokens.
### Hierarchy
| Token | Size | Weight | Line Height | Letter Spacing | Use |
|---|---|---|---|---|---|
| `{typography.display-hero}` | 88px | 700 | 1.2 | 0 | Marketing hero headline (single line of attention) |
| `{typography.display-large}` | 60px | 500 | 1.1 | 0 | Section openers on dark surfaces |
| `{typography.heading-xl}` | 30px | 500 | 1.2 | 0 | Page titles on light surfaces |
| `{typography.heading-lg}` | 27px | 500 | 1.25 | 0 | Sub-section headings, large card titles |
| `{typography.heading-md}` | 24px | 500 | 1.25 | 0 | Card titles, in-page section headings |
| `{typography.heading-sm}` | 20px | 600 | 1.25 | 0 | Compact card title, list-group title |
| `{typography.body-lg}` | 16px | 400 | 2.0 | 0 | Hero subtext — airy, two-line-leading |
| `{typography.body-strong}` | 16px | 600 | 1.5 | 0 | Emphasized body run, lead sentence |
| `{typography.body-md}` | 16px | 500 | 1.5 | 0 | Default UI body, label, list item |
| `{typography.eyebrow}` | 15px | 500 | 1.4 | 0 | Section eyebrow — all-caps on brand pages |
| `{typography.button-cap}` | 14px | 700 | 1.14 | 0.2px | Button labels — all-caps by default |
| `{typography.button-cap-light}` | 14px | 500 | 1.29 | 0.2px | Lighter button variant for secondary CTAs |
| `{typography.caption}` | 14px | 400 | 1.43 | 0 | Captions, footnotes, small print |
| `{typography.micro-cap}` | 10px | 600 | 1.8 | 0.25px | Overline, micro-label, tag |
| `{typography.code}` | 16px | 400 | 1.5 | 0 | Code block, inline code |
| `{typography.code-strong}` | 16px | 700 | 1.5 | 0 | Command emphasis, token highlights in code |
## Component Stylings
### Buttons
Sentri maintains a strict single-primary hierarchy: exactly one filled button per surface reads as the CTA. The color flips between `{colors.primary}` on light and `{colors.on-primary}` on dark.
| Token | Background | Text | Padding | Radius | Use |
|---|---|---|---|---|---|
| `{components.button-primary}` | `{colors.primary}` | `{colors.on-primary}` | 12px 16px | `{rounded.md}` | Primary CTA on light surfaces |
| `{components.button-primary-pressed}` | `{colors.surface-press-stronger}` | `{colors.ink-press}` | 12px 16px | `{rounded.md}` | Primary CTA pressed state |
| `{components.button-inverted}` | `{colors.on-primary}` | `{colors.ink-deep}` | 12px 16px | `{rounded.md}` | Primary CTA on dark surfaces |
| `{components.button-inverted-pressed}` | `{colors.surface-press-light}` | `{colors.ink-press}` | 12px 16px | `{rounded.md}` | Inverted pressed state |
| `{components.button-ghost-on-dark}` | `{colors.on-dark-faint}` | `{colors.on-primary}` | 8px | `{rounded.xl}` | Ghost nav buttons on dark |
| `{components.button-violet-token}` | `{colors.accent-violet-mid}` | `{colors.on-primary}` | 8px 16px | `{rounded.xl}` | Tag/token buttons, violet |
| `{components.button-disabled}` | `{colors.hairline-cloud}` | `{colors.on-dark-muted}` | 12px 16px | `{rounded.md}` | Disabled button |
**Button rules:**
- All-caps by default using `{typography.button-cap}` (700 weight, 0.2px tracking).
- Hover on filled buttons: brightness lifted 8-10% — no color shift, just luminance.
- 1px transparent border by default so ghost and filled buttons share the same box height.
### Cards
| Token | Background | Rounded | Padding | Use |
|---|---|---|---|---|
| `{components.card-pricing}` | `{colors.surface-canvas-light}` | `{rounded.xl}` | 32px | Standard pricing tier card |
| `{components.card-pricing-featured}` | `{colors.surface-night}` | `{rounded.xl}` | 32px | Featured/highlighted tier — dark on light canvas |
| `{components.card-feature-dark}` | `{colors.ink-deep}` | `{rounded.xxl}` | 32px | Feature card on dark canvas |
| `{components.card-spotlight-violet}` | `{colors.accent-violet-deep}` | `{rounded.xxl}` | 32px | Spotlight/CTA card on dark canvas |
**Card rules:**
- Cards on dark canvas use `{colors.ink-deep}` with `{colors.hairline-violet}` 1px border.
- Cards on light canvas use `{colors.surface-canvas-light}` with `{colors.hairline-cloud}` 1px border.
- Card hover: border darkens, a 4px-soft shadow drops, and the card lifts 2px on translateY.
- No color shift on hover — only depth changes.
### Inputs & Selects
| Token | Background | Text | Rounded | Padding |
|---|---|---|---|---|
| `{components.text-input}` | `{colors.surface-canvas-light}` | `{colors.ink-deep}` | `{rounded.sm}` | 8px 12px |
| `{components.text-input-focused}` | `{colors.surface-canvas-light}` | `{colors.ink-deep}` | `{rounded.sm}` | 8px 12px |
| `{components.select-violet}` | `{colors.accent-violet-deep}` | `{colors.on-primary}` | `{rounded.md}` | 8px 16px |
**Input rules:**
- Text inputs always use `{colors.hairline-cool}` border, 1px.
- Focus state: border changes to `{colors.ring-focus}` with a 2px outer ring, no inner color shift.
- Select dropdowns on dark canvas use violet fill (`{colors.accent-violet-deep}`) to differentiate from the background.
### Navigation
| Token | Background | Text | Rounded | Padding | Use |
|---|---|---|---|---|---|
| `{components.nav-bar-light}` | `{colors.surface-canvas-light}` | `{colors.ink-deep}` | `{rounded.xs}` | 16px 24px | Primary nav bar on light pages |
| `{components.link-on-dark}` | `{colors.surface-canvas-dark}` | `{colors.on-primary}` | `{rounded.xs}` | 0 | Text links on dark canvas — underline only |
| `{components.link-on-light}` | `{colors.surface-canvas-light}` | `{colors.ink-deep}` | `{rounded.xs}` | 0 | Text links on light canvas — underline only |
### Chips & Pills
| Token | Background | Text | Rounded | Padding | Use |
|---|---|---|---|---|---|
| `{components.pill-neutral-dark}` | `{colors.surface-night}` | `{colors.on-primary}` | `{rounded.xs}` | 4px 8px | Neutral pill (e.g., "Pro", "Beta") on dark |
| `{components.chip-lime-keyword}` | `{colors.accent-lime}` | `{colors.ink-deep}` | `{rounded.xs}` | 0 12px | Headline keyword highlight — wraps individual words |
### Code & Footer
| Token | Background | Text | Rounded | Padding | Use |
|---|---|---|---|---|---|
| `{components.code-block}` | `{colors.surface-night}` | `{colors.on-primary}` | `{rounded.md}` | 16px | Terminal, code snippets |
| `{components.footer-light}` | `{colors.surface-canvas-light}` | `{colors.ink-deep}` | `{rounded.xs}` | 32px 24px | Footer on light canvas |
## Layout Principles
### Spacing Scale
```
{spacing.xxs} →  2px  (icon padding, inline gap)
{spacing.xs}  →  4px  (tight inline gap, badge padding)
{spacing.sm}  →  8px  (button-internal, card-row gap)
{spacing.md}  → 12px  (card internal element gap)
{spacing.lg}  → 16px  (card padding default, section-element gap)
{spacing.xl}  → 24px  (section-card gap, grid-gap)
{spacing.xxl} → 32px  (card-to-card gap, pricing-card padding)
{spacing.section} → 96px (section-to-section vertical padding)
```
### Grid
- **Marketing pages** (home, product): 12-column, `{spacing.xl}` gutter, 1200px max-width.
- **Dashboard / app**: 12-column, `{spacing.lg}` gutter, 1440px max-width for data-dense views.
- **Pricing**: 3- or 4-column auto-fit grid with `{spacing.xl}` gap.
### Whitespace
- Dark canvas sections breathe more — `{spacing.section}` top and bottom padding.
- Light canvas sections for reference content decrease to `{spacing.xxl}` vertical padding to fit more data on screen.
- Cards maintain consistent internal padding based on content density: marketing cards `{spacing.xxl}`, data cards `{spacing.lg}`.
### Surface Switching
- Before every major context change (dark → light, hero → pricing), insert one full-width section divider: a 1px `{colors.hairline-violet}` line on dark canvas, `{colors.hairline-cloud}` on light canvas.
- Don't alternate dark/light/dark/light every section — use at minimum two consecutive dark sections before switching to light.
## Depth & Elevation
Sentri uses a 4-layer elevation system tuned for dark surfaces:
| Layer | Shadow | Use |
|---|---|---|
| 0 — Base | None | Page background, code blocks |
| 1 — Raised | `0 1px 3px rgba(0,0,0,0.3)` | Default cards, nav bar, table rows |
| 2 — Elevated | `0 4px 12px rgba(0,0,0,0.4)` | Hovered cards, dropdowns, tooltips |
| 3 — Modal | `0 12px 40px rgba(0,0,0,0.6)` | Modals, dialogs, slide-outs |
**Rules:**
- On light surfaces, shadows use a cooler tint: `rgba(0,0,0,0.08)` to `rgba(0,0,0,0.2)`.
- On dark surfaces, shadows are deeper and more opaque since the contrast floor is higher.
- Cards never use a border and a shadow simultaneously at the same visual weight — shadow communicates depth, border communicates separation.
- Code blocks sit at Layer 0 — flush with the parent surface.
## Do's and Don'ts
### Do
- ✅ Use a single fill button per surface — the CTA hierarchy must be unambiguous.
- ✅ Keep the palette narrow: midnight, lime, pink, violet. No new accent colors unless you can defend why.
- ✅ Wrap display-headline keywords in `{components.chip-lime-keyword}` chips at a maximum of 2-3 words per chip.
- ✅ Let card backgrounds follow canvas polarity (dark-on-dark, light-on-light).
- ✅ Use `{colors.on-dark-muted}` for secondary text on dark surfaces — never pure white at lowered opacity.
### Don't
- ❌ Don't mix dark and light cards on the same canvas section — a dark section gets dark cards, a light section gets light cards.
- ❌ Don't use `{colors.accent-lime}` as a button background — it's reserved as a typographic highlight and divider stroke.
- ❌ Don't use `{colors.accent-pink}` on text below display size — it's for chart points, sticker outlines, and punctuation accents.
- ❌ Don't add a new button color variant — the system has filled, inverted, ghost, violet-token, and disabled. That's the complete set.
- ❌ Don't place lime chips inside body text — they belong in display headlines only.
- ❌ Don't use blue outside of `{colors.ring-focus}` — the system has no blue brand role.
## Responsive Behavior
### Breakpoints
| Breakpoint | Width | Behavior |
|---|---|---|
| Desktop | ≥ 1024px | Full 12-column grid, full navigation |
| Tablet | 768px – 1023px | 8-column grid, stacked cards, condensed nav |
| Mobile | < 768px | Single-column, hamburger nav, stacked CTAs |
### Rules
- **Touch targets**: minimum 44px × 44px for all interactive elements on tablet and mobile.
- **Typography**: display-hero scales to 48px on tablet, 36px on mobile. display-large scales to 36px / 28px.
- **Cards**: pricing 3-column grid collapses to 2-column on tablet, single-column on mobile.
- **Tables**: horizontal-scroll wrapper on mobile; never force a data table to squish.
- **Navigation**: top nav collapses to hamburger menu below 768px. On dark pages, the hamburger uses `{colors.on-dark-faint}` as its background.
- **Spacing**: section vertical padding halves on mobile (`{spacing.section}` → 48px).
## Agent Prompt Guide
### Quick-Reference Color Cheat-sheet
When coding Sentri-style UI, reference these semantic colors instead of raw hex:
```
Primary button on light → {colors.primary} (#150f23 midnight violet)
Primary text on dark   → {colors.on-primary} (#ffffff)
Hero background        → {colors.surface-canvas-dark} (#1f1633 ink violet)
Card on dark           → {colors.ink-deep} (#1f1633)
Lime keyword chip      → {colors.accent-lime} (#c2ef4e)
Secondary text on dark → {colors.on-dark-muted} (rgba 255,255,255,0.72)
Card border dark       → {colors.hairline-violet} (#362d59)
Focus ring             → {colors.ring-focus} (rgba 59,130,246,0.5)
```
### Prompts for Claude Design
**To scaffold a new marketing page:**
```
"Create a Sentri-style landing page for [product]. Use {colors.surface-canvas-dark}
as the hero canvas, {typography.display-hero} for the headline with lime keyword
chips on 2-3 words, and a single {components.button-primary} as the CTA. Below
the hero, feature 3 {components.card-feature-dark} cards in a 3-column grid."
```
**To scaffold a pricing page:**
```
"Create a Sentri-style pricing page. Use light canvas ({colors.surface-canvas-light})
for the section, {typography.heading-xl} for the title, and 3-4 {components.card-pricing}
cards with one {components.card-pricing-featured} tier. Footer in
{components.footer-light}."
```
**To add a dark dashboard section:**
```
"Add a monitoring dashboard section on dark canvas. Use {components.card-feature-dark}
for stat cards, {typography.heading-sm} for card titles, {typography.caption} for
secondary values in {colors.on-dark-muted}. Data tables use horizontal scroll on mobile."
```
**To generate a contact form:**
```
"Create a contact form on light canvas. Inputs use {components.text-input} styling
with {components.text-input-focused} focus states. The submit button is
{components.button-primary}. Section divider is {colors.hairline-cloud}."
```
### General Prompt Pattern
```
"Build [component/page] in Sentri style.
- Canvas: [dark|light] → use {colors.surface-canvas-dark} or {colors.surface-canvas-light}
- Typography: [headline level] → reference {typography.*} scale
- CTA: single primary button → {components.button-primary} or {components.button-inverted}
- Cards: [count] cards in [N]-column grid → {components.card-feature-dark} or {components.card-pricing}
- All color and spacing decisions should reference the DESIGN.md tokens, not raw values."
```
