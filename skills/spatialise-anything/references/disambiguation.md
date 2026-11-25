# Framing and disambiguation

## 1. Frame the document first

Before resolving any named mention, decide:

- **Country** - default Australia when nothing says otherwise and the text
  reads Australian; say so.
- **State** - from explicit state names, postcodes (2xxx NSW, 3xxx VIC,
  4xxx QLD, 5xxx SA, 6xxx WA, 7xxx TAS, 08xx NT, 26xx/29xx ACT), phone area
  codes, LGA names, organisations with a known patch.
- **Dominant region** - the cluster that most explicit and structured
  mentions fall in.
- **Anchor points** - everything already resolved with confidence >= 0.9.
  Resolve explicit and structured mentions first; they become the `--near`
  points for named ones.

Record the frame in `assemble.py --frame '{"country": "AU", "state": "VIC", "region": "Ballarat"}'`.

## 2. Ask for candidates

`gazetteer.py search NAME --state XX --near LON,LAT LON,LAT ...` returns every
candidate with:

- `name_score` - 1.0 exact; 0.6-0.8 close spelling
- `share` - this candidate's score over the sum of all; a right-place
  probability given the frame
- `right_place_hint` - min(top share, top name score)
- `reasons` - text you can reuse in `rejected`

Search many names at once with `gazetteer.py batch` and a shared frame.

## 3. Decide

- **Start from `right_place_hint`** as the right-place half of confidence,
  then adjust with what the scripts cannot see: feature-type words ("the
  suburb of", "the council"), co-mentioned streets, the author's patch.
- **Resolved neighbours pull ambiguous ones.** Wendouree, Alfredton and Delacombe
  make a fourth name, "Sebastopol", the Ballarat suburb, not the NSW or SA one. Re-run ambiguous searches with
  the newly resolved points in `--near`.
- **Margin matters.** If the top two candidates are close (`margin` < 0.3)
  and nothing in the text separates them, the mention is ambiguous. Do not
  pick the bigger or more famous place by feel.
- **Reasons for every reject**, specific to this document: "document context
  is metropolitan Melbourne", not "lower score".

## Worked examples

These use places deliberately kept out of the evaluation corpus.

**Kensington in a Melbourne note.** Other mentions: Flemington, Ascot Vale,
Moonee Ponds (all VIC, all within 4 km). `--state VIC --near ...` puts
Kensington, VIC above 0.95 share. Resolved, confidence ~0.9; reject
Kensington NSW, QLD, SA and WA with "the other places named are inner
Melbourne".

**Paddington with Woollahra.** Paddington exists in Sydney and Brisbane, both
inner-city. Woollahra resolves to NSW first; `--near` it gives Paddington,
NSW share 0.95. Resolved.

**Kensington alone.** "Drop the forms at Kensington." No state, no other
places. Five candidates with equal share (0.2). Ambiguous, confidence 0.2, all
five as candidates with "no state or nearby place in the text". Ask once at
the end: "Which Kensington - NSW, VIC, QLD, SA or WA?"

**A locality and its LGA.** "Woollahra" returns the locality and the LGA of
the same name. They are one place at two sizes, not rivals: the LGA carries
`nested_in` and does not dilute the share. Pick the locality unless the text
says council, shire or municipality.

**Neighbouring names.** Clayton and Clayton South are separate localities.
Search the exact name the text uses; never collapse one into the other.

**Misspelling.** "Wollongog" -> Wollongong, NSW, name_score 0.76. Even if
unique, confidence is capped by the name score: ambiguous unless the context
makes the intent obvious (an Illawarra report), in which case raise it and
say why.

## When the gazetteer has nothing

- A place type the offline gazetteer does not hold (lake, river, road,
  facility, mountain, park): with the connector attached, `search_gazetteer`
  (see `connector.md`); without it, unresolved with `needs_network`.
- An unknown name: unresolved with no candidates. Do not invent coordinates
  from memory - a remembered coordinate has no resolver and no provenance.
