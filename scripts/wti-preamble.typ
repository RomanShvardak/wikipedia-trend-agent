// wti-preamble.typ - the pandoc typst template for wikipedia-trend-agent's PDF report.
//
// NOT a copy of the external `cv-render` project's `cvpreamble.typ`: that file carries a
// CV-specific header band and a CV colour scheme, neither of which belongs here. What is
// carried over is the TECHNIQUE, and only the parts this project measured for itself.
//
// Three values arrive as pandoc template variables from `--metadata-file`, never as
// hardcoded text, so there is no second copy of them anywhere:
//
//   $spec-name$    metrics.json's spec_name
//   $as-of$        metrics.json's as_of
//   $font-family$  probed by build_pdf.probe_font_family, NOT hardcoded
//   $lang$         the document language
//
// WHY THE FONT IS A VARIABLE. The technical assignment mandated the stack
// ("Arial", "Liberation Sans", "DejaVu Sans") and promised that Cyrillic could never
// disappear because DejaVu arrives with matplotlib. Measured on this machine: `typst fonts`
// lists `Arial`, and does NOT list `DejaVu Sans` (only `DejaVu Sans Mono`) or
// `Liberation Sans` - matplotlib ships its TTF inside its own package, not in the font
// registry typst reads. And typst does not fail on an unknown family: it WARNS and
// substitutes. A warn-and-substitute is the worst possible outcome in the one place this
// project cares most about, because a document would ship with no Cyrillic and no error.
// So the family is probed and a single verified one is named here.
//
// WHY THE COUNTER IDIOM IS WHAT IT IS. `counter(page).final().display()` - the exact
// expression in the assignment - does not compile on typst 0.15.1: "type array has no
// method `display`". The current page is `counter(page).display()`; the TOTAL is
// `counter(page).final().first()`.
#let spec-name = "$spec-name$"
#let as-of = "$as-of$"

#set page(
  paper: "a4",
  margin: (top: 1.5cm, bottom: 2.0cm, left: 2.0cm, right: 1.5cm),
  footer: context [
    #line(length: 100%, stroke: 0.4pt + rgb("#cccccc"))
    #v(3pt)
    #text(size: 8pt, fill: rgb("#555555"))[
      #spec-name · #as-of ·
      сторінка #counter(page).display() з #counter(page).final().first()
    ]
  ],
)

// 17.5 cm of usable width. The card layout exists because the 11-column metrics table
// cannot fit this: eleven cells at ~1.5 cm each wraps a 70-character cell over six to
// eight lines, which is not "ugly", it is unreadable.
#set text(font: "$font-family$", size: 10pt, lang: "$lang$")
#set par(justify: false, leading: 0.65em)
#set table(
  inset: 6pt,
  stroke: 0.5pt + rgb("#9aa7b4"),
  fill: rgb("#f4f7fa"),
)

#show heading.where(level: 1): it => block[
  #v(4pt) #text(size: 15pt, weight: "bold")[#it.body] #v(2pt)
]
#show heading.where(level: 2): it => block[
  #v(8pt) #text(size: 12pt, weight: "bold")[#it.body] #v(1pt)
  #line(length: 100%, stroke: 0.8pt + rgb("#999999"))
  #v(4pt)
]

// The image inventory: full content width, which at 1350x540 px and 17.5 cm gives
// 17.5 * 540 / 1350 = 7.0 cm tall - the size the assignment calculated.
//
// No figure-suppression rule here. `figure(suppress: true)` was tried and REMOVED: it is
// not a `figure` argument in typst 0.15.1, and the whole preamble then fails to compile
// with "unexpected argument: suppress". It was never needed - the body emits bare
// `![](name){width=100%}` references with no caption attribute, so pandoc produces
// images rather than numbered figures and there is nothing to suppress.
$body$
