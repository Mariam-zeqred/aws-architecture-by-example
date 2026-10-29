# Chapter 6 example: the review checklist as code

The reviewer that asks chapters 1 to 5 the 26 questions in [chapter 6](../../ch06-cost-and-security-review-checklist.md), the file of accepted findings, and the roll-up that adds the five cost models up as one system.

```
review.py               `python review.py synth`: every chapter's app.py run in its PRODUCTION flavour
                        (retain_data / protect_data on) into local/templates/<chapter>/;
                        `python review.py`: 26 questions x 6 stacks from those templates, the open and
                        accepted findings, the idle floor, and exit code 1 while any finding is neither
                        fixed nor accepted. `--templates DIR` reads another directory
accepted.json           the findings the review accepts, one reason each, `until` where temporary;
                        an entry that matches nothing fails the run too, so a fixed finding takes
                        its entry with it
rollup.py               `python rollup.py`: the five chapters' cost.py files loaded as they are, the
                        scenario of each that describes the same cooperative, the bill per chapter and
                        in total at two sizes, the largest lines, the three levers
prices.json             the few list prices the idle floor needs, reused from chapters 2 and 5
tests/                  28 pytest tests: every rule on hand-made templates, the acceptance logic, the
                        roll-up's de-duplication and levers, and one end-to-end run over the real
                        templates that pins the chapter's counts
local/                  git-ignored: the synthesised templates (`make ch06-review` writes them)
```

## Run it here (no AWS account needed)

```bash
pip install -r ../../requirements.txt
make -C ../.. check          # everything, chapter 6's review last
make -C ../.. ch06-review    # synth the production flavour, review it, roll the costs up
python review.py synth       # the six templates, about twenty seconds
python review.py             # the matrix, the findings, the idle floor; exit 1 if anything is open
python rollup.py             # the whole system's bill at two sizes, the largest lines, the levers
python -m pytest -q          # from the repo root: this chapter's 28 tests among the others
```

To see what the review looked like before the chapter's fixes and acceptances, empty the `accepted` list in `accepted.json` and run `python review.py`: the same 29 findings come back open. The 110 that the fixes closed (four bucket encryptions, three stage throttles, one table's point-in-time recovery, 102 missing tags) can be brought back by reverting the commits chapter 6 names.

## Reviewing something else

`review.py` reads CloudFormation JSON, not CDK code, so a directory of templates from anywhere works: `python review.py --templates /path/to/dir` expects `dir/<chapter>/<Stack>.template.json` for the chapter names in `CHAPTERS`; change that tuple, or the `load_stacks` function, for another layout. A rule is a function from a `Stack` to a list of findings (or `None` when nothing in the stack is its business), registered in `RULES` with its pillar, its question and the chapter that first answered it; the tests in `tests/test_ch06_review.py` show the shape of a template each rule reads.

## Known limits

- The rules read what a template declares. They cannot see a wrong value (a throttle of 10 000, a lifecycle rule that never fires), a permission granted outside the stack, or anything a deploy creates (the log group Lambda makes for a CDK provider function). Section 6 of the chapter lists what a template review does not cover.
- The idle floor prices four things: alarms, hosted zones, health checks and S3 replication metrics. Resources billed by the hour are a finding (COST-05), not a price. That S3 replication publishes four custom metrics per rule is from documentation the sandbox could not reach, as chapter 5's `prices.json` says.
- The roll-up adds up models, not bills: every number is list price on the dates in each chapter's `prices.json`, and chapter 1's visits, chapter 2's devices and chapter 4's orders are independent assumptions put side by side. The de-duplication (chapter 1's readings table and hosted zone) is a judgement written into `PARTS`.
- The 29 accepted findings and the 102 tags are the state of the templates on 2026-10-29; a CDK upgrade that changes what its provider functions look like moves the counts, and the end-to-end test will say so.
