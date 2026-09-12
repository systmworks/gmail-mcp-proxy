# Working rules for this repo

## Tagging

Every version entry added to `CHANGELOG.md` gets a matching annotated git tag
(`v0.1`, `v0.2`, …) at push time — not just "working milestones." When a commit
being pushed introduces one or more new `### 0.NN — ...` entries, create an
annotated tag for each (`git tag -a vN.NN <commit> -m "<version title>"`) pointing
at that commit, then push both the commit and the tags (`git push && git push
origin --tags`). If several versions land in one commit (the changelog's own
convention allows this — see its top-of-file note), tag all of them at that same
commit.
