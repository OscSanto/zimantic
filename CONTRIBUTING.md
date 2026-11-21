# Contributing to Zimantic

Thanks for helping! Zimantic is an offline, multilingual search engine for Kiwix ZIM files, built to
run on small hardware such as a Raspberry Pi. Contributions of all sizes are welcome: bug reports, fixes,
documentation, new query sets for the evaluation, and new features.

## What Zimantic aims for

Please keep these in mind; they guide what gets merged:

1. **Offline.** Everything runs without internet access once set up.
2. **Multilingual.** Search should work across languages, not only English. Language-specific rules
   (word lists, stemming, phrases) should not make other languages worse.
3. **Measured.** Changes to ranking or indexing should be measured, not random. 

## How to contribute

1. **Open an issue first** for anything bigger than a small fix, so we can agree on the approach.
2. **Fork** the repository and create a **branch** for your change 
3. Keep each pull request to **one topic**. Several small PRs are easier to review and merge than one
   large one. If you are still working on it, open it as a **draft** and mark it ready when done.
4. Open a pull request against `main` and describe what changed and why.

To pick up changes from `main` while your PR is open, sync your fork (GitHub's **Sync fork** button,
or `git pull upstream main`).

## Before you open a pull request

- **Tests:** if you add or change behaviour that can be tested, add tests in `tests/` (pytest) and make
  sure `python -m pytest` passes. (The project doesn't have a test suite yet; the first PR to add one
  is welcome.)
- **Evaluation, for ranking or indexing changes:** measure `main` and your branch on the same machine,
  with the same ZIM files, and include the comparison in the PR (see
  [evaluation/README.md](evaluation/README.md)):
  ```bash
  # 1. on main: start the server (python -m zimantic serve), then in a second terminal
  python evaluation/run.py --engine zimantic --url http://localhost:8090 --label main
  # 2. stop the server, switch to your branch (git switch my-branch), start it again, then
  python evaluation/run.py --engine zimantic --url http://localhost:8090 --label my-branch
  # 3. compare (the first file is the baseline)
  python evaluation/compare.py evaluation/results/main.json evaluation/results/my-branch.json --split test
  ```
  Only the ZIMs your server has indexes for are measured; the others are listed and left out. Report the
  results **by language and by query type**, and the **fixed / broken** counts, not only the overall
  number. If your change affects how indexes are built, rebuild the indexes on your branch before step 2
  (keep a copy of main's). Tune settings on `--split dev` and report on `--split test`.
- **Documentation:** update the README when behaviour, configuration, commands or published numbers
  change. Numbers in the README should come from a measurement of that version.
- **Compatibility:** if a change breaks existing configs, indexes or commands, say so in the PR and
  give users a clear message or migration path (e.g. "index built with an older version, please rebuild").

## Code style

Match the surrounding code: plain, readable Python, comments that explain *why*. Keep dependencies few;
anything that must run on the Pi should be checked for memory use.

## Licensing

Zimantic is licensed under the **GNU General Public License v3.0 or later** (see [LICENSE](LICENSE)).
By contributing, you agree that your contributions are licensed under the same terms. Content-derived
files (such as evaluation queries quoting Wikipedia) keep their own licenses, noted where they are used.

