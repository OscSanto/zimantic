# Vendored MathJax fonts

These are the MathJax 2.7.5 `HTML-CSS/TeX/woff` web fonts from the
[`mathjax` npm package](https://www.npmjs.com/package/mathjax), vendored so
offline Stack Exchange captures can render math when their ZIM does not include
the font files.

- Version: 2.7.5
- Source: `fonts/HTML-CSS/TeX/woff/` in the upstream npm package
- License: Apache License 2.0, see `LICENSE` in this directory

The `/content/` shell serves these files as a fallback for missing MathJax font
entries under `/article/<zim>/.../fonts/HTML-CSS/TeX/woff/`.
