# Review guidance for hermetic

1. Documentation overlap is not a finding. A docstring or comment says why the code is shaped as it is, for someone reading that code. `docs/architecture.md` says what a user or maintainer can rely on, and Known limits says what does not work. The two may state the same rationale in their own words, and a design fact may appear in two or three places. Do not flag overlap, repetition or "these copies will drift" on its own, and do not suggest cutting or merging documentation for that reason.
2. Flag documentation only when it is wrong, and name the two places that disagree:
   - A comment, docstring or doc that the diff makes false or stale (it describes an order, a file, a default or a limit the code no longer has).
   - Two copies that already disagree: a list of status codes, paths, limits or defaults that differs between the code, a docstring and the docs.
   - A name that contradicts its documented reason, such as a test named for one cause whose comment gives another.
   - A claim written as fact that the PR description says is unproven.
