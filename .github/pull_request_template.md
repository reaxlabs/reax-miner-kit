## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] `python3 -m unittest discover -s tests` passes
- [ ] `python3 -m py_compile reaxctl reaxlib/*.py` passes
- [ ] Tests added or updated for behaviour changes
- [ ] No weakening of the safety rules in SPEC.md (no coldkeys, no payments, no telemetry)
- [ ] No keys, tokens, wallet files or unredacted logs included
- [ ] No changes to image digests or pins unless this is a release update
