# ORAC Style Guide (the editorial system)

The default visual language for anything built with this system — the operator UI, any surface a
doer generates, any tool that renders to a human. It is deliberately **editorial / magazine**, not
SaaS-dashboard: warm paper, a serif voice, hairline rules, and an unusual accent. The reference
implementation is [`src/orac/ui`](../src/orac/ui) — `styles.css` is the source of truth for the
tokens below; this doc is the rationale and the rules to apply when building something new.

**Three laws. Don't reach for the defaults.**

---

## 1. A 7-colour palette with one unusual accent

Not the default three-colour scheme. Seven working colours, with **mustard** as the standout accent
and **dusty rose** as the second. Surfaces are *tints* of these (via `color-mix`), never new hues.

| Token | Hex | Role |
| --- | --- | --- |
| `--paper` | `#f2ece0` | newsprint background |
| `--ink` | `#1c1915` | warm near-black — text, headlines, rules |
| `--stone` | `#6d6457` | muted warm grey-brown — meta, captions |
| `--rule` | `#ccc1ad` | tan hairline rules |
| `--mustard` | `#bd8717` | **the unusual accent** — primary action, kickers, badges, key figures |
| `--rose` | `#a8586a` | dusty rose — second accent, alerts/blocked, user voice |
| `--moss` | `#586a30` | deep editorial green — done/ok, system voice |

Semantic roles map *onto* the palette — don't invent a new red/green/blue:
`--warn = mustard`, `--danger = rose`, `--ok = moss`. The audit-log voices are
`user = rose`, `agent = mustard`, `system = moss`.

Pick one unusual accent and commit to it. If you swap mustard for another (dusty rose, terracotta,
ochre, teal), keep the *structure*: one standout, one secondary, the rest warm neutrals.

## 2. One serif + one sans. Small radii only.

- **Type pairing is exactly one serif and one sans — never two sans.** Serif (`Fraunces`, falling
  back to `Georgia`) carries the masthead, headlines, and the large numerals — the *voice*. A
  humanist sans carries body, labels, buttons, and meta — the *plumbing*. Tokens: `--serif`,
  `--sans`.
- **No large radii anywhere.** No `rounded-2xl`, no pill shapes for content. Panels are square
  (`border-radius: 0`); controls use a single small `--radius: 3px`. A number badge is a small
  square, not a lozenge.
- Kickers, labels, buttons, and status words are **uppercase, letter-spaced sans** — the small-type
  counterpoint to the serif headlines.

## 3. Build it like a magazine, not a landing page

- **Masthead, not a logo bar.** Oversized serif wordmark, a letter-spaced dateline beneath, closed
  by a *double rule* (thick ink + thin tan). See `.topbar`.
- **Asymmetric layout.** No even three-up card grids. A dominant **feature column** plus a
  rule-separated **sidebar** (`.cockpit-grid` is `1.7fr / 1fr` with a hairline column rule). Let
  importance set width.
- **Kicker labels.** Every section opens with a short uppercase kicker over a serif heading
  (`TRIAGE`, `DISPATCH`, `THE COUNCIL`…). It frames the section like a magazine standfirst.
- **Rules, not cards.** Separate with hairline rules and whitespace, not drop-shadowed boxes. The
  audit log is one ruled column with a coloured left edge per voice — not a stack of cards.
- **Editorial spacing.** Generous top margins and line-height; let the page breathe. Density is the
  enemy of the editorial feel.

---

## Checklist for a new surface

- [ ] Background is `--paper`; text is `--ink`; nothing is pure `#000`/`#fff`.
- [ ] Exactly one unusual accent, used sparingly on the things that matter most.
- [ ] One serif (headlines/numerals) + one sans (everything else). No second sans.
- [ ] No radius over `--radius` (3px); panels square; no pills for content.
- [ ] A masthead with a kicker/dateline, not a logo bar.
- [ ] An asymmetric layout — a feature column and a sidebar, not an even grid.
- [ ] Sections open with a kicker over a serif heading.
- [ ] Separation by hairline rules + whitespace, not shadowed cards.
