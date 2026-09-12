## What this changes, and why

<!-- The problem first. A reviewer who knows what you were trying to do can
     tell whether the change does it. -->

## Checklist

- [ ] **`make check` passes.** It is what CI runs: both test suites, both
      linters, the generated protocol bindings, and the documentation site.
- [ ] **Both languages.** A user-visible capability ships in Python *and*
      TypeScript in the same change, with the same shape and the same defaults
      (`snake_case` against `camelCase`). If the two genuinely differ, both
      docs pages say so.
- [ ] **Tests that fail without the change.** Not tests that pass either way.
- [ ] **Documentation.** A new public name needs prose on a page;
      `make docs-check` fails if it has none.
- [ ] **Nothing about how StandIn is built.** The docs teach how to connect to
      the service and how to extend the SDK, never its implementation,
      infrastructure or internal addresses.
- [ ] **No credentials, real tenant ids or personal data** in the diff or in a
      screenshot.

## Anything a reviewer should look at twice

<!-- A trap you hit, a decision that could have gone the other way, a number
     you chose. This is the most useful part of the description. -->
