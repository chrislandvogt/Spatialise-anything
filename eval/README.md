# Evaluation

Two runs, as the design plan sets out: score the output against the corpus,
then compare against Claude with no skill loaded.

## 1. Scripts-only baseline (no model)

```
python eval/run_scripted.py --out runs/scripted
python eval/score.py --outputs runs/scripted --offline \
    --only t0-coords-mixed t0-datums t0-photos t1-assets none-traps
```

This measures what the deterministic scripts do alone. It covers the
explicit items, the asset table and the no-places document. CI runs it on
every push.

## 2. A Claude run with the skill (phase 2 exit test)

For each item in `corpus/manifest.json`, give Claude the item's input files
with the skill loaded and no connector attached, and the prompt:

> Spatialise this. Save the result as `<item_id>.geojson`.

Collect the outputs into one folder and score them:

```
python eval/score.py --outputs runs/claude-offline-<date> --offline
```

The phase 2 exit is the tier 0 bar met in that run: all explicit mentions
correct, no false positives (including the no-places document), every
mention accounted for.

## 3. Skill versus no skill

`claude plugin eval` grades a plugin's results against a run without it. Use
the same corpus prompts for both arms and score each folder with
`score.py`. The question the plan asks: **does the skill clearly beat the
bare model on tiers 2 and 3?** If not, the judgement layer is not earning its
place.

## Reading the scorer

| Outcome | Meaning |
|---|---|
| correct | Right status; within tolerance when placed |
| imprecise | Outside tolerance but inside its own stated uncertainty - honest, loose |
| flagged | Expected a placement, got `ambiguous` - safe, still a miss |
| not_attempted | Offline, network-only mention, correctly left unresolved |
| unresolved_miss | Expected a placement, got `unresolved` |
| confident_wrong | Resolved at or above threshold and wrong beyond its own error bar; or resolved where ambiguous/unresolved was expected |
| dropped | No feature for the mention at all |
| false_positive | A feature for something that is not a mention |

**Confident-wrong rate** = confident_wrong / all features resolved at or
above the threshold. The number a government customer cares about.

The bars in `score.py` are the plan's proposals, to be calibrated on the
first full run - not commitments. Set the confidence threshold from that
run too, and record the change in `contract.DEFAULT_THRESHOLD`.
